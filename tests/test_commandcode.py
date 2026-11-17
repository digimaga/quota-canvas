"""Command Code provider: parser, key lookup order, offline detect and fetch.

Dummy values only; no network or real credentials.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.providers import CredentialError, commandcode

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc

CREDITS = {
    "credits": {"belowThreshold": False, "creditThreshold": 0,
                "monthlyCredits": 56.0, "purchasedCredits": 0, "freeCredits": 0},
    "windowLimits": {
        "limited": True, "exceeded": None,
        "fiveHour": {"used": 3.5, "cap": 14, "exceeded": False,
                     "resetAt": 1795000000000},
        "weekly": {"used": 7, "cap": 35, "exceeded": False, "resetAt": 0},
    },
    "sandboxAccess": True,
}
SUBSCRIPTION = {
    "success": True,
    "data": {"status": "active", "planId": "individual-goat",
             "currentPeriodStart": "2026-11-17T04:45:43.000Z",
             "currentPeriodEnd": "2026-12-17T04:45:43.000Z"},
}


def _auth_file(monkeypatch, tmp_path, payload):
    path = tmp_path / "cc-auth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(commandcode, "_auth_file", lambda: path)
    return path


# --------------------------------------------------------------- parser

def test_parse_windows_and_name():
    name, windows, balances = commandcode.parse_commandcode(CREDITS, SUBSCRIPTION, NOW)
    assert name == "Command Code GOAT"
    by_key = {w.key: w for w in windows}
    assert list(by_key) == ["5h", "weekly", "30d"]

    five = by_key["5h"]
    assert five.id == "commandcode:5h"
    assert five.used_percent == pytest.approx(25.0)
    assert five.resets_at == datetime.fromtimestamp(1795000000, tz=UTC)  # ミリ秒
    assert five.length == timedelta(hours=5)

    weekly = by_key["weekly"]
    assert weekly.id == "commandcode:weekly"
    assert weekly.used_percent == pytest.approx(20.0)
    assert weekly.resets_at is None  # 窓がまだ開いていない (resetAt 0)
    assert weekly.length == timedelta(days=7)

    month = by_key["30d"]
    assert month.used_percent == pytest.approx(20.0)  # (70 - 56) / 70
    assert month.resets_at == datetime(2026, 12, 17, 4, 45, 43, tzinfo=UTC)
    assert month.length == timedelta(days=30)
    assert balances == []


def test_reset_at_seconds_and_millis():
    assert commandcode._reset_at(1795000000) == datetime.fromtimestamp(1795000000, tz=UTC)
    assert commandcode._reset_at(1795000000000) == datetime.fromtimestamp(1795000000, tz=UTC)
    for bad in (0, -5, None, "x", True, float("nan"), float("inf")):
        assert commandcode._reset_at(bad) is None


def test_exceeded_forces_full():
    data = {"windowLimits": {"limited": True,
            "fiveHour": {"used": 1, "cap": 14, "exceeded": True, "resetAt": 0},
            "weekly": {"used": 99, "cap": 35, "exceeded": False, "resetAt": 0}}}
    _, windows, _ = commandcode.parse_commandcode(data)
    by_key = {w.key: w for w in windows}
    assert by_key["5h"].used_percent == 100.0
    assert by_key["weekly"].used_percent == 100.0  # 99/35 を 100 に丸める


def test_bad_windows_are_skipped_and_none_valid_raises():
    data = {"windowLimits": {"limited": True,
            "fiveHour": {"used": "x", "cap": 14},
            "weekly": {"used": 1, "cap": 0}}}
    with pytest.raises(ValueError):
        commandcode.parse_commandcode(data)
    data["windowLimits"]["weekly"] = {"used": 5, "cap": 10}
    _, windows, _ = commandcode.parse_commandcode(data)
    assert [w.key for w in windows] == ["weekly"]


def test_not_limited_account_has_no_cap_windows():
    data = {"credits": {"monthlyCredits": 10},
            "windowLimits": {"limited": False,
                             "fiveHour": {"used": 1, "cap": 14},
                             "weekly": {"used": 1, "cap": 35}}}
    with pytest.raises(ValueError):
        commandcode.parse_commandcode(data)  # 購読が無く、月間の窓も出ない
    _, windows, _ = commandcode.parse_commandcode(data, SUBSCRIPTION)
    assert [w.key for w in windows] == ["30d"]


@pytest.mark.parametrize("sub", [
    None, {}, {"data": None},
    {"data": {"status": "canceled", "planId": "individual-goat"}},
    {"data": {"status": "active", "planId": "individual-unknown"}},
    {"data": {"status": "active", "planId": 5}},
])
def test_monthly_needs_active_known_plan(sub):
    _, windows, _ = commandcode.parse_commandcode(CREDITS, sub)
    assert [w.key for w in windows] == ["5h", "weekly"]


def test_monthly_pool_with_purchased_and_free_and_balance():
    data = dict(CREDITS)
    data["credits"] = {"monthlyCredits": 35, "purchasedCredits": 20, "freeCredits": 5}
    _, windows, balances = commandcode.parse_commandcode(data, SUBSCRIPTION)
    month = {w.key: w for w in windows}["30d"]
    # pool = 70 + 20 + 5 = 95、残り = 35 + 20 + 5 = 60
    assert month.used_percent == pytest.approx(35 / 95 * 100)
    assert [(b.currency, b.amount) for b in balances] == [("USD", 25.0)]


def test_monthly_remaining_above_plan_total_extends_pool():
    data = dict(CREDITS)
    data["credits"] = {"monthlyCredits": 100}  # プラン表より大きい (表が古い)
    _, windows, _ = commandcode.parse_commandcode(data, SUBSCRIPTION)
    assert {w.key: w for w in windows}["30d"].used_percent == 0.0


def test_plan_prefix_matching_prefers_longest():
    assert commandcode._plan_prefix("individual-pro-v1") == "individual-pro-v1"
    assert commandcode._plan_prefix("individual_pro") == "individual-pro"
    assert commandcode._plan_prefix("teams-pro-2026") == "teams-pro"
    assert commandcode._plan_prefix(None) is None
    sub = {"data": {"status": "active", "planId": "individual-pro-v1"}}
    name, _, _ = commandcode.parse_commandcode(CREDITS, sub)
    assert name == "Command Code Pro"


def test_negative_extra_credits_count_as_zero():
    data = dict(CREDITS)
    data["credits"] = {"monthlyCredits": 0, "purchasedCredits": -3, "freeCredits": None}
    _, windows, balances = commandcode.parse_commandcode(data, SUBSCRIPTION)
    assert {w.key: w for w in windows}["30d"].used_percent == 100.0
    assert balances == []


@pytest.mark.parametrize("credits", [{}, None, {"monthlyCredits": "x"},
                                     {"monthlyCredits": None, "purchasedCredits": 5}])
def test_missing_monthly_credits_gives_no_monthly_window(credits):
    data = dict(CREDITS)
    data["credits"] = credits
    _, windows, _ = commandcode.parse_commandcode(data, SUBSCRIPTION)
    assert [w.key for w in windows] == ["5h", "weekly"]


@pytest.mark.parametrize("bad", [None, [], "x", 3])
def test_parse_rejects_non_object(bad):
    with pytest.raises(ValueError):
        commandcode.parse_commandcode(bad)


def test_parse_rejects_non_object_credits():
    with pytest.raises(ValueError):
        commandcode.parse_commandcode({"credits": [], "windowLimits": CREDITS["windowLimits"]})


# ---------------------------------------------------------- credentials

def test_key_order_keychain_env_file(monkeypatch, tmp_path):
    _auth_file(monkeypatch, tmp_path, {"apiKey": "file-key"})
    assert commandcode.locate_commandcode_key()[0] == "file-key"

    monkeypatch.setenv("COMMANDCODE_API_KEY", " env-key ")
    key, source = commandcode.locate_commandcode_key()
    assert key == "env-key" and "COMMANDCODE_API_KEY" in source

    from quotacanvas import keychain
    monkeypatch.setattr(keychain, "read",
                        lambda provider: "kc-key" if provider == "commandcode" else None)
    assert commandcode.locate_commandcode_key()[0] == "kc-key"


@pytest.mark.parametrize("payload", [{}, {"apiKey": ""}, {"apiKey": 5}, [], "x"])
def test_bad_auth_file_means_no_key(monkeypatch, tmp_path, payload):
    _auth_file(monkeypatch, tmp_path, payload)
    with pytest.raises(CredentialError):
        commandcode.locate_commandcode_key()


def test_missing_and_corrupt_auth_file(monkeypatch, tmp_path):
    with pytest.raises(CredentialError):
        commandcode.locate_commandcode_key()
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(commandcode, "_auth_file", lambda: path)
    with pytest.raises(CredentialError):
        commandcode.locate_commandcode_key()


def test_detect_does_not_touch_network(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("detect must not use the network")
    monkeypatch.setattr(commandcode, "http_get_json", boom)
    missing = commandcode.detect()
    assert missing.detected is False and missing.error
    _auth_file(monkeypatch, tmp_path, {"apiKey": "k"})
    found = commandcode.detect()
    assert found.detected is True
    assert "Command Code" in found.source


# ---------------------------------------------------------------- fetch

class _Api:
    def __init__(self, whoami, credits=CREDITS, subscription=SUBSCRIPTION):
        self.calls = []
        self.responses = {
            "/alpha/whoami?limits=1": whoami,
            "/alpha/billing/credits": credits,
            "/alpha/billing/subscriptions": subscription,
        }

    def __call__(self, url, headers=None, timeout=None):
        path = url[len(commandcode.COMMANDCODE_API_URL):]
        self.calls.append((path, headers, timeout))
        value = self.responses[path.split("?orgId=")[0]]
        if isinstance(value, BaseException):
            raise value
        return value


def _http_error(code):
    return urllib.error.HTTPError("https://example.test", code, "x", {}, None)


def test_fetch_individual(monkeypatch, tmp_path):
    _auth_file(monkeypatch, tmp_path, {"apiKey": "dummy-key"})
    api = _Api({"success": True, "org": None})
    monkeypatch.setattr(commandcode, "http_get_json", api)
    usage = commandcode.fetch(NOW)
    assert usage.provider == "commandcode"
    assert usage.name == "Command Code GOAT"
    assert [w.key for w in usage.windows] == ["5h", "weekly", "30d"]
    assert usage.fetched_at == NOW and usage.error is None
    assert [c[0] for c in api.calls] == [
        "/alpha/whoami?limits=1", "/alpha/billing/credits", "/alpha/billing/subscriptions"]
    for _, headers, _ in api.calls:
        assert headers["Authorization"] == "Bearer dummy-key"
        assert headers["User-Agent"] == "quota-canvas"


def test_fetch_passes_org_id(monkeypatch, tmp_path):
    _auth_file(monkeypatch, tmp_path, {"apiKey": "k"})
    api = _Api({"org": {"id": "org 1"}})
    monkeypatch.setattr(commandcode, "http_get_json", api)
    commandcode.fetch(NOW)
    assert api.calls[1][0] == "/alpha/billing/credits?orgId=org+1"
    assert api.calls[2][0] == "/alpha/billing/subscriptions?orgId=org+1"


def test_fetch_survives_optional_failures(monkeypatch, tmp_path):
    _auth_file(monkeypatch, tmp_path, {"apiKey": "k"})
    api = _Api(_http_error(500), subscription=urllib.error.URLError("down"))
    monkeypatch.setattr(commandcode, "http_get_json", api)
    usage = commandcode.fetch(NOW)
    assert usage.name == "Command Code"
    assert [w.key for w in usage.windows] == ["5h", "weekly"]


def test_fetch_survives_truncated_optional_body(monkeypatch, tmp_path):
    _auth_file(monkeypatch, tmp_path, {"apiKey": "k"})
    api = _Api({"org": None}, subscription=http.client.IncompleteRead(b"{"))
    monkeypatch.setattr(commandcode, "http_get_json", api)
    usage = commandcode.fetch(NOW)
    assert [w.key for w in usage.windows] == ["5h", "weekly"]


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("failing", ["whoami", "credits", "subscription"])
def test_fetch_auth_failure_is_credential_error(monkeypatch, tmp_path, code, failing):
    _auth_file(monkeypatch, tmp_path, {"apiKey": "k"})
    kwargs = {"whoami": {"org": None}}
    if failing == "whoami":
        kwargs["whoami"] = _http_error(code)
    elif failing == "credits":
        kwargs["credits"] = _http_error(code)
    else:
        kwargs["subscription"] = _http_error(code)
    monkeypatch.setattr(commandcode, "http_get_json", _Api(**kwargs))
    with pytest.raises(CredentialError):
        commandcode.fetch(NOW)


def test_fetch_server_error_on_credits_propagates(monkeypatch, tmp_path):
    _auth_file(monkeypatch, tmp_path, {"apiKey": "k"})
    monkeypatch.setattr(commandcode, "http_get_json",
                        _Api({"org": None}, credits=_http_error(500)))
    with pytest.raises(urllib.error.HTTPError):
        commandcode.fetch(NOW)


def test_fetch_without_key_raises_credential_error():
    with pytest.raises(CredentialError):
        commandcode.fetch(NOW)


def test_registered():
    assert providers.REGISTRY["commandcode"] is commandcode.PROVIDER
    assert commandcode.PROVIDER.needs_key is True
