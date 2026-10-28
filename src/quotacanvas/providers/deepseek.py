"""DeepSeek account balances."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from ..i18n import t
from ..models import Balance, ProviderUsage
from ._base import (
    USER_AGENT, Detection, Provider, detect_api_key, finite_number,
    http_get_json, locate_api_key,
)

BALANCE_URL = "https://api.deepseek.com/user/balance"


def detect() -> Detection:
    """Check the keychain and environment without making a request."""
    return detect_api_key("deepseek", "DeepSeek", "DEEPSEEK_API_KEY")


def parse_balance(data: Dict[str, Any]) -> List[Balance]:
    """Return the total available balance for each reported currency."""
    available = data["is_available"]
    if not isinstance(available, bool) or not isinstance(data["balance_infos"], list) or not data["balance_infos"]:
        raise ValueError("invalid balance response")
    result = []
    for item in data["balance_infos"]:
        currency = item["currency"]
        if currency not in ("USD", "CNY"):
            raise ValueError("invalid currency")
        result.append(Balance(currency, finite_number(item["total_balance"]),
                              None if available else t("providers.insufficient_balance")))
    return result


def fetch(now: datetime) -> ProviderUsage:
    """Fetch DeepSeek balances with the configured API key."""
    key, _ = locate_api_key("deepseek", "DEEPSEEK_API_KEY")
    data = http_get_json(BALANCE_URL, {"Authorization": "Bearer " + key, "User-Agent": USER_AGENT})
    return ProviderUsage("deepseek", "DeepSeek", [], None, now, balances=parse_balance(data))


PROVIDER = Provider(
    id="deepseek", name="DeepSeek", needs_key=True, detect=detect, fetch=fetch,
    display_name="DeepSeek",
)
