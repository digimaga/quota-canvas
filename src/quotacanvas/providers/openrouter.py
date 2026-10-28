"""OpenRouter API key limits, usage and account credits (USD)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from ..i18n import t
from ..models import Balance, ProviderUsage, Window
from ._base import (
    USER_AGENT, Detection, Provider, _short_reason, detect_api_key,
    finite_number, http_get_json, locate_api_key,
)

KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
log = logging.getLogger("quotacanvas")
_credits_warning_logged = False


def detect() -> Detection:
    """Check the keychain and environment without making a request."""
    return detect_api_key("openrouter", "OpenRouter", "OPENROUTER_API_KEY")


def _usage_balance(payload: Dict[str, Any]) -> Balance:
    """今月の使用額 (無ければ累計) を表示専用の残高行にする。"""
    monthly = payload.get("usage_monthly")
    if monthly is not None:
        return Balance("USD", finite_number(monthly), t("providers.monthly_usage"))
    return Balance("USD", finite_number(payload["usage"]), t("providers.total_usage"))


def parse_key(data: Dict[str, Any], now: datetime) -> Tuple[List[Window], List[Balance]]:
    """Return the recurring limit window or display-only key usage/balance."""
    payload = data["data"]
    limit = payload["limit"]
    remaining = payload.get("limit_remaining")
    if limit is None or remaining is None:
        return [], [_usage_balance(payload)]  # 上限が無い・残りが分からないときは使用額を出す

    limit = finite_number(limit)
    remaining = finite_number(remaining)
    period = payload.get("limit_reset")
    if period not in ("daily", "weekly", "monthly") or limit <= 0:
        return [], [Balance("USD", remaining, t("providers.limit_remaining"))]

    utc = now.astimezone(timezone.utc)
    midnight = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        key, label, length = "daily", t("providers.window.days", n=1), timedelta(days=1)
        reset = midnight + length
    elif period == "weekly":
        key, label, length = "weekly", t("providers.window.weekly"), timedelta(days=7)
        reset = midnight + timedelta(days=7 - midnight.weekday())
    else:
        key, label = "monthly", t("providers.window.monthly")
        start = midnight.replace(day=1)
        reset = start.replace(year=start.year + (start.month == 12),
                              month=start.month % 12 + 1)
        length = reset - start
    used = min(100.0, max(0.0, (limit - remaining) / limit * 100))
    return [Window("openrouter", key, label, used, reset, length)], []


def parse_credits(data: Dict[str, Any]) -> Balance:
    """Return purchased credits minus cumulative account usage."""
    payload = data["data"]
    return Balance("USD", finite_number(payload["total_credits"]) -
                   finite_number(payload["total_usage"]))


def fetch(now: datetime) -> ProviderUsage:
    """Fetch key information and, when permitted, account credits."""
    global _credits_warning_logged
    key, _ = locate_api_key("openrouter", "OPENROUTER_API_KEY")
    headers = {"Authorization": "Bearer " + key, "User-Agent": USER_AGENT}
    windows, balances = parse_key(http_get_json(KEY_URL, headers), now)
    try:
        balances.append(parse_credits(http_get_json(CREDITS_URL, headers)))
    except Exception as exc:  # /credits is optional, including transport failures
        reason = _short_reason(exc)
        if not _credits_warning_logged:
            _credits_warning_logged = True
            log.warning("OpenRouter credits unavailable: %s", reason)
        else:
            log.debug("OpenRouter credits unavailable: %s", reason)
    return ProviderUsage("openrouter", "OpenRouter", windows, None, now, balances=balances)


PROVIDER = Provider(
    id="openrouter", name="OpenRouter", needs_key=True, detect=detect, fetch=fetch,
    display_name="OpenRouter",
)
