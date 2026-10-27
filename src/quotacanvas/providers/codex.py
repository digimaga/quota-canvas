"""Codex: Codex CLI のログイン (~/.codex/auth.json) で利用枠を取得する。

トークンの更新は Codex CLI に任せる (自分ではリフレッシュしない):
- providers.codex.token_refresh = "cli": HTTP 401/403 のとき `codex exec` を
  最小のプロンプトで裏から 1 回実行し、CLI 自身に auth.json を書き戻させる
  (Codex には枠を消費しない更新コマンドが無いため)。`run` のあいだだけ有効で、
  30 分に 1 回まで (ただし exit 0 でも auth.json が進まなかったときは 5 分後に
  再試行)。成功したら run ループに反映要求を出して取り直させる。fetch は
  ブロックしない。
- "off": CLI は呼ばない。401/403 は従来どおり HTTP エラーのまま。
- 未設定 (None): run 以外 (once / detect など)。CLI は起動せず "off" と同じ。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..i18n import t
from ..models import ProviderUsage, ResetCredit, Window
from ._base import (
    USER_AGENT, CredentialError, Detection, Provider,
    _from_epoch, _parse_iso, _short_reason, _window_identity, http_get_json,
)

log = logging.getLogger("quotacanvas.codex")

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# リセット権 (週間枠を即リセットできる権利) の一覧。閲覧 (GET) 専用:
# 同じ系統に権利を消費する API があるが、quota-canvas は URL を定義せず
# 絶対に呼ばない。
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"

# リセット権一覧の取得はこの間隔までに 1 回 (前回の結果を返す)。
# キャッシュは account_id ごとに有効 (アカウントが変わったら取り直す)。
RESET_CREDITS_TTL = timedelta(minutes=15)
_RESET_CACHE: Dict[str, Any] = {"at": None, "credits": None, "account_id": None}

CLI_REFRESH_TIMEOUT = 120.0        # codex exec 1 回の上限 (秒)
CLI_REFRESH_COOLDOWN = 30 * 60.0   # codex を起動してよい最小間隔 (秒)。codex exec は枠を使うので結果によらず守る

TOKEN_REFRESH_MODES = ("cli", "off")  # providers.codex.token_refresh の値

# codex 実行ファイルのフォールバック候補 (which("codex") で見つからないとき)。
# LaunchAgent は PATH が最小限なため ~ 配下も明示する。
_CODEX_FALLBACKS = (
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "~/.codex/bin/codex",
)

# 子プロセスに渡す環境変数はこの 7 つだけ (os.environ 全部は渡さない)。
_ENV_KEYS = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "PATH", "SHELL")
_PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

_refresh_mode: Optional[str] = None        # None = 未設定 (run 以外は触らない)
_cooldown_until: Optional[float] = None    # 次に codex を起動してよい monotonic 時刻
_last_not_found: Optional[float] = None    # 最後に「CLI が見つからない」警告を出した時刻
_refresh_lock = threading.Lock()           # 同時に走る更新スレッドは 1 本まで
_refresh_thread: Optional[threading.Thread] = None
_refresh_started_at: Optional[float] = None  # 更新スレッドの開始時刻 (反映要求の待ちに使う)
_child: Optional[subprocess.Popen] = None    # 起動中の子プロセス (終了時に killpg)


# ----------------------------------------------------------- credentials

def read_codex_credentials() -> Tuple[str, Optional[str]]:
    """(access_token, account_id) from ~/.codex/auth.json."""
    path = Path.home() / ".codex" / "auth.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CredentialError(t("providers.no_credentials")) from None
    except OSError:
        raise CredentialError(t("providers.unreadable")) from None
    try:
        tokens = json.loads(raw).get("tokens") or {}
    except json.JSONDecodeError:
        raise CredentialError(t("providers.malformed")) from None
    token = tokens.get("access_token")
    if not token:
        raise CredentialError(t("providers.no_credentials"))
    return token, tokens.get("account_id")


# ------------------------------------------------- refresh via Codex CLI

def set_token_refresh(mode: str) -> None:
    """トークン自動更新の設定を反映する ("cli" = CLI に更新させる / "off" = しない)。

    "off" への切り替えのとき、走っている codex 子プロセスがいれば kill して
    更新スレッドの待ちを解く。
    """
    global _refresh_mode
    if mode not in TOKEN_REFRESH_MODES:
        raise ValueError("token_refresh は %s のいずれか" % (TOKEN_REFRESH_MODES,))
    _refresh_mode = mode
    if mode == "off":
        _kill_running_child()


def _kill_running_child() -> None:
    """起動中の子プロセス (codex exec) がいれば killpg する。"""
    proc = _child
    if proc is not None and proc.poll() is None:
        _kill_process_group(proc)


atexit.register(_kill_running_child)  # 終了時にもみ消し (実行中の codex を残さない)


def _prefixed_env() -> Dict[str, str]:
    """子プロセス用の限定 env。PATH の先頭に LaunchAgent でも届く場所を足す。"""
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    extra = os.pathsep + os.environ["PATH"] if os.environ.get("PATH") else ""
    env["PATH"] = _PATH_PREFIX + extra
    return env


def _find_codex_cli(search_path: Optional[str] = None) -> Optional[str]:
    """codex 実行ファイルのパス。見つからなければ None。"""
    found = shutil.which("codex", path=search_path)
    if found:
        return found
    for candidate in _CODEX_FALLBACKS:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _read_auth_state() -> Tuple[Optional[str], Optional[datetime]]:
    """auth.json の (access_token, last_refresh)。読めなければ (None, None)。

    値をログや例外に出すことはない (成功判定の比較にだけ使う)。
    """
    path = Path.home() / ".codex" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (None, None)
    tokens = data.get("tokens") if isinstance(data, dict) else None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    raw = data.get("last_refresh") if isinstance(data, dict) else None
    last = _parse_iso(raw) if isinstance(raw, str) else _from_epoch(raw)
    return (token or None), last


def _kill_process_group(proc: subprocess.Popen) -> None:
    """proc のプロセスグループを SIGKILL して終わるのを待つ。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass  # 既に終わっている
    try:
        proc.wait()
    except OSError:
        pass


