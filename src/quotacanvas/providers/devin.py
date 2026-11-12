"""Devin (Cognition。旧 Windsurf と同じ利用枠): 非公開の Connect-JSON RPC で残量を取得する。

POST {server}/exa.seat_management_pb.SeatManagementService/GetUserStatus に
API キーを載せ、日次 (1 日) と週次の残り % を窓にする。残り % は proto3 が
ゼロを省略するので、残り % が無くリセット時刻だけがあるときは使用率 100% と
みなす。overageBalanceMicros があれば追加利用残高 (USD) として表示する。

キーの探索順: キーチェーン quota-canvas/devin → Devin CLI の
~/.local/share/devin/credentials.toml の windsurf_api_key → Devin デスクトップ
アプリの state.vscdb の windsurfAuthStatus の apiKey。環境変数は使わない。

無料モデル (SWE-2 など) は Pro の枠を使わず API にも出ないが、無料枠の利用制限
("Reached free model rate limit ... reset in 18 hours 40 minutes") に当たると
戻る時刻まで使えない。CLI を呼ぶラッパーがその戻る時刻 (epoch 秒) を
~/.claude/run/devin-ratelimit に書いていれば、その時刻までのあいだだけ
"devin:free" の窓 (使用 100%・リセット = 戻る時刻) を足す。routing の
ワーカーをこの窓に結べば、制限中は stop になる。場所は環境変数
QUOTA_CANVAS_DEVIN_FREE_LIMIT_FILE で差し替えられる。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tomllib
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .. import keychain
from ..i18n import t
from ..models import Balance, ProviderUsage, Window
from ._base import (
    USER_AGENT, CredentialError, Detection, Provider,
    _from_epoch, _window_identity, finite_number, http_post_json,
)

DEVIN_SERVER = "https://server.codeium.com"
DEVIN_STATUS_PATH = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"

# Devin (Windsurf) のクライアント版。ideVersion / extensionVersion が無いと
# サーバーが 400 を返すため、デスクトップアプリと同じ値を名乗る。
DEVIN_CLIENT_VERSION = "3.11.238"

# Devin CLI が保存する認証情報。無い・壊れているときは次の探索へ進む。
CLI_CREDENTIALS_PATH = Path.home() / ".local" / "share" / "devin" / "credentials.toml"
CLI_API_KEY_FIELD = "windsurf_api_key"
CLI_SERVER_FIELD = "api_server_url"

# Devin デスクトップアプリの認証情報 (SQLite の ItemTable)。
DESKTOP_STATE_DB = (
    Path.home() / "Library" / "Application Support" / "Devin" / "User"
    / "globalStorage" / "state.vscdb"
)
DESKTOP_AUTH_KEY = "windsurfAuthStatus"

# 無料モデルの利用制限の戻る時刻 (epoch 秒の 1 行) を CLI のラッパーが書くファイル。
FREE_LIMIT_FILE = Path.home() / ".claude" / "run" / "devin-ratelimit"
FREE_LIMIT_ENV = "QUOTA_CANVAS_DEVIN_FREE_LIMIT_FILE"
FREE_WINDOW_KEY = "free"

# ----------------------------------------------------------- credentials

def _credentials_file() -> Path:
    """Devin CLI の credentials.toml。テストで差し替えられるよう関数にしている。"""
    return CLI_CREDENTIALS_PATH


def _desktop_state_db() -> Path:
    """Devin デスクトップアプリの state.vscdb。テストで差し替えられるよう関数にしている。"""
    return DESKTOP_STATE_DB


def _cli_server(value: Any) -> str:
    """credentials.toml の api_server_url を検証する。

    https で、ホストが codeium.com / windsurf.com / devin.ai 自身またはその
    サブドメインのときだけ採用し、"https://" + ホスト[:ポート] (認証情報・パス・クエリは捨てる)
    を返す。それ以外は既定サーバー。"""
    if not isinstance(value, str):
        return DEVIN_SERVER
    try:
        parts = urllib.parse.urlsplit(value.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:  # 壊れたポートなど
        return DEVIN_SERVER
    if parts.scheme != "https" or not host:
        return DEVIN_SERVER
    for domain in ("codeium.com", "windsurf.com", "devin.ai"):
        if host == domain or host.endswith("." + domain):
            return "https://" + host + (":%d" % port if port else "")
    return DEVIN_SERVER


def _key_from_credentials_file() -> Optional[Tuple[str, str]]:
    """(API key, server) from credentials.toml. 読めなければ None。"""
    try:
        raw = _credentials_file().read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    key = data.get(CLI_API_KEY_FIELD)
    if not isinstance(key, str) or not key.strip():
        return None
    return key.strip(), _cli_server(data.get(CLI_SERVER_FIELD))


def _key_from_desktop_db() -> Optional[str]:
    """windsurfAuthStatus の apiKey。DB が無い・壊れているときは None。"""
    path = _desktop_state_db()
    try:
        uri = "file:%s?mode=ro" % urllib.parse.quote(path.absolute().as_posix())
        connection = sqlite3.connect(uri, uri=True, timeout=0.2)
    except (OSError, sqlite3.Error):
        return None
    try:
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (DESKTOP_AUTH_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not row:
        return None
    value = row[0]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        payload = json.loads(value)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    key = payload.get("apiKey")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def locate_devin_key() -> Tuple[str, str, str]:
    """(API key, server, 取得元の説明)。どこにも無ければ CredentialError。"""
    stored = keychain.read("devin")
    if stored:
        return stored, DEVIN_SERVER, t("providers.detect.keychain", provider="devin")
    cli = _key_from_credentials_file()
    if cli:
        key, server = cli
        return key, server, t("providers.detect.devin_cli")
    key = _key_from_desktop_db()
    if key:
        return key, DEVIN_SERVER, t("providers.detect.devin_desktop")
    raise CredentialError(t("providers.api_key_not_set"))


def read_devin_api_key() -> str:
    """Devin API key. キーの探索順はモジュール docstring 参照。"""
    return locate_devin_key()[0]


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """API キーがあるか。通信しない。"""
    try:
        _, _, source = locate_devin_key()
    except CredentialError as exc:
        return Detection(False, None, str(exc) or t("providers.api_key_not_set"))
    return Detection(True, t("providers.detect.api_key", name="Devin", source=source))


# ------------------------------------------------------ free-model limit

def _free_limit_file() -> Path:
    """無料モデルの利用制限の戻る時刻のファイル。環境変数で差し替えられる。"""
    override = os.environ.get(FREE_LIMIT_ENV)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return FREE_LIMIT_FILE


def free_limit_window(now: datetime) -> Optional[Window]:
    """無料枠の利用制限中なら "devin:free" の窓 (使用 100%) を返す。

    ファイルが無い・読めない・数字でない・戻る時刻を過ぎているときは None
    (制限中ではない = 窓を出さない)。窓の長さは今から戻る時刻まで
    (最低 1 分)。"""
    try:
        raw = _free_limit_file().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw.isdigit():
        return None
    reset_at = _from_epoch(int(raw))
    if reset_at is None or reset_at <= now:
        return None
    length = max(reset_at - now, timedelta(minutes=1))
    return Window("devin", FREE_WINDOW_KEY, t("providers.window.free_limit"),
                  100.0, reset_at, length)


def with_free_limit(usage: ProviderUsage, now: datetime) -> ProviderUsage:
    """Devin の usage の devin:free を今のファイルから作り直したものにする。

    Devin 以外・窓が 1 つも無い usage (取得失敗で前回値も無い = エラーカード) は
    そのまま返す。"""
    if usage.provider != "devin" or not usage.windows:
        return usage
    windows = [w for w in usage.windows if w.key != FREE_WINDOW_KEY]
    free = free_limit_window(now)
    if free is not None:
        windows.append(free)
    if windows == list(usage.windows):
        return usage
    return replace(usage, windows=windows)


# --------------------------------------------------------------- parser

def _remaining_percent(value: Any) -> float:
    """残り % を 0〜100 に丸める。bool・非数値・非有限は ValueError。

    同じ API の int64 系フィールド (リセット時刻・残高) は JSON で数字の文字列に
    なるので、残り % も "42" のような数字の文字列なら数値として受け付ける。"""
    try:
        percent = finite_number(value)
    except (TypeError, ValueError):
        raise ValueError("invalid percent") from None
    return min(100.0, max(0.0, percent))


# (残り % のキー, リセット時刻のキー, 窓の長さ秒, hide フラグのキー)
_DEVIN_WINDOWS = (
    ("dailyQuotaRemainingPercent", "dailyQuotaResetAtUnix", 86400, "hideDailyQuota"),
    ("weeklyQuotaRemainingPercent", "weeklyQuotaResetAtUnix", 7 * 86400, "hideWeeklyQuota"),
)


def parse_devin(data: Any, now: Optional[datetime] = None) \
        -> Tuple[str, List[Window], List[Balance]]:
    """(name, windows, balances) from the Devin GetUserStatus response.

    proto3 はゼロを省略する: 残り % が無くリセット時刻だけがある窓は
    使用率 100% とする。どちらも無ければその窓は出さない。残り % が null の
    ときも「キー無し」と同じ扱い。overageBalanceMicros が数値化できないときは
    残高だけ落とし、窓が無くても残高があれば windows=[] で返す。"""
    if not isinstance(data, dict):
        raise ValueError("invalid response")
    user_status = data.get("userStatus")
    if not isinstance(user_status, dict):
        raise ValueError("invalid response")
    plan_status = user_status.get("planStatus")
    if not isinstance(plan_status, dict):
        raise ValueError("invalid response")
    plan_info = plan_status.get("planInfo")
    if not isinstance(plan_info, dict):
        plan_info = {}

    windows: List[Window] = []
    for remaining_key, reset_key, seconds, hide_key in _DEVIN_WINDOWS:
        if plan_info.get(hide_key) is True:
            continue
        remaining_raw = plan_status.get(remaining_key)
        reset_at = _from_epoch(plan_status.get(reset_key))
        has_remaining = remaining_raw is not None
        has_reset = reset_at is not None
        if not has_remaining and not has_reset:
            continue
        if has_remaining:
            used = 100.0 - _remaining_percent(remaining_raw)
        else:
            used = 100.0  # proto3 が省略したゼロ = 残り 0%
        key, label, length = _window_identity(seconds)
        windows.append(Window("devin", key, label, used, reset_at, length))

    balances: List[Balance] = []
    if "overageBalanceMicros" in plan_status:
        try:
            amount = finite_number(plan_status["overageBalanceMicros"]) / 1_000_000
        except ValueError:
            pass  # 残高だけ落とす (窓はそのまま返す)
        else:
            balances.append(Balance(
                "USD", amount, t("providers.extra_usage_balance"),
            ))
    if not windows and not balances:
        raise ValueError("no quota")

    plan_name = plan_info.get("planName")
    if isinstance(plan_name, str) and plan_name.strip():
        name = "Devin " + plan_name.strip()
    else:
        name = "Devin"
    return name, windows, balances


# ---------------------------------------------------------------- fetch

def fetch(now: datetime) -> ProviderUsage:
    key, server, _source = locate_devin_key()
    url = server.rstrip("/") + DEVIN_STATUS_PATH
    body = {"metadata": {
        "apiKey": key,
        "ideName": "windsurf",
        "ideVersion": DEVIN_CLIENT_VERSION,
        "extensionName": "windsurf",
        "extensionVersion": DEVIN_CLIENT_VERSION,
        "locale": "en",
    }}
    data = http_post_json(url, {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": USER_AGENT,
    }, body)
    name, windows, balances = parse_devin(data, now)
    free = free_limit_window(now)
    if free is not None:
        windows.append(free)
    return ProviderUsage("devin", name, windows, None, now, balances=balances)


PROVIDER = Provider(
    id="devin", name="Devin", needs_key=True, detect=detect, fetch=fetch,
    display_name="Devin",
)
