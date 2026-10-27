"""GLM (Z.ai): API キーで利用枠を取得する。

キーの探索順: キーチェーン quota-canvas/glm → 環境変数 ZAI_API_KEY
→ ~/.config/glm-run/zai_api_key → ~/.dsh/.credentials.yaml
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import keychain
from ..i18n import t
from ..models import ProviderUsage, Window
from ._base import (
    USER_AGENT, CredentialError, Detection, Provider,
    _from_epoch_millis, _window_identity, http_get_json,
)

GLM_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"

_ZAI_KEY_LINE = re.compile(r"^\s*ZAI_API_KEY:\s*(.+?)\s*$", re.MULTILINE)


# ----------------------------------------------------------- credentials

def locate_glm_key() -> Tuple[str, str]:
    """(API key, 取得元の説明)。どこにも無ければ CredentialError。"""
    stored = keychain.read("glm")
    if stored:
        return stored, t("providers.detect.glm_keychain")
    env = os.environ.get("ZAI_API_KEY")
    if env and env.strip():
        return env.strip(), t("providers.detect.glm_env")
    candidate = Path.home() / ".config" / "glm-run" / "zai_api_key"
    try:
        text = candidate.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text, "~/.config/glm-run/zai_api_key"
    try:
        match = _ZAI_KEY_LINE.search(
            (Path.home() / ".dsh" / ".credentials.yaml").read_text(encoding="utf-8")
        )
    except OSError:
        match = None
    if match:
        return match.group(1).strip("'\""), "~/.dsh/.credentials.yaml"
    raise CredentialError(t("providers.api_key_not_set"))


def read_glm_api_key() -> str:
    """Z.ai API key. キーの探索順はモジュール docstring 参照。"""
    return locate_glm_key()[0]


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """API キーがあるか。通信しない。"""
    try:
        _, source = locate_glm_key()
    except CredentialError as exc:
        return Detection(False, None, str(exc) or t("providers.api_key_not_set"))
    return Detection(True, t("providers.detect.glm", source=source))


# --------------------------------------------------------------- parser

_GLM_UNIT_SECONDS = {3: 3600, 1: 86400, 5: 30 * 86400}  # hour / day / month(30d)


def parse_glm(data: Dict[str, Any], now: Optional[datetime] = None) -> Tuple[str, List[Window]]:
    """(name, windows) from the Z.ai monitor usage response."""
    payload = data.get("data") or {}
    windows: List[Window] = []
    for limit in payload.get("limits") or []:
        if not isinstance(limit, dict) or limit.get("type") != "TOKENS_LIMIT":
            continue  # TIME_LIMIT (MCP) などは無視
        used = limit.get("percentage")
        unit_seconds = _GLM_UNIT_SECONDS.get(limit.get("unit"))
        number = limit.get("number")
        if used is None or unit_seconds is None or not number:
            continue
        key, label, length = _window_identity(int(unit_seconds * number))
        windows.append(Window(
            "glm", key, label, float(used),
            _from_epoch_millis(limit.get("nextResetTime")), length,
        ))
    level = str(payload.get("level") or "").strip().capitalize()
    return ("GLM " + level).strip(), windows


# ---------------------------------------------------------------- fetch

def fetch(now: datetime) -> ProviderUsage:
    key = read_glm_api_key()
    data = http_get_json(GLM_USAGE_URL, {
        "Authorization": key,  # Bearer なし
        "Accept-Language": "en-US,en",
        "User-Agent": USER_AGENT,
    })
    name, windows = parse_glm(data, now)
    return ProviderUsage("glm", name, windows, None, now)


PROVIDER = Provider(
    id="glm", name="GLM", needs_key=True, detect=detect, fetch=fetch,
    display_name="GLM (Z.ai)",
)