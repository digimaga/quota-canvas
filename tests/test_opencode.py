"""OpenCode Go provider: parser, key lookup order and offline detect.

Dummy values only; no network or real credentials.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.providers import CredentialError, opencode

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc

USAGE = {
    "usage": {
        "rolling": {"status": "ok", "percent": 2,
                    "resetsAt": "2026-10-16T10:57:34.000Z"},
        "weekly": {"status": "ok", "percent": 58,
                   "resetsAt": "2026-10-19T00:00:00.000Z"},
        "monthly": {"status": "ok", "percent": 29,
                    "resetsAt": "2026-11-13T09:14:35.000Z"},
    }
}


def _auth_file(monkeypatch, tmp_path, payload):
    path = tmp_path / "opencode-auth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(opencode, "_auth_file", lambda: path)
    return path


# --------------------------------------------------------------- parser

def test_parse_opencode_windows():
    name, windows = opencode.parse_opencode(USAGE, NOW)
    assert name == "OpenCode Go"
    by_key = {w.key: w for w in windows}
    assert list(by_key) == ["5h", "weekly", "30d"]

    five = by_key["5h"]
    assert five.provider == "opencode"
    assert five.id == "opencode:5h"
    assert five.label == "5時間"
    assert five.used_percent == pytest.approx(2.0)
    assert five.resets_at == datetime(2026, 10, 16, 10, 57, 34, tzinfo=UTC)
    assert five.length == timedelta(hours=5)

    weekly = by_key["weekly"]
    assert weekly.id == "opencode:weekly"
    assert weekly.label == "週間"
    assert weekly.used_percent == pytest.approx(58.0)
    assert weekly.resets_at == datetime(2026, 10, 19, tzinfo=UTC)
    assert weekly.length == timedelta(days=7)

    month = by_key["30d"]
    assert month.id == "opencode:30d"
    assert month.label == "30日"
    assert month.used_percent == pytest.approx(29.0)
    assert month.resets_at == datetime(2026, 11, 13, 9, 14, 35, tzinfo=UTC)
    assert month.length == timedelta(days=30)


def test_parse_opencode_null_resets_at():
    data = {"usage": {"rolling": {"percent": 3, "resetsAt": None}}}
    _, windows = opencode.parse_opencode(data, NOW)
    assert [w.key for w in windows] == ["5h"]
    assert windows[0].resets_at is None


@pytest.mark.parametrize("status", ["rate_limited", "exhausted", "unknown-status"])
def test_parse_opencode_non_ok_status_is_100_percent(status):
    data = {"usage": {"weekly": {"status": status, "percent": 58, "resetsAt": None}}}
    _, windows = opencode.parse_opencode(data, NOW)
    assert windows[0].used_percent == pytest.approx(100.0)


def test_parse_opencode_missing_status_keeps_percent():
    data = {"usage": {"weekly": {"percent": 58, "resetsAt": None}}}
    _, windows = opencode.parse_opencode(data, NOW)
    assert windows[0].used_percent == pytest.approx(58.0)


def test_parse_opencode_null_status_keeps_percent():
    data = {"usage": {"weekly": {"status": None, "percent": 58, "resetsAt": None}}}
    _, windows = opencode.parse_opencode(data, NOW)
    assert windows[0].used_percent == pytest.approx(58.0)


def test_parse_opencode_status_ok_keeps_percent():
    data = {"usage": {"rolling": {"status": "ok", "percent": 42, "resetsAt": None}}}
    _, windows = opencode.parse_opencode(data, NOW)
    assert windows[0].used_percent == pytest.approx(42.0)


@pytest.mark.parametrize("percent,expected", [
    (150, 100.0),
    (100, 100.0),
    (0, 0.0),
    (-5, 0.0),
])
def test_parse_opencode_percent_is_clamped(percent, expected):
    data = {"usage": {"monthly": {"percent": percent, "resetsAt": None}}}
    _, windows = opencode.parse_opencode(data, NOW)
    assert windows[0].used_percent == pytest.approx(expected)


@pytest.mark.parametrize("data", [
    [],
    "usage",
    {"usage": []},
    {"usage": "not-a-dict"},
    {"usage": {"rolling": "not-a-dict"}},
    {"usage": {"weekly": []}},
])
def test_parse_opencode_non_dict_response_is_error(data):
    with pytest.raises(ValueError):
        opencode.parse_opencode(data, NOW)


def test_parse_opencode_skips_missing_meters():
    data = {"usage": {"weekly": {"percent": 12, "resetsAt": None}}}
    _, windows = opencode.parse_opencode(data, NOW)
    assert [w.key for w in windows] == ["weekly"]
    assert windows[0].resets_at is None

    data = {"usage": {"rolling": {"percent": 0}, "monthly": None}}
    _, windows = opencode.parse_opencode(data, NOW)
    assert [w.key for w in windows] == ["5h"]


@pytest.mark.parametrize("data", [
    {},
    {"usage": {}},
    {"usage": None},
    {"usage": {"rolling": None, "weekly": None, "monthly": None}},
])
def test_parse_opencode_no_meters_is_error(data):
    with pytest.raises(ValueError):
        opencode.parse_opencode(data, NOW)


@pytest.mark.parametrize("percent", ["2", None, True, float("nan"), float("inf")])
def test_parse_opencode_invalid_percent_is_error(percent):
    data = {"usage": {"rolling": {"percent": percent, "resetsAt": None}}}
    with pytest.raises(ValueError):
        opencode.parse_opencode(data, NOW)


# --------------------------------------------------------- key lookup order

def test_keychain_takes_priority(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: "keychain-dummy")
    monkeypatch.setenv("OPENCODE_API_KEY", "env-dummy")
    _auth_file(monkeypatch, tmp_path, {"opencode-go": {"key": "auth-dummy"}})

    key, source = opencode.locate_opencode_key()
    assert key == "keychain-dummy"
    assert source == "キーチェーン quota-canvas/opencode"


def test_env_is_used_when_keychain_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: None)
    monkeypatch.setenv("OPENCODE_API_KEY", "env-dummy")
    _auth_file(monkeypatch, tmp_path, {"opencode-go": {"key": "auth-dummy"}})

    key, source = opencode.locate_opencode_key()
    assert key == "env-dummy"
    assert source == "環境変数 OPENCODE_API_KEY"


def test_blank_env_falls_back_to_auth_file(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: None)
    monkeypatch.setenv("OPENCODE_API_KEY", " ")
    _auth_file(monkeypatch, tmp_path, {"opencode-go": {"key": "auth-dummy"}})

    key, source = opencode.locate_opencode_key()
    assert key == "auth-dummy"
    assert source == "~/.local/share/opencode/auth.json"


@pytest.mark.parametrize("payload", [
    {},                                     # エントリが無い
    {"opencode": {"key": "other-dummy"}},   # 別サービスのみ
    {"opencode-go": None},
    {"opencode-go": {"key": None}},
    {"opencode-go": {"key": "   "}},
    {"opencode-go": {"key": 42}},
])
def test_auth_file_without_usable_key_is_error(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: None)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    _auth_file(monkeypatch, tmp_path, payload)
    with pytest.raises(CredentialError):
        opencode.locate_opencode_key()


def test_missing_or_broken_auth_file_is_error(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: None)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)

    monkeypatch.setattr(opencode, "_auth_file", lambda: tmp_path / "missing.json")
    with pytest.raises(CredentialError):
        opencode.locate_opencode_key()

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(opencode, "_auth_file", lambda: broken)
    with pytest.raises(CredentialError):
        opencode.locate_opencode_key()


# ------------------------------------------------------------- detection

def test_detect_uses_keychain_without_network(monkeypatch):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: "keychain-dummy")

    def no_network(*args, **kwargs):
        raise AssertionError("detect must not make a request")

    monkeypatch.setattr(opencode, "http_get_json", no_network)
    detection = opencode.detect()
    assert detection.detected is True
    assert "OpenCode Go" in detection.source
    assert "キーチェーン" in detection.source
    assert detection.error is None


def test_detect_reports_missing_key_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: None)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(opencode, "_auth_file", lambda: tmp_path / "missing.json")

    def no_network(*args, **kwargs):
        raise AssertionError("detect must not make a request")

    monkeypatch.setattr(opencode, "http_get_json", no_network)
    detection = opencode.detect()
    assert detection.detected is False
    assert detection.source is None
    assert detection.error == "API キーが未設定"


# ------------------------------------------------------------------ fetch

def test_fetch_requests_usage_with_bearer_token(monkeypatch):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: "keychain-dummy")
    captured = {}

    def fake_http(url, headers, timeout=providers.TIMEOUT):
        captured.update(url=url, headers=headers)
        return USAGE

    monkeypatch.setattr(opencode, "http_get_json", fake_http)
    usage = opencode.fetch(NOW)

    assert captured["url"] == "https://opencode.ai/zen/go/v1/usage"
    assert captured["headers"]["Authorization"] == "Bearer keychain-dummy"
    assert captured["headers"]["User-Agent"] == "quota-canvas"
    assert usage.provider == "opencode"
    assert usage.name == "OpenCode Go"
    assert usage.error is None
    assert [w.id for w in usage.windows] == [
        "opencode:5h", "opencode:weekly", "opencode:30d"]
    assert usage.fetched_at == NOW


def test_fetch_propagates_http_errors(monkeypatch):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: "keychain-dummy")

    def unauthorized(url, headers, timeout=providers.TIMEOUT):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(opencode, "http_get_json", unauthorized)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        opencode.fetch(NOW)
    assert excinfo.value.code == 401


# ---------------------------------------------------------------- provider

def test_provider_metadata():
    assert opencode.PROVIDER.id == "opencode"
    assert opencode.PROVIDER.name == "OpenCode"
    assert opencode.PROVIDER.display_name == "OpenCode Go"
    assert opencode.PROVIDER.needs_key is True
    assert providers.REGISTRY["opencode"] is opencode.PROVIDER
