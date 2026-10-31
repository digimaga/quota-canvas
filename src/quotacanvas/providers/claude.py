"""Claude: Claude Code の OAuth トークン (login キーチェーン) で利用枠を取得する。

トークンの更新は Claude Code CLI に任せる (自分で OAuth refresh はしない):
- providers.claude.token_refresh = "cli": 期限切れ間近か HTTP 401/403 のとき
  `claude -p` を軽く 1 回バックグラウンドで実行し、CLI 自身にキーチェーンへ
  書き戻させる。`run` のあいだだけ有効で、30 分に 1 回まで (ただし exit 0 でも
  expiresAt が進まなかったときは 5 分後に再試行)。成功したら run ループに
  反映要求を出して取り直させる。fetch はブロックしない。
- "off": CLI は呼ばない。401/403 は「claude を一度起動してください」のエラー。
- 未設定 (None): run 以外 (once / detect など)。CLI は起動せず "off" と同じ。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.error
from datetime import datetime, timedelta, timezone
from shutil import rmtree
from typing import Any, Dict, List, Optional, Tuple

from ..platform_support import process, security
from ..platform_support.process import kill_group as _kill_process_group
from ..i18n import t
from ..models import ProviderUsage, Window
from ._base import (
    TIMEOUT, USER_AGENT, CredentialError, Detection, Provider,
    _from_epoch_millis, _parse_iso, http_get_json,
)

log = logging.getLogger("quotacanvas.claude")

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CLI_REFRESH_TIMEOUT = 120.0        # claude -p 1 回の上限 (秒)
CLI_REFRESH_COOLDOWN = 30 * 60.0   # claude -p を起動してよい最小間隔 (秒)
CLI_RETRY_SOON = 5 * 60.0          # exit 0 でも expiresAt が進まなかったときの再試行間隔
EXPIRY_MARGIN = timedelta(minutes=5)  # これより先に切れるなら先回りで更新する

TOKEN_REFRESH_MODES = ("cli", "off")  # providers.claude.token_refresh の値

# claude 実行ファイルのフォールバック候補 (which("claude") で見つからないとき)。
# LaunchAgent は PATH が最小限なため ~ 配下も明示する。
_CLAUDE_FALLBACKS = process.CLAUDE_PATHS

_refresh_mode: Optional[str] = None        # None = 未設定 (run 以外は触らない)
_cooldown_until: Optional[float] = None    # 次に claude -p を起動してよい monotonic 時刻
_last_not_found: Optional[float] = None    # 最後に「CLI が見つからない」警告を出した時刻
_last_expiry: Optional[datetime] = None    # 最後にキーチェーンを読んだときの expiresAt
_refresh_lock = threading.Lock()           # 同時に走る更新スレッドは 1 本まで
_refresh_thread: Optional[threading.Thread] = None
_refresh_started_at: Optional[float] = None  # 更新スレッドの開始時刻 (反映要求の待ちに使う)
_child: Optional[subprocess.Popen] = None    # 起動中の子プロセス (終了時に killpg)


# ----------------------------------------------------------- credentials

# 2026-10 の改称: Max 5x -> Max、Max 20x -> Ultra (キーチェーンは旧名のまま)
_MAX_TIER_NAMES = {"5x": "max", "20x": "ultra"}


def _plan_label(subscription: Optional[str], tier: Optional[str]) -> Optional[str]:
    """subscriptionType と rateLimitTier からプラン名を作る。max / ultra は倍率で
    新しい名前に読み替え ("max" + "default_claude_max_20x" -> "ultra")、
    それ以外は倍率を足す。倍率が読めなければそのまま。"""
    m = re.search(r"_(\d+x)$", str(tier or ""))
    if subscription and m:
        if subscription in ("max", "ultra") and m.group(1) in _MAX_TIER_NAMES:
            return _MAX_TIER_NAMES[m.group(1)]
        return "%s %s" % (subscription, m.group(1))
    return subscription


def _read_keychain_payload() -> Tuple[str, Optional[str], Optional[datetime]]:
    """(access_token, subscription_type, expires_at) from the login keychain item.

    expires_at は claudeAiOauth.expiresAt (epoch ミリ秒) から。無ければ None。
    読めた値は _last_expiry にも記録する (fetch の期限前更新で使う)。
    """
    global _last_expiry
    try:
        proc = security.run(
            ["find-generic-password", "-s", "Claude Code-credentials", "-w"],
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise CredentialError("timeout") from None
    except OSError:
        raise CredentialError(t("providers.security_unavailable")) from None
    if proc.returncode != 0:
        raise CredentialError(t("providers.no_credentials"))
    try:
        data = json.loads(proc.stdout)
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken") or data.get("accessToken")
        subscription = _plan_label(
            oauth.get("subscriptionType") or data.get("subscriptionType"),
            oauth.get("rateLimitTier") or data.get("rateLimitTier"))
        expires_at = _from_epoch_millis(oauth.get("expiresAt"))
    except (json.JSONDecodeError, AttributeError):
        raise CredentialError(t("providers.malformed")) from None
    if not token:
        raise CredentialError(t("providers.no_credentials"))
    _last_expiry = expires_at
    return token, subscription, expires_at


def _known_expiry() -> Optional[datetime]:
    """最後にキーチェーンを読んだときの expiresAt (不明なら None)。"""
    return _last_expiry


def read_claude_credentials() -> Tuple[str, Optional[str]]:
    """(access_token, subscription_type) from the login keychain item."""
    token, subscription, _ = _read_keychain_payload()
    return token, subscription


# ------------------------------------------------- refresh via Claude CLI

def set_token_refresh(mode: str) -> None:
    """トークン自動更新の設定を反映する ("cli" = CLI に更新させる / "off" = しない)。

    "off" への切り替えのとき、走っている claude 子プロセスがいれば kill して
    更新スレッドの待ちを解く。
    """
    global _refresh_mode
    if mode not in TOKEN_REFRESH_MODES:
        raise ValueError("token_refresh は %s のいずれか" % (TOKEN_REFRESH_MODES,))
    _refresh_mode = mode
    if mode == "off":
        _kill_running_child()


def _kill_running_child() -> None:
    """起動中の子プロセス (claude -p) がいれば killpg する。"""
    proc = _child
    if proc is not None and proc.poll() is None:
        _kill_process_group(proc)


atexit.register(_kill_running_child)  # 終了時にもみ消し (実行中の claude を残さない)


def _find_claude_cli(search_path: Optional[str] = None) -> Optional[str]:
    """claude 実行ファイルのパス。見つからなければ None。"""
    return process.find_cli("claude", search_path, _CLAUDE_FALLBACKS,
                            which=shutil.which)


def _current_expiry() -> Optional[datetime]:
    """キーチェーンの expiresAt (読めなければ None)。"""
    try:
        return _read_keychain_payload()[2]
    except CredentialError:
        return None


def _refresh_via_cli() -> bool:
    """(ブロッキング本体) `claude -p` を起動し、CLI 自身にトークンを更新させる。

    成功 = exit 0 かつ、キーチェーンを読み直した expiresAt が起動前より進んでいる。
    クールダウンは「起動できたとき」だけ記録する (見つからない・起動失敗は記録
    しない。ただし「見つからない」警告は 30 分に 1 回)。30 分のところ、exit 0 でも
    expiresAt が進まなかったときだけ 5 分にする (CLI は期限前にはまだ更新しない
    ので、期限切れ後すぐ再試行できるように)。
    """
    global _cooldown_until, _child, _last_not_found
    now = time.monotonic()
    if _cooldown_until is not None and now < _cooldown_until:
        log.debug("claude CLI でのトークン更新はクールダウン中")
        return False
    env = process.limited_env()
    claude_cli = _find_claude_cli(env["PATH"])
    if not claude_cli:
        if _last_not_found is None or now - _last_not_found >= CLI_REFRESH_COOLDOWN:
            log.warning("claude CLI が見つからないためトークンを自動更新できない")
            _last_not_found = now
        return False
    if _refresh_mode != "cli":  # 待っているあいだに off / 未設定になった
        return False
    bindir = os.path.dirname(claude_cli)
    if bindir and bindir not in env["PATH"].split(os.pathsep):
        env["PATH"] = env["PATH"] + os.pathsep + bindir  # 見つけたバイナリの仲間も使えるように
    before = _current_expiry()
    try:
        project_dir = process.claude_refresh_dir()
    except OSError:
        log.warning("claude CLI を起動できなかった")  # 起動失敗はクールダウンに入れない
        return False
    try:
        try:
            proc = process.start(subprocess.Popen,
                [claude_cli, "-p", "ok", "--model", "haiku", "--max-turns", "1",
                 "--no-session-persistence", "--setting-sources", "project",
                 "--strict-mcp-config", "--disable-slash-commands"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, cwd=project_dir,
                env=env,
            )
        except OSError:
            log.warning("claude CLI を起動できなかった")  # 起動失敗はクールダウンに入れない
            return False
        _child = proc
        _cooldown_until = now + CLI_REFRESH_COOLDOWN
        try:
            returncode = proc.wait(timeout=CLI_REFRESH_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            _child = None
            log.warning("claude CLI が %s 秒で終わらないため強制終了した", CLI_REFRESH_TIMEOUT)
            return False
        _child = None
        if returncode != 0:
            log.warning("claude CLI によるトークン更新に失敗した (exit %s)", returncode)
            return False
        after = _current_expiry()
        if after is None or (before is not None and after <= before):
            # exit 0 でも更新されていない: 期限が切れたあとすぐ再試行できるよう 5 分だけ空ける
            _cooldown_until = now + CLI_RETRY_SOON
            log.warning("claude CLI は終了したがキーチェーンのトークンは更新されなかった")
            return False
        log.info("claude CLI を実行して OAuth トークンを更新した")
        return True
    finally:
        rmtree(project_dir, ignore_errors=True)


def _wait_for_refresh_gap(min_gap: float) -> None:
    """run ループの反映要求チェック (REFRESH_MIN_GAP) で捨てられないよう、更新
    スレッドの開始から min_gap + 1 秒経つまで待ってから request_refresh する。
    (time.sleep はテストで差し替え可能なように claude.time 経由で呼ぶ)"""
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
        log.warning("claude CLI によるトークン更新でエラー (%s)", type(exc).__name__)
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
                                  name="claude-token-refresh", daemon=True)
        _refresh_thread = thread
        thread.start()
        return True


def _expires_soon(expires_at: Optional[datetime], now: datetime) -> bool:
    """expiresAt が分かっていて now + 5 分より前なら True。"""
    if expires_at is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return expires_at <= now + EXPIRY_MARGIN


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """Claude Code のログイン情報があるか。通信もトークンの読み出し内容も使わない。"""
    try:
        read_claude_credentials()
    except CredentialError as exc:
        return Detection(False, None, str(exc) or t("providers.no_credentials"))
    return Detection(True, t("providers.detect.claude"))


# --------------------------------------------------------------- parser

def _clean_display_name(value: Any) -> str:
    """表示名から制御文字・書式文字など Unicode の C カテゴリを除く。"""
    return "".join(ch for ch in str(value)
                   if not unicodedata.category(ch).startswith("C")).strip()


def parse_claude(data: Dict[str, Any], now: Optional[datetime] = None,
                 subscription_type: Optional[str] = None) -> Tuple[str, List[Window]]:
    """(name, windows) from the Anthropic OAuth usage response."""
    windows: List[Window] = []
    for entry, key, label, length in (
        (data.get("five_hour"), "5h", t("providers.window.five_hours"), timedelta(hours=5)),
        (data.get("seven_day"), "weekly", t("providers.window.weekly"), timedelta(days=7)),
    ):
        if not isinstance(entry, dict) or entry.get("utilization") is None:
            continue
        windows.append(Window(
            "claude", key, label, float(entry["utilization"]),
            _parse_iso(entry.get("resets_at")), length,
        ))
    for limit in data.get("limits") or []:
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue  # also skips null entries
        scope = limit.get("scope") or {}
        name = (scope.get("model") or {}).get("display_name")
        used = limit.get("percent")
        if not name or used is None:
            continue
        name = _clean_display_name(name)
        if not name:
            continue
        windows.append(Window(
            "claude", "weekly_" + name.lower(),
            t("providers.claude.weekly_scoped", model=name),
            float(used), _parse_iso(limit.get("resets_at")), timedelta(days=7),
        ))
    suffix = str(subscription_type or "").strip().capitalize()
    return ("Claude " + suffix).strip(), windows


# ---------------------------------------------------------------- fetch

def _get_usage(token: str, subscription: Optional[str], now: datetime) -> ProviderUsage:
    data = http_get_json(CLAUDE_USAGE_URL, {
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": USER_AGENT,
    })
    name, windows = parse_claude(data, now, subscription)
    return ProviderUsage("claude", name, windows, None, now)


def fetch(now: datetime) -> ProviderUsage:
    token, subscription = read_claude_credentials()
    if _refresh_mode == "cli" and _expires_soon(_known_expiry(), now):
        # 期限切れ間近: バックグラウンドで更新を開始し、今あるトークンでそのまま取得
        _spawn_refresh_thread()
    try:
        return _get_usage(token, subscription, now)
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        if _refresh_mode != "cli":  # "off" と未設定 (run 以外) は CLI を起動しない
            raise CredentialError(t("providers.token_expired_run_claude")) from None
        _spawn_refresh_thread()  # 更新はバックグラウンドで。成功したら取り直される
        raise CredentialError(t("providers.token_expired_refreshing")) from None


PROVIDER = Provider(
    id="claude", name="Claude", needs_key=False, detect=detect, fetch=fetch,
)
