"""Command Code: API キーで 5 時間・週間・月間 (クレジットのプール) の利用枠を取得する。

キーの探索順: キーチェーン quota-canvas/commandcode → 環境変数 COMMANDCODE_API_KEY
→ 公式 CLI (cmd login) の ~/.commandcode/auth.json の "apiKey"。

取得は公式 CLI の /usage と同じ非公開 API (パスに alpha が付く。変わりうる):
  GET https://api.commandcode.ai/alpha/whoami?limits=1            (org.id。個人は null)
  GET https://api.commandcode.ai/alpha/billing/credits[?orgId=]   (残クレジットと窓の上限)
  GET https://api.commandcode.ai/alpha/billing/subscriptions[?orgId=]  (プランと更新日)
credits の windowLimits.fiveHour / weekly は {used, cap, resetAt} (金額のクレジット)。
resetAt は窓がまだ開いていない (最初のリクエストで開く) ときは 0。
月間は独立した上限ではなく、プランの月間クレジットを課金サイクルで使い切るプールで、
使用率 = (プール - 残り) / プール。プールはプランの月間クレジットと追加分から決める。
whoami と subscriptions は補助なので、失敗しても窓の取得は続ける (月間の窓だけ出ない)。
"""

from __future__ import annotations

import http.client
import json
import math
import os
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import keychain
from ..i18n import t
from ..models import Balance, ProviderUsage, Window
from ._base import (
    TIMEOUT, USER_AGENT, CredentialError, Detection, Provider,
    _from_epoch, _from_epoch_millis, _parse_iso, _window_identity,
    finite_number, http_get_json,
)

COMMANDCODE_API_URL = "https://api.commandcode.ai"
WHOAMI_PATH = "/alpha/whoami"
CREDITS_PATH = "/alpha/billing/credits"
SUBSCRIPTIONS_PATH = "/alpha/billing/subscriptions"

# 公式 CLI (cmd login) が保存する認証情報。無い・壊れている・apiKey が無いときは
# 「キー無し」として次の探索へ進む。
COMMANDCODE_AUTH_PATH = Path.home() / ".commandcode" / "auth.json"

# プラン ID → 月間クレジット (USD)。公式 CLI の表に合わせる。接頭辞が長い順に
# 照合する ("individual-pro-v1" は "individual-pro" より先)。未知のプランは
# 月間の窓を出さない。
_PLAN_CREDITS = {
    "individual-go": 10,
    "individual-go-v1": 10,
    "individual-goat": 70,
    "individual-pro": 30,
    "individual-pro-v1": 80,
    "individual-provider": 15,
    "individual-max": 150,
    "individual-ultra": 300,
    "teams-pro": 40,
}
_PLAN_NAMES = {
    "individual-go": "Go",
    "individual-go-v1": "Go",
    "individual-goat": "GOAT",
    "individual-pro": "Pro",
    "individual-pro-v1": "Pro",
    "individual-provider": "Provider",
    "individual-max": "Max",
    "individual-ultra": "Ultra",
    "teams-pro": "Teams Pro",
}
_PLAN_PREFIXES = sorted(_PLAN_CREDITS, key=len, reverse=True)

_FIVE_HOURS = 5 * 3600
_WEEK = 7 * 86400
_MONTH = 30 * 86400


# ----------------------------------------------------------- credentials

def _auth_file() -> Path:
    """cmd の auth.json。テストで差し替えられるよう関数にしている。"""
    return COMMANDCODE_AUTH_PATH


