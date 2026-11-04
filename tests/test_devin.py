"""Devin provider: parser, key lookup order and offline detect.

Dummy values only; no network or real credentials.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.providers import CredentialError, devin

NOW = datetime(2026, 11, 4, 6, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc

STATUS = {
    "userStatus": {
        "planStatus": {
            "planInfo": {
                "planName": "Pro",
                "billingStrategy": "BILLING_STRATEGY_QUOTA",
                "hideDailyQuota": False,
            },
            "planStart": "2026-11-04T05:26:56Z",
            "planEnd": "2026-12-04T05:26:56Z",
            "dailyQuotaRemainingPercent": 100,
            "weeklyQuotaRemainingPercent": 100,
            "overageBalanceMicros": "10000000",
            "dailyQuotaResetAtUnix": "1793779200",
            "weeklyQuotaResetAtUnix": "1794124800",
        }
    }
}


def _status(plan_status, plan_info=None):
    if plan_info is None:
        plan_info = {"planName": "Pro"}
    return {"userStatus": {"planStatus": dict(plan_status, planInfo=plan_info)}}


def _credentials(monkeypatch, tmp_path, text):
    path = tmp_path / "devin-credentials.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(devin, "_credentials_file", lambda: path)
    return path


def _state_db(monkeypatch, tmp_path, rows):
    path = tmp_path / "devin-state.vscdb"
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    connection.executemany("INSERT INTO ItemTable (key, value) VALUES (?, ?)", rows)
    connection.commit()
    connection.close()
    monkeypatch.setattr(devin, "_desktop_state_db", lambda: path)
    return path


# --------------------------------------------------------------- parser

def test_parse_devin_windows():
    name, windows, balances = devin.parse_devin(STATUS, NOW)
    assert name == "Devin Pro"
    by_key = {w.key: w for w in windows}
    assert list(by_key) == ["1d", "weekly"]

    daily = by_key["1d"]
    assert daily.provider == "devin"
    assert daily.id == "devin:1d"
    assert daily.label == "1日"
    assert daily.used_percent == pytest.approx(0.0)
    assert daily.resets_at == datetime(2026, 11, 4, 8, 0, 0, tzinfo=UTC)
    assert daily.length == timedelta(days=1)

    weekly = by_key["weekly"]
    assert weekly.id == "devin:weekly"
    assert weekly.label == "週間"
    assert weekly.used_percent == pytest.approx(0.0)
    assert weekly.resets_at == datetime(2026, 11, 8, 8, 0, 0, tzinfo=UTC)
    assert weekly.length == timedelta(days=7)

    assert len(balances) == 1
    assert balances[0].currency == "USD"
    assert balances[0].amount == pytest.approx(10.0)
    assert balances[0].label == "追加利用残高"


def test_parse_devin_used_percent_is_100_minus_remaining():
    data = _status({"dailyQuotaRemainingPercent": 40,
                    "weeklyQuotaRemainingPercent": 75})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert [w.used_percent for w in windows] == pytest.approx([60.0, 25.0])
    assert all(w.resets_at is None for w in windows)


def test_parse_devin_omitted_zero_with_reset_is_100_percent():
    # proto3 はゼロを省略する: 残り % が無くリセットだけある窓は使用率 100%
    data = _status({"dailyQuotaResetAtUnix": 1793779200})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert [w.key for w in windows] == ["1d"]
    assert windows[0].used_percent == pytest.approx(100.0)
    assert windows[0].resets_at == datetime(2026, 11, 4, 8, 0, 0, tzinfo=UTC)


def test_parse_devin_window_without_reset_is_kept():
    data = _status({"weeklyQuotaRemainingPercent": 0})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert [w.key for w in windows] == ["weekly"]
    assert windows[0].used_percent == pytest.approx(100.0)
    assert windows[0].resets_at is None


def test_parse_devin_null_remaining_is_treated_as_omitted():
    # null は「キー無し」と同じ: リセットがあれば残り 0 (使用率 100)
    data = _status({"dailyQuotaRemainingPercent": None,
                    "dailyQuotaResetAtUnix": 1793779200})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert [w.key for w in windows] == ["1d"]
    assert windows[0].used_percent == pytest.approx(100.0)
    assert windows[0].resets_at == datetime(2026, 11, 4, 8, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("reset", ["abc", "", "not-a-number"])
def test_parse_devin_invalid_reset_is_treated_as_missing(reset):
    data = _status({"dailyQuotaRemainingPercent": 40,
                    "dailyQuotaResetAtUnix": reset})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert [w.key for w in windows] == ["1d"]
    assert windows[0].used_percent == pytest.approx(60.0)
    assert windows[0].resets_at is None


def test_parse_devin_invalid_reset_without_percent_is_no_quota():
    data = _status({"dailyQuotaResetAtUnix": "abc"})
    with pytest.raises(ValueError):
        devin.parse_devin(data, NOW)


def test_parse_devin_skips_windows_without_percent_or_reset():
    data = _status({"dailyQuotaRemainingPercent": 30})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert [w.key for w in windows] == ["1d"]


def test_parse_devin_hide_daily_quota():
    data = _status({"dailyQuotaRemainingPercent": 50,
                    "weeklyQuotaRemainingPercent": 50},
                   {"planName": "Pro", "hideDailyQuota": True})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert [w.key for w in windows] == ["weekly"]


def test_parse_devin_hide_weekly_quota():
    data = _status({"dailyQuotaRemainingPercent": 50,
                    "weeklyQuotaRemainingPercent": 50},
                   {"planName": "Pro", "hideWeeklyQuota": True})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert [w.key for w in windows] == ["1d"]


def test_parse_devin_hide_both_is_error():
    data = _status({"dailyQuotaResetAtUnix": 1793779200,
                    "weeklyQuotaResetAtUnix": 1794124800},
                   {"planName": "Pro", "hideDailyQuota": True,
                    "hideWeeklyQuota": True})
    with pytest.raises(ValueError):
        devin.parse_devin(data, NOW)


@pytest.mark.parametrize("plan_info,expected", [
    ({}, "Devin"),
    ({"planName": None}, "Devin"),
    ({"planName": ""}, "Devin"),
    ({"planName": "   "}, "Devin"),
    ({"planName": 42}, "Devin"),
    ({"planName": " Pro "}, "Devin Pro"),
])
def test_parse_devin_name(plan_info, expected):
    data = _status({"dailyQuotaRemainingPercent": 100}, plan_info)
    name, _, _ = devin.parse_devin(data, NOW)
    assert name == expected


@pytest.mark.parametrize("remaining,expected_used", [
    (150, 0.0),
    (100, 0.0),
    (0, 100.0),
    (-5, 100.0),
])
def test_parse_devin_percent_is_clamped(remaining, expected_used):
    data = _status({"dailyQuotaRemainingPercent": remaining})
    _, windows, _ = devin.parse_devin(data, NOW)
    assert windows[0].used_percent == pytest.approx(expected_used)


@pytest.mark.parametrize("remaining", ["40", True, float("nan"), float("inf")])
def test_parse_devin_invalid_percent_is_error(remaining):
    data = _status({"dailyQuotaRemainingPercent": remaining})
    with pytest.raises(ValueError):
        devin.parse_devin(data, NOW)


@pytest.mark.parametrize("data", [
    [],
    "userStatus",
    None,
    {},
    {"userStatus": []},
    {"userStatus": "not-a-dict"},
    {"userStatus": {"planStatus": []}},
    {"userStatus": {"planStatus": None}},
])
def test_parse_devin_non_dict_response_is_error(data):
    with pytest.raises(ValueError):
        devin.parse_devin(data, NOW)


@pytest.mark.parametrize("plan_status", [
    {},
    {"dailyQuotaRemainingPercent": None},
    {"weeklyQuotaResetAtUnix": None},
])
def test_parse_devin_no_quota_is_error(plan_status):
    with pytest.raises(ValueError):
        devin.parse_devin(_status(plan_status), NOW)


@pytest.mark.parametrize("micros,expected", [
    ("10000000", 10.0),
    (2_500_000, 2.5),
    ("-2500000", -2.5),
    (0, 0.0),
])
def test_parse_devin_overage_balance(micros, expected):
    data = _status({"dailyQuotaRemainingPercent": 100,
                    "overageBalanceMicros": micros})
    _, _, balances = devin.parse_devin(data, NOW)
    assert len(balances) == 1
    assert balances[0].currency == "USD"
    assert balances[0].amount == pytest.approx(expected)
    assert balances[0].label == "追加利用残高"


def test_parse_devin_omitted_overage_has_no_balance():
    data = _status({"dailyQuotaRemainingPercent": 100})
    _, _, balances = devin.parse_devin(data, NOW)
    assert balances == []


@pytest.mark.parametrize("micros", ["abc", None, True, float("nan"), float("inf")])
def test_parse_devin_invalid_overage_drops_only_balance(micros):
    data = _status({"dailyQuotaRemainingPercent": 100,
                    "overageBalanceMicros": micros})
    _, windows, balances = devin.parse_devin(data, NOW)
    assert [w.key for w in windows] == ["1d"]
    assert balances == []


def test_parse_devin_balance_only_response():
    # 窓が 1 つも無くても残高が取れたら windows=[] で残高だけ返す
    data = _status({"overageBalanceMicros": "10000000"},
                   {"planName": "Pro", "hideDailyQuota": True,
                    "hideWeeklyQuota": True})
    name, windows, balances = devin.parse_devin(data, NOW)
    assert name == "Devin Pro"
    assert windows == []
    assert len(balances) == 1
    assert balances[0].amount == pytest.approx(10.0)


@pytest.mark.parametrize("plan_status", [
    {},
    {"overageBalanceMicros": "abc"},
])
def test_parse_devin_no_windows_and_no_balance_is_error(plan_status):
    data = _status(plan_status,
                   {"planName": "Pro", "hideDailyQuota": True,
                    "hideWeeklyQuota": True})
    with pytest.raises(ValueError):
        devin.parse_devin(data, NOW)


# --------------------------------------------------------- key lookup order

def test_keychain_takes_priority(monkeypatch, tmp_path):
    monkeypatch.setattr(devin.keychain, "read", lambda account: "keychain-dummy")
    _credentials(monkeypatch, tmp_path,
                 'windsurf_api_key = "cli-dummy"\n'
                 'api_server_url = "https://api.devin.ai"\n')
    _state_db(monkeypatch, tmp_path,
              [("windsurfAuthStatus", json.dumps({"apiKey": "desktop-dummy"}))])

    key, server, source = devin.locate_devin_key()
    assert key == "keychain-dummy"
    assert server == "https://server.codeium.com"
    assert source == "キーチェーン quota-canvas/devin"


def test_cli_credentials_are_used_when_keychain_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)
    _credentials(monkeypatch, tmp_path,
                 'windsurf_api_key = "cli-dummy"\n'
                 'api_server_url = "https://api.devin.ai/v1?x=1"\n')
    _state_db(monkeypatch, tmp_path,
              [("windsurfAuthStatus", json.dumps({"apiKey": "desktop-dummy"}))])

    key, server, source = devin.locate_devin_key()
    assert key == "cli-dummy"
    assert server == "https://api.devin.ai"  # パス・クエリは捨てる
    assert source == "Devin CLI のログイン（~/.local/share/devin/credentials.toml）"


@pytest.mark.parametrize("server_line,expected_server", [
    ("", "https://server.codeium.com"),
    ('api_server_url = "http://server.codeium.com"\n', "https://server.codeium.com"),
    ('api_server_url = 42\n', "https://server.codeium.com"),
    ('api_server_url = "https://example.test"\n', "https://server.codeium.com"),
    ('api_server_url = "https://notcodeium.com"\n', "https://server.codeium.com"),
    ('api_server_url = "https://codeium.com.evil.test"\n', "https://server.codeium.com"),
    ('api_server_url = "https://server.codeium.com"\n', "https://server.codeium.com"),
    ('api_server_url = "https://api.codeium.com/v1?x=1"\n', "https://api.codeium.com"),
    ('api_server_url = "https://windsurf.com"\n', "https://windsurf.com"),
    ('api_server_url = "https://sub.windsurf.com:8443/path"\n', "https://sub.windsurf.com:8443"),
    ('api_server_url = "https://api.devin.ai"\n', "https://api.devin.ai"),
])
def test_cli_server_url(monkeypatch, tmp_path, server_line, expected_server):
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)
    _credentials(monkeypatch, tmp_path,
                 'windsurf_api_key = "cli-dummy"\n' + server_line)
    _state_db(monkeypatch, tmp_path, [])

    _, server, _ = devin.locate_devin_key()
    assert server == expected_server


def test_desktop_state_db_is_used_as_last_resort(monkeypatch, tmp_path):
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)
    _credentials(monkeypatch, tmp_path, "")  # 空の TOML
    _state_db(monkeypatch, tmp_path,
              [("windsurfAuthStatus", json.dumps({"apiKey": "desktop-dummy"}))])

    key, server, source = devin.locate_devin_key()
    assert key == "desktop-dummy"
    assert server == "https://server.codeium.com"
    assert source == "Devin デスクトップアプリのログイン（state.vscdb）"


def test_desktop_state_db_blob_value(monkeypatch, tmp_path):
    # VS Code の state.vscdb は value を BLOB で保存することがある
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)
    _credentials(monkeypatch, tmp_path, "")
    _state_db(monkeypatch, tmp_path,
              [("windsurfAuthStatus",
                json.dumps({"apiKey": "blob-dummy"}).encode("utf-8"))])

    key, _, source = devin.locate_devin_key()
    assert key == "blob-dummy"
    assert source == "Devin デスクトップアプリのログイン（state.vscdb）"


def test_desktop_state_db_without_api_key_falls_through(monkeypatch, tmp_path):
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)
    _credentials(monkeypatch, tmp_path, "")
    _state_db(monkeypatch, tmp_path,
              [("windsurfAuthStatus", json.dumps({"other": "x"}))])

    with pytest.raises(CredentialError):
        devin.locate_devin_key()


@pytest.mark.parametrize("payload", [
    {},
    {"windsurf_api_key": None},
    {"windsurf_api_key": "   "},
    {"windsurf_api_key": 42},
])
def test_cli_credentials_without_usable_key_fall_through(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)
    _credentials(monkeypatch, tmp_path,
                 "".join('%s = "%s"\n' % (k, v) if isinstance(v, str) else ""
                         for k, v in payload.items()) or "")
    _state_db(monkeypatch, tmp_path, [])

    with pytest.raises(CredentialError):
        devin.locate_devin_key()


def test_missing_or_broken_credentials_are_error(monkeypatch, tmp_path):
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)

    monkeypatch.setattr(devin, "_credentials_file",
                        lambda: tmp_path / "missing.toml")
    monkeypatch.setattr(devin, "_desktop_state_db",
                        lambda: tmp_path / "missing.vscdb")
    with pytest.raises(CredentialError):
        devin.locate_devin_key()

    broken_toml = tmp_path / "broken.toml"
    broken_toml.write_text("{not toml", encoding="utf-8")
    monkeypatch.setattr(devin, "_credentials_file", lambda: broken_toml)
    with pytest.raises(CredentialError):
        devin.locate_devin_key()

    broken_db = tmp_path / "broken.vscdb"
    broken_db.write_text("not a database", encoding="utf-8")
    monkeypatch.setattr(devin, "_desktop_state_db", lambda: broken_db)
    with pytest.raises(CredentialError):
        devin.locate_devin_key()


# ------------------------------------------------------------- detection

def test_detect_uses_keychain_without_network(monkeypatch):
    monkeypatch.setattr(devin.keychain, "read", lambda account: "keychain-dummy")

    def no_network(*args, **kwargs):
        raise AssertionError("detect must not make a request")

    monkeypatch.setattr(devin, "http_post_json", no_network)
    detection = devin.detect()
    assert detection.detected is True
    assert "Devin" in detection.source
    assert "キーチェーン" in detection.source
    assert detection.error is None


def test_detect_reports_missing_key_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)
    monkeypatch.setattr(devin, "_credentials_file",
                        lambda: tmp_path / "missing.toml")
    monkeypatch.setattr(devin, "_desktop_state_db",
                        lambda: tmp_path / "missing.vscdb")

    def no_network(*args, **kwargs):
        raise AssertionError("detect must not make a request")

    monkeypatch.setattr(devin, "http_post_json", no_network)
    detection = devin.detect()
    assert detection.detected is False
    assert detection.source is None
    assert detection.error == "API キーが未設定"


# ------------------------------------------------------------------ fetch

def test_fetch_posts_status_request(monkeypatch):
    monkeypatch.setattr(devin.keychain, "read", lambda account: "keychain-dummy")
    captured = {}

    def fake_http(url, headers, body, timeout=providers.TIMEOUT):
        captured.update(url=url, headers=headers, body=body)
        return STATUS

    monkeypatch.setattr(devin, "http_post_json", fake_http)
    usage = devin.fetch(NOW)

    assert captured["url"] == (
        "https://server.codeium.com"
        "/exa.seat_management_pb.SeatManagementService/GetUserStatus")
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["Connect-Protocol-Version"] == "1"
    assert captured["headers"]["User-Agent"] == "quota-canvas"
    metadata = captured["body"]["metadata"]
    assert metadata["apiKey"] == "keychain-dummy"
    assert metadata["ideName"] == "windsurf"
    assert metadata["ideVersion"] == "3.11.238"
    assert metadata["extensionName"] == "windsurf"
    assert metadata["extensionVersion"] == "3.11.238"
    assert metadata["locale"] == "en"

    assert usage.provider == "devin"
    assert usage.name == "Devin Pro"
    assert usage.error is None
    assert [w.id for w in usage.windows] == ["devin:1d", "devin:weekly"]
    assert len(usage.balances) == 1
    assert usage.fetched_at == NOW


def test_fetch_uses_cli_server_url(monkeypatch, tmp_path):
    monkeypatch.setattr(devin.keychain, "read", lambda account: None)
    _credentials(monkeypatch, tmp_path,
                 'windsurf_api_key = "cli-dummy"\n'
                 'api_server_url = "https://api.devin.ai/v1?x=1"\n')
    captured = {}

    def fake_http(url, headers, body, timeout=providers.TIMEOUT):
        captured.update(url=url, body=body)
        return STATUS

    monkeypatch.setattr(devin, "http_post_json", fake_http)
    devin.fetch(NOW)
    assert captured["url"] == (
        "https://api.devin.ai"
        "/exa.seat_management_pb.SeatManagementService/GetUserStatus")
    assert captured["body"]["metadata"]["apiKey"] == "cli-dummy"


def test_fetch_propagates_http_errors(monkeypatch):
    monkeypatch.setattr(devin.keychain, "read", lambda account: "keychain-dummy")

    def unauthorized(url, headers, body, timeout=providers.TIMEOUT):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(devin, "http_post_json", unauthorized)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        devin.fetch(NOW)
    assert excinfo.value.code == 401


# ---------------------------------------------------------------- provider

def test_provider_metadata():
    assert devin.PROVIDER.id == "devin"
    assert devin.PROVIDER.name == "Devin"
    assert devin.PROVIDER.display_name == "Devin"
    assert devin.PROVIDER.needs_key is True
    assert providers.REGISTRY["devin"] is devin.PROVIDER
