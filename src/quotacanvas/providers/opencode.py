"""OpenCode Go: API キーで 5 時間・週間・月間の利用枠を取得する。

キーの探索順: キーチェーン quota-canvas/opencode → 環境変数 OPENCODE_API_KEY
→ opencode CLI の ~/.local/share/opencode/auth.json の "opencode-go" エントリの "key"。

無料枠の利用制限の戻る時刻 (epoch 秒) を opencode-run が
~/.claude/run/opencode-free-exhausted に書いていれば、その時刻までのあいだ
"opencode:free" の窓 (使用 100%・リセット = 戻る時刻) を足す。制限が無いときも
使用 0%・リセットなしの窓を出し、routing のワーカーが unknown/missing に
ならないようにする (制限中は stop)。場所は環境変数
QUOTA_CANVAS_OPENCODE_FREE_EXHAUSTED_FILE で差し替えられる。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import keychain
from ..i18n import t
from ..models import ProviderUsage, Window
from ._base import (
    USER_AGENT, CredentialError, Detection, Provider,
    _from_epoch, _parse_iso, _window_identity, http_get_json,
)

OPENCODE_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"

# opencode CLI が保存する認証情報。このファイルが無い・壊れている・
# "opencode-go" エントリが無いときは「キー無し」として次の探索へ進む。
OPENCODE_AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"
OPENCODE_AUTH_ENTRY = "opencode-go"

# 無料枠の利用制限の戻る時刻 (epoch 秒の 1 行) を opencode-run が書くファイル。
FREE_LIMIT_FILE = Path.home() / ".claude" / "run" / "opencode-free-exhausted"
FREE_LIMIT_ENV = "QUOTA_CANVAS_OPENCODE_FREE_EXHAUSTED_FILE"
FREE_WINDOW_KEY = "free"
# 制限が無いときの窓の長さ。リセット時刻が無いので判定には使われない。
FREE_WINDOW_LENGTH = timedelta(hours=5)

# ----------------------------------------------------------- credentials

def _auth_file() -> Path:
    """opencode CLI の auth.json。テストで差し替えられるよう関数にしている。"""
    return OPENCODE_AUTH_PATH


def _key_from_auth_file() -> Optional[str]:
    """auth.json の "opencode-go" エントリの key。読めなければ None。"""
    try:
        data = json.loads(_auth_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(OPENCODE_AUTH_ENTRY) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def locate_opencode_key() -> Tuple[str, str]:
    """(API key, 取得元の説明)。どこにも無ければ CredentialError。"""
    stored = keychain.read("opencode")
    if stored:
        return stored, t("providers.detect.keychain", provider="opencode")
    env = os.environ.get("OPENCODE_API_KEY")
    if env and env.strip():
        return env.strip(), t("providers.detect.env", variable="OPENCODE_API_KEY")
    key = _key_from_auth_file()
    if key:
        return key, t("providers.detect.opencode_auth")
    raise CredentialError(t("providers.api_key_not_set"))


def read_opencode_api_key() -> str:
    """OpenCode Go API key. キーの探索順はモジュール docstring 参照。"""
    return locate_opencode_key()[0]


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """API キーがあるか。通信しない。"""
    try:
        _, source = locate_opencode_key()
    except CredentialError as exc:
        return Detection(False, None, str(exc) or t("providers.api_key_not_set"))
    return Detection(True, t("providers.detect.api_key", name="OpenCode Go", source=source))


# ------------------------------------------------------ free-model limit

def _free_limit_file() -> Path:
    """無料枠の利用制限の戻る時刻のファイル。環境変数で差し替えられる。"""
    override = os.environ.get(FREE_LIMIT_ENV)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return FREE_LIMIT_FILE


def free_limit_window(now: datetime) -> Window:
    """opencode:free の窓を返す。

    ファイルの戻る時刻が未来なら使用 100%・リセット = その時刻 (制限中)。
    ファイルが無い・読めない・数字でない・戻る時刻を過ぎているときは
    使用 0%・リセットなし (制限中ではない)。routing のワーカーがこの窓だけに
    結ばれていても、制限が無ければ通常の go/boost 判定になる。"""
    reset_at: Optional[datetime] = None
    try:
        raw = _free_limit_file().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raw = ""
    if raw.isdigit():
        # isdigit は "²" のような非 ASCII 数字や 4300 桁超の値も真にするので、
        # int() の失敗は壊れたファイルとして扱う。
        try:
            reset_at = _from_epoch(int(raw))
        except ValueError:
            reset_at = None
    if reset_at is not None and reset_at > now:
        length = max(reset_at - now, timedelta(minutes=1))
        return Window("opencode", FREE_WINDOW_KEY, t("providers.window.free_limit"),
                      100.0, reset_at, length)
    return Window("opencode", FREE_WINDOW_KEY, t("providers.window.free_limit"),
                  0.0, None, FREE_WINDOW_LENGTH)


def with_free_limit(usage: ProviderUsage, now: datetime) -> ProviderUsage:
    """OpenCode の usage の opencode:free を今のファイルから作り直したものにする。

    OpenCode 以外・窓が 1 つも無い usage (取得失敗で前回値も無い = エラーカード) は
    そのまま返す。"""
    if usage.provider != "opencode" or not usage.windows:
        return usage
    windows = [w for w in usage.windows if w.key != FREE_WINDOW_KEY]
    windows.append(free_limit_window(now))
    if windows == list(usage.windows):
        return usage
    return replace(usage, windows=windows)


# --------------------------------------------------------------- parser

# 応答の meter 名 → 窓の長さ (秒)。rolling は 5 時間、weekly は 7 日、
# monthly は 30 日として扱う。
_OPENCODE_WINDOWS = (
    ("rolling", 5 * 3600),
    ("weekly", 7 * 86400),
    ("monthly", 30 * 86400),
)


def parse_opencode(data: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[str, List[Window]]:
    """(name, windows) from the OpenCode Go usage response.

    status が "ok" 以外の文字列 (rate_limited など) の meter は使用率 100% と
    みなす。status が無い・null なら percent をそのまま使う。"""
    if not isinstance(data, dict):
        raise ValueError("invalid response")
    payload = data.get("usage")
    if payload is None:
        raise ValueError("no usage meters")
    if not isinstance(payload, dict):
        raise ValueError("invalid usage")
    windows: List[Window] = []
    for meter, seconds in _OPENCODE_WINDOWS:
        item = payload.get(meter)
        if item is None:
            continue  # その meter が無い応答では、その窓を出さない
        if not isinstance(item, dict):
            raise ValueError("invalid meter")
        percent = item.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise ValueError("invalid percent")
        value = float(percent)
        if not math.isfinite(value):
            raise ValueError("invalid percent")
        status = item.get("status")
        if isinstance(status, str) and status.strip().lower() != "ok":
            value = 100.0
        value = min(100.0, max(0.0, value))
        key, label, length = _window_identity(seconds)
        windows.append(Window(
            "opencode", key, label, value,
            _parse_iso(item.get("resetsAt")), length,
        ))
    if not windows:
        raise ValueError("no usage meters")
    return "OpenCode Go", windows


# ---------------------------------------------------------------- fetch

def fetch(now: datetime) -> ProviderUsage:
    key = read_opencode_api_key()
    data = http_get_json(OPENCODE_USAGE_URL, {
        "Authorization": "Bearer " + key,
        "User-Agent": USER_AGENT,
    })
    name, windows = parse_opencode(data, now)
    windows.append(free_limit_window(now))
    return ProviderUsage("opencode", name, windows, None, now)


PROVIDER = Provider(
    id="opencode", name="OpenCode", needs_key=True, detect=detect, fetch=fetch,
    display_name="OpenCode Go",
)
