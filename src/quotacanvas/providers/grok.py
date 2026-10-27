"""Grok: Grok CLI のログイン (~/.grok/auth.json) で週間の利用枠を取得する。

取得先は Grok CLI の `/usage`（Usage limit タブ）が使っているのと同じ
`cli-chat-proxy.grok.com/v1/billing?format=credits`。非公開の API なので形が変わりうる。

トークンの更新は Grok CLI に任せる (自分ではリフレッシュしない):
- providers.grok.token_refresh = "cli": ログイン切れ (`expires_at` 経過) か
  HTTP 401/403 のとき `grok models` を裏で 1 回実行する。モデルは呼ばない
  (枠を消費しない) のに認証付き API を叩くので、期限切れなら CLI 自身が
  auth.json のトークンを更新してリトライする。`run` のあいだだけ有効で、
  30 分に 1 回まで (ただし exit 0 でも auth.json が進まなかったときは
  5 分後に再試行)。成功したら run ループに反映要求を出して取り直させる。
  fetch はブロックしない。
- "off": CLI は呼ばない。期限切れは「grok を一度起動してください」、401/403 は
  従来どおり HTTP エラーのまま。
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..i18n import t
from ..models import ProviderUsage, Window
from ._base import (
    USER_AGENT, CredentialError, Detection, Provider,
    _parse_iso, _utc_now, _window_identity, http_get_json,
)

log = logging.getLogger("quotacanvas.grok")

GROK_USAGE_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
_FALLBACK_CLIENT_VERSION = "1.0.70"

CLI_REFRESH_TIMEOUT = 120.0        # grok models 1 回の上限 (秒)
CLI_REFRESH_COOLDOWN = 30 * 60.0   # grok を起動してよい最小間隔 (秒)
CLI_RETRY_SOON = 5 * 60.0          # exit 0 でも auth.json が進まなかったときの再試行間隔
CLI_RETRY_SOON_MAX = 3             # 5 分での再試行が続いてよい回数。超えたら 30 分あける

TOKEN_REFRESH_MODES = ("cli", "off")  # providers.grok.token_refresh の値

# grok 実行ファイルのフォールバック候補 (which("grok") で見つからないとき)。
# LaunchAgent は PATH が最小限なため ~ 配下も明示する。
_GROK_FALLBACKS = (
    "/opt/homebrew/bin/grok",
    "/usr/local/bin/grok",
    "~/.grok/bin/grok",
)

# 子プロセスに渡す環境変数はこれだけ (os.environ 全部は渡さない)。
# GROK_HOME は親が読む認証の場所と子が更新する場所を揃えるため。
_ENV_KEYS = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "PATH", "SHELL", "GROK_HOME")
_PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

_refresh_mode: Optional[str] = None        # None = 未設定 (run 以外は触らない)
_cooldown_until: Optional[float] = None    # 次に grok を起動してよい monotonic 時刻
_last_not_found: Optional[float] = None    # 最後に「CLI が見つからない」警告を出した時刻
_soon_retries = 0                          # 続けて 5 分再試行にした回数
_refresh_lock = threading.Lock()           # 同時に走る更新スレッドは 1 本まで
_refresh_thread: Optional[threading.Thread] = None
_refresh_started_at: Optional[float] = None  # 更新スレッドの開始時刻 (反映要求の待ちに使う)
_child: Optional[subprocess.Popen] = None    # 起動中の子プロセス (終了時に killpg)


def _grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")


def _client_version() -> str:
    """Installed CLI version from the ~/.grok/bin/grok -> grok-X.Y.Z symlink."""
    try:
        target = os.readlink(_grok_home() / "bin" / "grok")
    except OSError:
        return _FALLBACK_CLIENT_VERSION
    name = Path(target).name
    return name[len("grok-"):] if name.startswith("grok-") else _FALLBACK_CLIENT_VERSION


# ----------------------------------------------------------- credentials

def _read_entry(now: Optional[datetime] = None) -> Tuple[str, Optional[datetime]]:
    """(access token, expires_at) of the first login entry in ~/.grok/auth.json.

    期限切れでも値はそのまま返す (fetch が CLI 更新の要否を判断するため)。
    ファイルが無い・読めない・有効なエントリが無いときだけ CredentialError。
    """
    path = _grok_home() / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CredentialError(t("providers.no_credentials")) from None
    except (OSError, json.JSONDecodeError):
        raise CredentialError(t("providers.unreadable")) from None
    entry = next((v for v in data.values() if isinstance(v, dict) and v.get("key")), None) \
        if isinstance(data, dict) else None
    if entry is None:
        raise CredentialError(t("providers.no_credentials"))
    return entry["key"], _parse_iso(entry.get("expires_at"))


def read_grok_credentials(now: Optional[datetime] = None) -> str:
    """Access token of the first login entry in ~/.grok/auth.json."""
    token, expires = _read_entry(now)
    if expires is not None and expires <= (now or _utc_now()):
        # The CLI refreshes the token itself; we only read it.
        raise CredentialError(t("providers.login_expired_run_grok"))
    return token


# ---------------------------------------------- refresh via Grok CLI

def set_token_refresh(mode: str) -> None:
    """トークン自動更新の設定を反映する ("cli" = CLI に更新させる / "off" = しない)。

    "off" への切り替えのとき、走っている grok 子プロセスがいれば kill して
    更新スレッドの待ちを解く。
    """
    global _refresh_mode
    if mode not in TOKEN_REFRESH_MODES:
        raise ValueError("token_refresh は %s のいずれか" % (TOKEN_REFRESH_MODES,))
    _refresh_mode = mode
    if mode == "off":
        _kill_running_child()


def _kill_running_child() -> None:
    """起動中の子プロセス (grok models) がいれば killpg する。"""
    proc = _child
    if proc is not None and proc.poll() is None:
        _kill_process_group(proc)


atexit.register(_kill_running_child)  # 終了時にもみ消し (実行中の grok を残さない)


def _prefixed_env() -> Dict[str, str]:
    """子プロセス用の限定 env。PATH の先頭に LaunchAgent でも届く場所を足す。"""
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    extra = os.pathsep + os.environ["PATH"] if os.environ.get("PATH") else ""
    env["PATH"] = _PATH_PREFIX + extra
    return env


def _find_grok_cli(search_path: Optional[str] = None) -> Optional[str]:
    """grok 実行ファイルのパス。見つからなければ None。"""
    found = shutil.which("grok", path=search_path)
    if found:
        return found
    for candidate in _GROK_FALLBACKS:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _current_state() -> Tuple[Optional[str], Optional[datetime]]:
    """auth.json の (key, expires_at)。読めなければ (None, None)。"""
    try:
        return _read_entry()
    except CredentialError:
        return (None, None)


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
    """認証情報が更新された (key が変わったか expires_at が進んだ) か。"""
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
    """(ブロッキング本体) `grok models` を起動し、CLI 自身にトークンを更新させる。

    grok models はモデルを呼ばない (枠を消費しない) 代わりに認証付き API を
    叩くので、トークンが期限切れなら CLI が更新してリトライする。成功 = exit 0
    かつ、auth.json を読み直した (key, expires_at) が起動前より進んでいる。
    クールダウンは「起動できたとき」だけ記録する (見つからない・起動失敗は
    記録しない。ただし「見つからない」警告は 30 分に 1 回)。exit 0 でも
    auth.json が進まなかったときだけ 5 分にする (期限切れ後すぐ再試行できるように)。
    """
    global _cooldown_until, _child, _last_not_found, _soon_retries
    now = time.monotonic()
    if _cooldown_until is not None and now < _cooldown_until:
        log.debug("grok CLI でのトークン更新はクールダウン中")
        return False
    env = _prefixed_env()
    grok_cli = _find_grok_cli(env["PATH"])
    if not grok_cli:
        if _last_not_found is None or now - _last_not_found >= CLI_REFRESH_COOLDOWN:
            log.warning("grok CLI が見つからないためトークンを自動更新できない")
            _last_not_found = now
        return False
    if _refresh_mode != "cli":  # 待っているあいだに off / 未設定になった
        return False
    bindir = os.path.dirname(grok_cli)
    if bindir and bindir not in env["PATH"].split(os.pathsep):
        env["PATH"] = env["PATH"] + os.pathsep + bindir  # 見つけたバイナリの仲間も使えるように
    before = _current_state()
    try:
        proc = subprocess.Popen(
            [grok_cli, "models"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd=tempfile.gettempdir(),
            env=env, start_new_session=True,
        )
    except OSError:
        log.warning("grok CLI を起動できなかった")  # 起動失敗はクールダウンに入れない
        return False
    _child = proc
    _cooldown_until = now + CLI_REFRESH_COOLDOWN
    try:
        returncode = proc.wait(timeout=CLI_REFRESH_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        _child = None
        log.warning("grok CLI が %s 秒で終わらないため強制終了した", CLI_REFRESH_TIMEOUT)
        return False
    _child = None
    if returncode != 0:
        log.warning("grok CLI によるトークン更新に失敗した (exit %s)", returncode)
        return False
    if not _advanced(before, _current_state()):
        # exit 0 でも更新されていない: 期限が切れたあとすぐ再試行できるよう 5 分だけ空ける
        # (grok models は枠を使わないが、続くようなら CLI_RETRY_SOON_MAX 回で 30 分に戻す)
        if _soon_retries < CLI_RETRY_SOON_MAX:
            _soon_retries += 1
            _cooldown_until = now + CLI_RETRY_SOON
        log.warning("grok CLI は終了したが auth.json のトークンは更新されなかった")
        return False
    _soon_retries = 0
    log.info("grok CLI を実行してログイン トークンを更新した")
    return True


def _wait_for_refresh_gap(min_gap: float) -> None:
    """run ループの反映要求チェック (REFRESH_MIN_GAP) で捨てられないよう、更新
    スレッドの開始から min_gap + 1 秒経つまで待ってから request_refresh する。
    (time.sleep はテストで差し替え可能なように grok.time 経由で呼ぶ)"""
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
        log.warning("grok CLI によるトークン更新でエラー (%s)", type(exc).__name__)
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
                                  name="grok-token-refresh", daemon=True)
        _refresh_thread = thread
        thread.start()
        return True


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """Grok CLI のログイン情報があるか。通信しない。"""
    path = _grok_home() / "auth.json"
    if not path.exists():
        return Detection(False, None, t("providers.no_credentials"))
    try:
        read_grok_credentials()
    except CredentialError as exc:
        return Detection(False, None, str(exc))
    return Detection(True, t("providers.detect.grok"))


# --------------------------------------------------------------- parser

def _split_grok_prefix(product: str) -> str:
    """先頭の "Grok" を外した名前 ("GrokBuild" -> "Build")。外した結果が
    空なら元の名前をそのまま返す。"""
    body = product[len("Grok"):] if product.startswith("Grok") else product
    return body or product


def parse_grok(data: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[str, List[Window]]:
    """(name, windows) from the billing?format=credits response.

    config.productUsage があれば各製品 (GrokBuild / GrokTasks / ...) を
    メイン窓の内訳として part_of 付きの Window にする。形が違う・壊れて
    いる応答では内訳を諦めてメイン窓だけ返す。"""
    config = data.get("config") or {}
    period = config.get("currentPeriod") or {}
    start = _parse_iso(period.get("start") or config.get("billingPeriodStart"))
    end = _parse_iso(period.get("end") or config.get("billingPeriodEnd"))
    used = config.get("creditUsagePercent")
    if used is None and start and end:
        # proto3 の JSON は 0 の項目を省く。リセット直後 (使用率 0%) は
        # creditUsagePercent が来ないので、期間があれば 0% とみなす。
        used = 0.0
    windows: List[Window] = []
    if used is not None and start and end and end > start:
        key, label, length = _window_identity(int((end - start).total_seconds()))
        main = Window("grok", key, label, float(used), end, length)
        windows.append(main)
        products = config.get("productUsage")
        if isinstance(products, list):
            seen = set()            # 正規化後の key (重複は後のものを捨てる)
            budget = float(used)    # 内訳の累計はメイン窓の使用率を超えない
            for item in products:
                if not isinstance(item, dict):
                    continue
                product = item.get("product")
                usage = item.get("usagePercent")
                if not isinstance(product, str) or not product:
                    continue
                if isinstance(usage, bool) or not isinstance(usage, (int, float)):
                    continue
                if usage <= 0:
                    continue  # 0 / 負の値の製品は内訳にしない
                name = _split_grok_prefix(product)
                key = f"{main.key}_{name.lower()}"
                if key in seen:
                    continue
                seen.add(key)
                seg = min(float(usage), budget)  # メイン窓を超える分は切り詰める
                if seg <= 0.0:                  # 切り詰めて 0 になった分は捨てる
                    continue
                budget -= seg
                windows.append(Window(
                    "grok", key, f"{main.label} {name}",
                    seg, main.resets_at, main.length, part_of=main.key))
    return "Grok", windows


# ---------------------------------------------------------------- fetch

def fetch(now: datetime) -> ProviderUsage:
    try:
        token, expires = _read_entry(now)
    except CredentialError:
        raise  # ファイル / エントリ自体が無い場合は更新しても始まらない
    if expires is not None and expires <= now:
        # ログイン切れ: token_refresh = "cli" なら裏で grok を起動して更新させる
        if _refresh_mode == "cli":
            _spawn_refresh_thread()  # 更新はバックグラウンドで。成功したら取り直される
            raise CredentialError(t("providers.login_expired_refreshing")) from None
        raise CredentialError(t("providers.login_expired_run_grok"))
    try:
        data = http_get_json(GROK_USAGE_URL, {
            "Authorization": "Bearer " + token,
            "x-grok-client-version": _client_version(),
            "x-grok-client-mode": "billing",
            "User-Agent": USER_AGENT,
        })
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        if _refresh_mode != "cli":
            raise  # "off" と未設定 (run 以外): 従来どおり HTTP エラー
        _spawn_refresh_thread()
        raise CredentialError(t("providers.login_expired_refreshing")) from None
    name, windows = parse_grok(data, now)
    return ProviderUsage("grok", name, windows, None, now)


PROVIDER = Provider(
    id="grok", name="Grok", needs_key=False, detect=detect, fetch=fetch,
)
