"""Moonshot global API account balance (USD)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from ..models import Balance, ProviderUsage
from ._base import (
    USER_AGENT, Detection, Provider, detect_api_key, finite_number,
    http_get_json, locate_api_key,
)

BALANCE_URL = "https://api.moonshot.ai/v1/users/me/balance"


def detect() -> Detection:
    """Check the keychain and environment without making a request."""
    return detect_api_key("kimi", "Kimi", "MOONSHOT_API_KEY")


def parse_balance(data: Dict[str, Any]) -> List[Balance]:
    """Return the global USD balance, rejecting failed API responses."""
    if type(data["code"]) is not int or data["code"] != 0 or data["status"] is not True:
        raise ValueError("Moonshot balance request failed")
    return [Balance("USD", finite_number(data["data"]["available_balance"]))]


def fetch(now: datetime) -> ProviderUsage:
    """Fetch the global Moonshot balance with the configured API key."""
    key, _ = locate_api_key("kimi", "MOONSHOT_API_KEY")
    data = http_get_json(BALANCE_URL, {"Authorization": "Bearer " + key, "User-Agent": USER_AGENT})
    return ProviderUsage("kimi", "Kimi", [], None, now, balances=parse_balance(data))


PROVIDER = Provider(
    id="kimi", name="Kimi", needs_key=True, detect=detect, fetch=fetch,
    display_name="Kimi (Moonshot)",
)
