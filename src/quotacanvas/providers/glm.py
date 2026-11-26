"""GLM (Z.ai): API キーで利用枠を取得する。

キーの探索順: キーチェーン quota-canvas/glm → 環境変数 ZAI_API_KEY

リセット権 (Z.ai の Reset Card: 5 時間枠 / 週間枠を即座に満タンへ戻す権利) も
同じキーで一覧だけ読む。使う (消費する) 要求は一切送らない。
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .. import keychain
from ..i18n import t
from ..models import ProviderUsage, ResetCredit, Window
from ._base import (
    USER_AGENT, CredentialError, Detection, Provider,
    _from_epoch_millis, _short_reason, _window_identity, finite_number, http_get_json,
)

GLM_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
# z.ai の Coding Plan 使用量画面 (Reset Quota) が読む一覧。個人プランは PERSONAL。
GLM_RESET_CREDITS_URL = ("https://api.z.ai/api/biz/customer-package-reset/list"
                         "?targetType=PERSONAL")
RESET_CREDITS_TTL = timedelta(minutes=15)
# 応答の日時 ("2026-12-22 23:59:59") はタイムゾーン無しの中国標準時 (UTC+8)。
# 使用量 API の nextResetTime (epoch) と lastFiveHourResetTime の突き合わせで確認。
_GLM_TZ = timezone(timedelta(hours=8))

log = logging.getLogger("quotacanvas.glm")

# ----------------------------------------------------------- credentials

def locate_glm_key() -> Tuple[str, str]:
    """(API key, 取得元の説明)。どこにも無ければ CredentialError。"""
    stored = keychain.read("glm")
    if stored:
        return stored, t("providers.detect.glm_keychain")
    env = os.environ.get("ZAI_API_KEY")
    if env and env.strip():
        return env.strip(), t("providers.detect.glm_env")
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
            "glm", key, label, finite_number(used),
            _from_epoch_millis(limit.get("nextResetTime")), length,
        ))
    level = str(payload.get("level") or "").strip().capitalize()
    return ("GLM " + level).strip(), windows


# -------------------------------------------------------- reset credits

_RESET_KINDS = (("fiveHourResets", "glm_reset_5h"), ("weekResets", "glm_reset_week"))


def _parse_glm_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_GLM_TZ)
    except (TypeError, ValueError):
        return None


def parse_glm_reset_credits(data: Dict[str, Any], now: datetime) -> List[ResetCredit]:
    """customer-package-reset/list の応答 -> 使えるリセット権 (期限の早い順)。

    available が明示的に False のもの・期限切れ (expireTime <= now) は除く。
    title は種類 (5 時間枠 / 週間枠) を表す。
    """
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return []
    credits: List[ResetCredit] = []
    for field_name, title_key in _RESET_KINDS:
        for entry in payload.get(field_name) or []:
            if not isinstance(entry, dict) or entry.get("available") is False:
                continue
            expires_at = _parse_glm_time(entry.get("expireTime"))
            if expires_at is not None and expires_at <= now:
                continue
            credits.append(ResetCredit(t("providers." + title_key), None, expires_at))
    far = datetime.max.replace(tzinfo=timezone.utc)
    credits.sort(key=lambda c: c.expires_at or far)
    return credits


_RESET_CACHE: Dict[str, Any] = {"at": None, "credits": [], "key_id": None}


def _unexpired(credits: List[ResetCredit], now: datetime) -> List[ResetCredit]:
    return [c for c in credits if c.expires_at is None or c.expires_at > now]


def fetch_reset_credits(now: datetime, key: str) -> List[ResetCredit]:
    """リセット権の一覧を GET する (15 分に 1 回まで)。失敗しても例外は出さず、
    同じキーの前回結果 (無ければ空) を返す。キーそのものは保持・ログ出力しない。"""
    key_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
    same_key = _RESET_CACHE.get("key_id") == key_id
    cached_at = _RESET_CACHE["at"]
    if same_key and cached_at is not None and now - cached_at < RESET_CREDITS_TTL:
        return _unexpired(list(_RESET_CACHE["credits"]), now)
    try:
        data = http_get_json(GLM_RESET_CREDITS_URL, _headers(key))
        if (not isinstance(data, dict) or data.get("success") is False
                or data.get("code", 200) != 200):
            raise ValueError("code=%s" % (data.get("code") if isinstance(data, dict) else "?"))
        credits = parse_glm_reset_credits(data, now)
    except Exception as exc:  # noqa: BLE001 - 利用量の取得を壊さない
        log.warning("GLM のリセット権一覧を取得できません: %s", _short_reason(exc))
        return _unexpired(list(_RESET_CACHE["credits"]), now) if same_key else []
    _RESET_CACHE.update(at=now, credits=credits, key_id=key_id)
    return _unexpired(list(credits), now)


# ---------------------------------------------------------------- fetch

def _headers(key: str) -> Dict[str, str]:
    return {
        "Authorization": key,  # Bearer なし
        "Accept-Language": "en-US,en",
        "User-Agent": USER_AGENT,
    }


def fetch(now: datetime) -> ProviderUsage:
    key = read_glm_api_key()
    data = http_get_json(GLM_USAGE_URL, _headers(key))
    name, windows = parse_glm(data, now)
    usage = ProviderUsage("glm", name, windows, None, now)
    # リセット権は付随情報。失敗しても利用量には影響しない (内部で警告だけ)。
    usage.reset_credits = fetch_reset_credits(now, key)
    return usage


PROVIDER = Provider(
    id="glm", name="GLM", needs_key=True, detect=detect, fetch=fetch,
    display_name="GLM (Z.ai)",
)