def _advanced(before: Tuple[Optional[str], Optional[datetime]],
              after: Tuple[Optional[str], Optional[datetime]]) -> bool:
    """認証情報が更新された (access_token が変わったか last_refresh が進んだ) か。"""
    bkey, bexp = before
    akey, aexp = after
    if not akey:
        return False  # 読めなくなっているのは失敗扱い
    if akey != bkey:
        return True
    if bexp is None:
        return aexp is not None
    return aexp is not None and aexp > bexp


def _refresh_via_cli() -> bool:
    """(ブロッキング本体) `codex exec` を最小のプロンプトで起動し、CLI 自身に
    トークンを更新させる。

    Codex には枠を消費しない更新コマンドが無い (`codex login` は対話専用、
    `codex login status` は表示のみで auth.json を書き換えない) ので、Claude と
    同じく 1 回実行する方式。成功 = exit 0 かつ、auth.json を読み直した
    (access_token, last_refresh) が起動前より進んでいる。クールダウンは
    「起動できたとき」だけ記録する (見つからない・起動失敗は記録しない。ただし
    「見つからない」警告は 30 分に 1 回)。codex exec は枠を使うので、exit 0 でも
    auth.json が進まなかったときも 30 分あける。
    """
    global _cooldown_until, _child, _last_not_found
    now = time.monotonic()
    if _cooldown_until is not None and now < _cooldown_until:
        log.debug("codex CLI でのトークン更新はクールダウン中")
        return False
    env = _prefixed_env()
    codex_cli = _find_codex_cli(env["PATH"])
    if not codex_cli:
        if _last_not_found is None or now - _last_not_found >= CLI_REFRESH_COOLDOWN:
            log.warning("codex CLI が見つからないためトークンを自動更新できない")
            _last_not_found = now
        return False
    if _refresh_mode != "cli":  # 待っているあいだに off / 未設定になった
        return False
    bindir = os.path.dirname(codex_cli)
    if bindir and bindir not in env["PATH"].split(os.pathsep):
        env["PATH"] = env["PATH"] + os.pathsep + bindir  # 見つけたバイナリの仲間も使えるように
    before = _read_auth_state()
    try:
        proc = subprocess.Popen(
            [codex_cli, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
             "--ephemeral", "-C", tempfile.gettempdir(), "ok"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd=tempfile.gettempdir(),
            env=env, start_new_session=True,
        )
    except OSError:
        log.warning("codex CLI を起動できなかった")  # 起動失敗はクールダウンに入れない
        return False
    _child = proc
    _cooldown_until = now + CLI_REFRESH_COOLDOWN
    try:
        returncode = proc.wait(timeout=CLI_REFRESH_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        _child = None
        log.warning("codex CLI が %s 秒で終わらないため強制終了した", CLI_REFRESH_TIMEOUT)
        return False
    _child = None
    if returncode != 0:
        log.warning("codex CLI によるトークン更新に失敗した (exit %s)", returncode)
        return False
    if not _advanced(before, _read_auth_state()):
        log.warning("codex CLI は終了したが auth.json のトークンは更新されなかった")
        return False
    log.info("codex CLI を実行して OAuth トークンを更新した")
    return True


def _wait_for_refresh_gap(min_gap: float) -> None:
    """run ループの反映要求チェック (REFRESH_MIN_GAP) で捨てられないよう、更新
    スレッドの開始から min_gap + 1 秒経つまで待ってから request_refresh する。
    (time.sleep はテストで差し替え可能なように codex.time 経由で呼ぶ)"""
    started = _refresh_started_at if _refresh_started_at is not None else time.monotonic()
    rest = started + min_gap + 1.0 - time.monotonic()
    if rest > 0:
        time.sleep(rest)


def _refresh_worker() -> None:
    """更新スレッドの本体。fetch をブロックしないよう別スレッドで動く。"""
    if _refresh_mode != "cli":
        return
    try:
        ok = _refresh_via_cli()
    except Exception as exc:  # スレッドの外へ出さない。メッセージは型名だけ (秘密を含みうるため)
        log.warning("codex CLI によるトークン更新でエラー (%s)", type(exc).__name__)
        return
    if not ok or _refresh_mode != "cli":  # 子が終わったあとに off になっていたら黙る
        return
    try:
        from .. import refresh as refresh_mod  # 循環 import を避けるため関数内で
        _wait_for_refresh_gap(refresh_mod.REFRESH_MIN_GAP)
        refresh_mod.request_refresh()
        log.info("トークンが更新されたので利用量を取り直させる")
    except OSError as exc:
        log.warning("反映要求ファイルを作成できません (%s)", exc)


def _spawn_refresh_thread() -> bool:
    """トークン更新を daemon スレッドで開始する。既に走っていれば何もしない。"""
    global _refresh_thread, _refresh_started_at
    if _refresh_mode != "cli":
        return False
    with _refresh_lock:
        thread = _refresh_thread
        if thread is not None and thread.is_alive():
            return False  # 同時に 1 本だけ
        _refresh_started_at = time.monotonic()
        thread = threading.Thread(target=_refresh_worker,
                                  name="codex-token-refresh", daemon=True)
        _refresh_thread = thread
        thread.start()
        return True


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """Codex CLI のログイン情報があるか。通信しない。"""
    try:
        read_codex_credentials()
    except CredentialError as exc:
        return Detection(False, None, str(exc) or t("providers.no_credentials"))
    return Detection(True, t("providers.detect.codex"))


# --------------------------------------------------------------- parser

def parse_codex(data: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[str, List[Window]]:
    """(name, windows) from the ChatGPT/Codex wham/usage response."""
    rate = data.get("rate_limit") or {}
    windows: List[Window] = []
    for entry in (rate.get("primary_window"), rate.get("secondary_window")):
        if not isinstance(entry, dict):
            continue  # window may be null
        used = entry.get("used_percent")
        seconds = entry.get("limit_window_seconds")
        if used is None or not seconds:
            continue
        key, label, length = _window_identity(int(seconds))
        windows.append(Window(
            "codex", key, label, float(used),
            _from_epoch(entry.get("reset_at")), length,
        ))
    plan = str(data.get("plan_type") or "").strip().capitalize()
    return ("Codex " + plan).strip(), windows


# -------------------------------------------------------- reset credits

def parse_reset_credits(data: Dict[str, Any], now: datetime) -> List[ResetCredit]:
    """wham/rate-limit-reset-credits の応答 -> 使えるリセット権のリスト。

    status が "available" で、is_supported_by_plan が明示的に False でない
    ものだけ。期限切れ (expires_at <= now) は除外する。並びは期限の早い順
    (期限が同じなら granted_at の早い順)。
    """
    credits: List[ResetCredit] = []
    for entry in (data.get("credits") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "available":
            continue
        if entry.get("is_supported_by_plan") is False:
            continue
        expires_at = _parse_iso(entry.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue  # 期限切れ
        granted_at = _parse_iso(entry.get("granted_at"))
        title = str(entry.get("title") or "").strip() or t("providers.reset_credit")
        credits.append(ResetCredit(title, granted_at, expires_at))
    far = datetime.max.replace(tzinfo=timezone.utc)  # 日時なしは最後に並べる
    credits.sort(key=lambda c: (c.expires_at or far, c.granted_at or far))
    return credits


def _unexpired(credits: List[ResetCredit], now: datetime) -> List[ResetCredit]:
    """expires_at が now より後のものだけ残す (日時なしは残す)。"""
    return [c for c in credits if c.expires_at is None or c.expires_at > now]


def fetch_reset_credits(now: datetime, token: Optional[str] = None,
                        account_id: Optional[str] = None) -> List[ResetCredit]:
    """リセット権の一覧を GET する (15 分に 1 回まで、前回の結果を返す)。

    失敗しても例外は出さず、前回の結果 (無ければ空リスト) を返す。どの経路
    でも返す前に期限切れ (expires_at <= now) を除く。キャッシュは account_id
    ごとに有効で、変わっていれば TTL 内でも取り直す (失敗時に前アカウントの
    件を返さない)。ログには短い理由だけを出す (トークンは出さない)。GET 以外
    の要求は一切行わない。
    """
    if token is None or account_id is None:
        try:
            token, account_id = read_codex_credentials()
        except Exception as exc:  # noqa: BLE001 - 利用量の取得を壊さない
            log.warning("Codex の認証情報を読めません: %s", _short_reason(exc))
            # アカウント不明でキャッシュの account_id と比べられないので、
            # 前回値を期限で絞って返す。
            return _unexpired(list(_RESET_CACHE["credits"] or []), now)
    cached_at = _RESET_CACHE["at"]
    if (cached_at is not None and _RESET_CACHE.get("account_id") == account_id
            and now - cached_at < RESET_CREDITS_TTL):
        return _unexpired(list(_RESET_CACHE["credits"] or []), now)
    try:
        data = http_get_json(CODEX_RESET_CREDITS_URL, _headers(token, account_id))
        credits = parse_reset_credits(data, now)
    except Exception as exc:  # noqa: BLE001 - 利用量の取得を壊さない
        log.warning("Codex のリセット権一覧を取得できません: %s", _short_reason(exc))
        if _RESET_CACHE.get("account_id") != account_id:
            return []  # 前アカウントの件は返さない
        return _unexpired(list(_RESET_CACHE["credits"] or []), now)
    _RESET_CACHE["at"] = now
    _RESET_CACHE["credits"] = credits
    _RESET_CACHE["account_id"] = account_id
    return _unexpired(list(credits), now)


# ---------------------------------------------------------------- fetch

def _headers(token: str, account_id: Optional[str]) -> Dict[str, str]:
    """wham 系 API 共通のヘッダ (利用量もリセット権一覧も同じ)。"""
    return {
        "Authorization": "Bearer " + token,
        "ChatGPT-Account-Id": account_id or "",
        "User-Agent": USER_AGENT,
    }


def fetch(now: datetime) -> ProviderUsage:
    token, account_id = read_codex_credentials()
    try:
        data = http_get_json(CODEX_USAGE_URL, _headers(token, account_id))
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        if _refresh_mode != "cli":
            raise  # "off" と未設定 (run 以外): 従来どおり HTTP 401/403 のエラー
        _spawn_refresh_thread()  # 更新はバックグラウンドで。成功したら取り直される
        raise CredentialError(t("providers.token_expired_refreshing")) from None
    name, windows = parse_codex(data, now)
    usage = ProviderUsage("codex", name, windows, None, now)
    # リセット権は付随情報。失敗しても利用量には影響しない (内部で警告だけ)。
    usage.reset_credits = fetch_reset_credits(now, token, account_id)
    return usage


PROVIDER = Provider(
    id="codex", name="Codex", needs_key=False, detect=detect, fetch=fetch,
)