def _key_from_auth_file() -> Optional[str]:
    """auth.json の apiKey。読めなければ None。"""
    try:
        data = json.loads(_auth_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key = data.get("apiKey") if isinstance(data, dict) else None
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def locate_commandcode_key() -> Tuple[str, str]:
    """(API key, 取得元の説明)。どこにも無ければ CredentialError。"""
    stored = keychain.read("commandcode")
    if stored:
        return stored, t("providers.detect.keychain", provider="commandcode")
    env = os.environ.get("COMMANDCODE_API_KEY")
    if env and env.strip():
        return env.strip(), t("providers.detect.env", variable="COMMANDCODE_API_KEY")
    key = _key_from_auth_file()
    if key:
        return key, t("providers.detect.commandcode_auth")
    raise CredentialError(t("providers.commandcode_login_required"))


def read_commandcode_api_key() -> str:
    return locate_commandcode_key()[0]


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """API キーがあるか。通信しない。"""
    try:
        _, source = locate_commandcode_key()
    except CredentialError as exc:
        return Detection(False, None, str(exc) or t("providers.commandcode_login_required"))
    return Detection(True, t("providers.detect.api_key", name="Command Code", source=source))


# --------------------------------------------------------------- parser

def _reset_at(value: Any) -> Optional[datetime]:
    """resetAt (0 = 窓がまだ開いていない) → 時刻。10^11 以上はミリ秒、未満は秒。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return _from_epoch_millis(value) if value >= 1e11 else _from_epoch(value)


def _plan_prefix(plan_id: Any) -> Optional[str]:
    if not isinstance(plan_id, str):
        return None
    normalized = plan_id.strip().lower().replace("_", "-")
    for prefix in _PLAN_PREFIXES:
        if normalized.startswith(prefix):
            return prefix
    return None


def _credit(value: Any) -> float:
    """残クレジット。無い・数値化できない・負のときは 0。"""
    try:
        return max(0.0, finite_number(value))
    except ValueError:
        return 0.0


def _window_from_limit(item: Any, seconds: int) -> Optional[Window]:
    """windowLimits の fiveHour / weekly 1 つ → 窓。使えない値なら None。"""
    if not isinstance(item, dict):
        return None
    try:
        used = finite_number(item.get("used"))
        cap = finite_number(item.get("cap"))
    except ValueError:
        return None
    if cap <= 0:
        return None
    value = min(100.0, max(0.0, used / cap * 100.0))
    if item.get("exceeded") is True:
        value = 100.0
    key, label, length = _window_identity(seconds)
    return Window("commandcode", key, label, value,
                  _reset_at(item.get("resetAt")), length)


def parse_commandcode(
    credits_data: Any, subscription_data: Any = None,
    now: Optional[datetime] = None,
) -> Tuple[str, List[Window], List[Balance]]:
    """(name, windows, balances) from the credits + subscription responses.

    5 時間・週間の窓は credits.windowLimits (limited が false の従量課金だけの
    アカウントや、壊れた窓は出さない)。月間の窓は、有効な購読 (status が active)
    の既知のプランがあるときだけ、(プール - 残り) / プール で出し、リセットは
    購読の currentPeriodEnd。プール = max(プランの月間クレジット, 月間残り)
    + 追加購入分 + 無料分。追加購入分と無料分の残りは USD の残高として出す。
    月間残りが欠落・非数値のときは月間の窓を出さない (使い切りと区別できない)。
    窓が 1 つも無いときだけ ValueError。"""
    if not isinstance(credits_data, dict):
        raise ValueError("invalid response")
    credits = credits_data.get("credits")
    if credits is not None and not isinstance(credits, dict):
        raise ValueError("invalid credits")
    credits = credits or {}

    windows: List[Window] = []
    limits = credits_data.get("windowLimits")
    if isinstance(limits, dict) and limits.get("limited") is not False:
        for field, seconds in (("fiveHour", _FIVE_HOURS), ("weekly", _WEEK)):
            window = _window_from_limit(limits.get(field), seconds)
            if window is not None:
                windows.append(window)

    sub = subscription_data.get("data") if isinstance(subscription_data, dict) else None
    sub = sub if isinstance(sub, dict) else {}
    prefix = _plan_prefix(sub.get("planId"))
    name = "Command Code"
    if prefix is not None:
        name = "Command Code " + _PLAN_NAMES[prefix]

    try:  # 月間残りが無い・数値でないときは、使い切りと区別できないので月間の窓を出さない
        monthly = max(0.0, finite_number(credits.get("monthlyCredits")))
    except ValueError:
        monthly = None
    purchased = _credit(credits.get("purchasedCredits"))
    free = _credit(credits.get("freeCredits"))
    if prefix is not None and sub.get("status") == "active" and monthly is not None:
        pool = max(float(_PLAN_CREDITS[prefix]), monthly) + purchased + free
        remaining = monthly + purchased + free
        if pool > 0:
            value = min(100.0, max(0.0, (pool - remaining) / pool * 100.0))
            key, label, length = _window_identity(_MONTH)
            windows.append(Window(
                "commandcode", key, label, value,
                _parse_iso(sub.get("currentPeriodEnd")), length))
    if not windows:
        raise ValueError("no usage windows")

    balances: List[Balance] = []
    extra = purchased + free
    if extra > 0:
        balances.append(Balance("USD", extra))
    return name, windows, balances


# ---------------------------------------------------------------- fetch

def _get(path: str, key: str, org_id: Optional[str], timeout: float) -> Any:
    url = COMMANDCODE_API_URL + path
    if org_id:
        url += "?" + urllib.parse.urlencode({"orgId": org_id})
    return http_get_json(url, {
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,  # Python-urllib の既定 UA は Cloudflare に 403 される
    }, timeout=timeout)


def _org_id(data: Any) -> Optional[str]:
    org = data.get("org") if isinstance(data, dict) else None
    org_id = org.get("id") if isinstance(org, dict) else None
    if isinstance(org_id, str) and org_id.strip():
        return org_id.strip()
    return None


def _optional(path: str, key: str, org_id: Optional[str]) -> Any:
    """補助の取得。認証切れ (401/403) 以外の失敗は None (その分の情報を出さない)。"""
    try:
        return _get(path, key, org_id, TIMEOUT / 2)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise
        return None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None


def fetch(now: datetime) -> ProviderUsage:
    key = read_commandcode_api_key()
    try:
        org_id = _org_id(_optional(WHOAMI_PATH + "?limits=1", key, None))
        credits = _get(CREDITS_PATH, key, org_id, TIMEOUT)
        subscription = _optional(SUBSCRIPTIONS_PATH, key, org_id)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CredentialError(t("providers.commandcode_login_expired")) from None
        raise
    name, windows, balances = parse_commandcode(credits, subscription, now)
    return ProviderUsage("commandcode", name, windows, None, now, balances=balances)


PROVIDER = Provider(
    id="commandcode", name="Command Code", needs_key=True, detect=detect,
    fetch=fetch,
)
