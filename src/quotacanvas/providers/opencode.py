"""OpenCode Go: API キーで 5 時間・週間・月間の利用枠を取得する。

キーの探索順: キーチェーン quota-canvas/opencode → 環境変数 OPENCODE_API_KEY
→ opencode CLI の ~/.local/share/opencode/auth.json の "opencode-go" エントリの "key"。
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import keychain
from ..i18n import t
from ..models import ProviderUsage, Window
from ._base import (
    USER_AGENT, CredentialError, Detection, Provider,
    _parse_iso, _window_identity, http_get_json,
)

OPENCODE_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"

# opencode CLI が保存する認証情報。このファイルが無い・壊れている・
# "opencode-go" エントリが無いときは「キー無し」として次の探索へ進む。
OPENCODE_AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"
OPENCODE_AUTH_ENTRY = "opencode-go"

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
    return ProviderUsage("opencode", name, windows, None, now)


PROVIDER = Provider(
    id="opencode", name="OpenCode", needs_key=True, detect=detect, fetch=fetch,
    display_name="OpenCode Go",
)
