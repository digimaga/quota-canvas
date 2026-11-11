"""OpenDesign provider: parser, credential lookup order and offline detect.

Dummy values only; no network or real credentials.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.providers import CredentialError, opendesign

NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc

SNAPSHOT = {
    "plan": {"tier": "go", "status": "active",
             "currentPeriodEnd": "2026-02-01T00:00:00.000Z"},
    "wallet": {"balanceUsd": "2.5000"},
    "designPlanWindows": [
        {"kind": "5h", "windowStart": "2026-01-01T00:00:00.000Z",
         "resetsAt": "2026-01-01T05:00:00.000Z",
         "remainingCredits": "40000", "limitCredits": "50000"},
        {"kind": "7d", "windowStart": "2026-01-01T00:00:00.000Z",
         "resetsAt": "2026-01-08T00:00:00.000Z",
         "remainingCredits": "100000", "limitCredits": "150000"},
    ],
}


def _config_file(monkeypatch, tmp_path, payload):
    """Write payload as {AMR_HOME}/config.json and point AMR_HOME at it."""
    home = tmp_path / "amr-home"
    home.mkdir(exist_ok=True)
    path = home / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("AMR_HOME", str(home))
    return path


def _config_payload(control_key="vela-dummy",
                    api_url="https://amr-api.open-design.ai",
                    profile="prod"):
    entry = {}
    if control_key is not None:
        entry["controlKey"] = control_key
    if api_url is not None:
        entry["apiUrl"] = api_url
    return {"profiles": {profile: entry}}


# --------------------------------------------------------------- parser

def test_parse_opendesign_windows():
    name, windows, balances = opendesign.parse_opendesign(SNAPSHOT, NOW)
    assert name == "OpenDesign Go"
    assert [w.key for w in windows] == ["5h", "weekly"]

    five = windows[0]
    assert five.provider == "opendesign"
    assert five.id == "opendesign:5h"
    assert five.label == "5時間"
    assert five.used_percent == pytest.approx(20.0)  # (50000-40000)/50000*100
    assert five.resets_at == datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)
    assert five.length == timedelta(hours=5)

    week = windows[1]
    assert week.id == "opendesign:weekly"
    assert week.label == "週間"
    assert week.used_percent == pytest.approx((150000 - 100000) / 150000 * 100)
    assert week.resets_at == datetime(2026, 1, 8, 0, 0, 0, tzinfo=UTC)
    assert week.length == timedelta(days=7)

    assert len(balances) == 1
    assert balances[0].currency == "USD"
    assert balances[0].amount == pytest.approx(2.5)


def test_parse_opendesign_percent_from_credits():
    data = {"designPlanWindows": [
        {"kind": "5h", "resetsAt": None,
         "remainingCredits": "25000", "limitCredits": "50000"},
    ]}
    _, windows, balances = opendesign.parse_opendesign(data, NOW)
    assert windows[0].used_percent == pytest.approx(50.0)
    assert balances == []


@pytest.mark.parametrize("remaining,limit,expected", [
    ("0", "100", 100.0),       # 使い切り
    ("100", "100", 0.0),       # 未使用
    ("150", "100", 0.0),       # 残りが上限超過 → 0% に丸める
    ("-50", "100", 100.0),     # 負の残り → 100% に丸める
])
def test_parse_opendesign_percent_is_clamped(remaining, limit, expected):
    data = {"designPlanWindows": [
        {"kind": "7d", "resetsAt": None,
         "remainingCredits": remaining, "limitCredits": limit},
    ]}
    _, windows, _ = opendesign.parse_opendesign(data, NOW)
    assert windows[0].used_percent == pytest.approx(expected)


def test_parse_opendesign_unknown_kind_is_ignored():
    data = {"designPlanWindows": [
        {"kind": "1m", "remainingCredits": "0", "limitCredits": "100"},
        {"kind": "5h", "resetsAt": None,
         "remainingCredits": "40000", "limitCredits": "50000"},
        {"kind": "monthly", "remainingCredits": "0", "limitCredits": "100"},
    ]}
    _, windows, _ = opendesign.parse_opendesign(data, NOW)
    assert [w.id for w in windows] == ["opendesign:5h"]


@pytest.mark.parametrize("data", [
    [],
    "snapshot",
    {"designPlanWindows": "not-a-list"},
    {"designPlanWindows": None},
    {"designPlanWindows": []},
    {"designPlanWindows": [{"kind": "1m", "remainingCredits": "0",
                            "limitCredits": "100"}]},  # 未知の kind のみ
    {"wallet": {"balanceUsd": "1.5"}},                  # 残高だけでは窓にならない
    {},
])
def test_parse_opendesign_no_windows_is_error(data):
    with pytest.raises(ValueError):
        opendesign.parse_opendesign(data, NOW)


@pytest.mark.parametrize("entry", [
    {"kind": "5h", "remainingCredits": "100"},                       # limit 無し
    {"kind": "5h", "remainingCredits": "100", "limitCredits": "0"},
    {"kind": "5h", "remainingCredits": "100", "limitCredits": "-5"},
    {"kind": "5h", "remainingCredits": "100", "limitCredits": "abc"},
    {"kind": "5h", "remainingCredits": "abc", "limitCredits": "100"},
    {"kind": "5h", "limitCredits": "100"},                           # remaining 無し
    {"kind": "5h", "remainingCredits": True, "limitCredits": "100"},
])
def test_parse_opendesign_invalid_credits_window_is_skipped(entry):
    """壊れた窓はその窓だけ飛ばす。有効な窓が残ればエラーにしない。"""
    data = {"designPlanWindows": [entry, {
        "kind": "7d", "resetsAt": None,
        "remainingCredits": "100000", "limitCredits": "150000",
    }]}
    _, windows, _ = opendesign.parse_opendesign(data, NOW)
    assert [w.id for w in windows] == ["opendesign:weekly"]


@pytest.mark.parametrize("entry", [
    {"kind": "5h", "remainingCredits": "100", "limitCredits": "0"},
    {"kind": "5h", "remainingCredits": "abc", "limitCredits": "100"},
])
def test_parse_opendesign_all_windows_invalid_is_error(entry):
    with pytest.raises(ValueError):
        opendesign.parse_opendesign({"designPlanWindows": [entry]}, NOW)


def test_parse_opendesign_duplicate_kind_keeps_first():
    data = {"designPlanWindows": [
        {"kind": "5h", "resetsAt": None,
         "remainingCredits": "40000", "limitCredits": "50000"},
        {"kind": "5h", "resetsAt": None,
         "remainingCredits": "0", "limitCredits": "50000"},
        {"kind": "7d", "resetsAt": None,
         "remainingCredits": "100000", "limitCredits": "150000"},
    ]}
    _, windows, _ = opendesign.parse_opendesign(data, NOW)
    assert [w.id for w in windows] == ["opendesign:5h", "opendesign:weekly"]
    assert windows[0].used_percent == pytest.approx(20.0)


def test_parse_opendesign_invalid_duplicate_does_not_block_valid():
    """先の壊れたエントリは飛ばし、あとの有効な同じ kind を採る。"""
    data = {"designPlanWindows": [
        {"kind": "5h", "remainingCredits": "abc", "limitCredits": "50000"},
        {"kind": "5h", "resetsAt": None,
         "remainingCredits": "40000", "limitCredits": "50000"},
    ]}
    _, windows, _ = opendesign.parse_opendesign(data, NOW)
    assert [w.id for w in windows] == ["opendesign:5h"]
    assert windows[0].used_percent == pytest.approx(20.0)


def test_parse_opendesign_non_dict_entry_is_skipped():
    data = {"designPlanWindows": [
        "junk",
        {"kind": "5h", "resetsAt": None,
         "remainingCredits": "50000", "limitCredits": "50000"},
    ]}
    _, windows, _ = opendesign.parse_opendesign(data, NOW)
    assert [w.key for w in windows] == ["5h"]


def test_parse_opendesign_null_resets_at():
    data = {"designPlanWindows": [
        {"kind": "7d", "resetsAt": None,
         "remainingCredits": "75000", "limitCredits": "150000"},
    ]}
    _, windows, _ = opendesign.parse_opendesign(data, NOW)
    assert windows[0].resets_at is None


@pytest.mark.parametrize("tier,name", [
    ("go", "OpenDesign Go"),
    ("pro", "OpenDesign Pro"),
    ("pro_plus", "OpenDesign Pro Plus"),
    ("go-plus", "OpenDesign Go Plus"),
    (None, "OpenDesign"),
    ("", "OpenDesign"),
    ("  ", "OpenDesign"),
])
def test_parse_opendesign_plan_tier_names_the_plan(tier, name):
    data = {"plan": {"tier": tier}, "designPlanWindows": [
        {"kind": "5h", "resetsAt": None,
         "remainingCredits": "50000", "limitCredits": "50000"},
    ]}
    parsed, _, _ = opendesign.parse_opendesign(data, NOW)
    assert parsed == name


def test_parse_opendesign_invalid_balance_is_dropped():
    data = {"wallet": {"balanceUsd": "not-a-number"}, "designPlanWindows": [
        {"kind": "5h", "resetsAt": None,
         "remainingCredits": "50000", "limitCredits": "50000"},
    ]}
    _, windows, balances = opendesign.parse_opendesign(data, NOW)
    assert len(windows) == 1
    assert balances == []


# --------------------------------------------------------- key lookup order

def test_env_control_key_takes_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")
    _config_file(monkeypatch, tmp_path, _config_payload("file-dummy"))

    key, api_url, source = opendesign.locate_opendesign_credentials()
    assert key == "env-dummy"
    assert api_url == opendesign.DEFAULT_API_URL
    assert source == "環境変数 VELA_CONTROL_KEY"


def test_env_api_url_is_used_when_https(monkeypatch):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")
    monkeypatch.setenv("VELA_API_URL", "https://amr-staging.example.test")

    _, api_url, _ = opendesign.locate_opendesign_credentials()
    assert api_url == "https://amr-staging.example.test"


@pytest.mark.parametrize("bad_url", [
    "http://insecure.example.test",       # https ではない
    "ftp://example.test",
    "not-a-url",
    "https:///path-without-host",         # ホスト無し
    "https://example.test:abc",           # 壊れたポート
    "https://user:pass@example.test",     # userinfo 付き
])
def test_non_https_api_url_is_credential_error(monkeypatch, tmp_path, bad_url):
    """https 以外・壊れた apiUrl は CredentialError (既定 URL へは戻さない)。
    controlKey が平文や別ホストへ送られないようにするため。"""
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")
    monkeypatch.setenv("VELA_API_URL", bad_url)
    with pytest.raises(CredentialError) as excinfo:
        opendesign.locate_opendesign_credentials()
    assert "https" in str(excinfo.value)

    monkeypatch.delenv("VELA_CONTROL_KEY")
    _config_file(monkeypatch, tmp_path, _config_payload(api_url=bad_url))
    with pytest.raises(CredentialError):
        opendesign.locate_opendesign_credentials()


def test_non_string_api_url_is_credential_error(monkeypatch, tmp_path):
    _config_file(monkeypatch, tmp_path, _config_payload(api_url=42))
    with pytest.raises(CredentialError):
        opendesign.locate_opendesign_credentials()


def test_missing_api_url_falls_back_to_default(monkeypatch, tmp_path):
    """apiUrl が無い・空のときだけ既定 URL に戻す。"""
    _config_file(monkeypatch, tmp_path, _config_payload(api_url=None))
    _, api_url, _ = opendesign.locate_opendesign_credentials()
    assert api_url == opendesign.DEFAULT_API_URL

    _config_file(monkeypatch, tmp_path, _config_payload(api_url="  "))
    _, api_url, _ = opendesign.locate_opendesign_credentials()
    assert api_url == opendesign.DEFAULT_API_URL


def test_env_api_url_without_control_key_is_ignored(monkeypatch, tmp_path):
    """VELA_API_URL だけあって VELA_CONTROL_KEY が無いとき、config.json の
    controlKey を VELA_API_URL へ送らない (config の apiUrl を使う)。"""
    monkeypatch.setenv("VELA_API_URL", "https://evil.example.test")
    _config_file(monkeypatch, tmp_path, _config_payload(
        "file-dummy", api_url="https://amr-corp.example.test"))

    key, api_url, _ = opendesign.locate_opendesign_credentials()
    assert key == "file-dummy"
    assert api_url == "https://amr-corp.example.test"

    calls = _patch_http(monkeypatch)
    opendesign.fetch(NOW)
    assert calls[0]["url"].startswith("https://amr-corp.example.test/")
    assert all("evil.example.test" not in c["url"] for c in calls)


def test_config_file_prod_profile_is_default(monkeypatch, tmp_path):
    _config_file(monkeypatch, tmp_path, _config_payload("vela-file-dummy"))

    key, api_url, source = opendesign.locate_opendesign_credentials()
    assert key == "vela-file-dummy"
    assert api_url == "https://amr-api.open-design.ai"
    assert "OpenDesign CLI のログイン" in source


def test_amr_profile_selects_profile(monkeypatch, tmp_path):
    _config_file(monkeypatch, tmp_path, {"profiles": {
        "prod": {"controlKey": "prod-dummy"},
        "staging": {"controlKey": "staging-dummy"},
    }})
    monkeypatch.setenv("AMR_PROFILE", "staging")

    key, _, _ = opendesign.locate_opendesign_credentials()
    assert key == "staging-dummy"


def test_sole_profile_is_used_when_no_prod(monkeypatch, tmp_path):
    _config_file(monkeypatch, tmp_path, _config_payload("solo-dummy",
                                                        profile="staging"))
    key, _, _ = opendesign.locate_opendesign_credentials()
    assert key == "solo-dummy"


def test_multiple_profiles_without_prod_is_error(monkeypatch, tmp_path):
    _config_file(monkeypatch, tmp_path, {"profiles": {
        "a": {"controlKey": "a-dummy"},
        "b": {"controlKey": "b-dummy"},
    }})
    with pytest.raises(CredentialError):
        opendesign.locate_opendesign_credentials()


@pytest.mark.parametrize("payload", [
    {},                                        # profiles が無い
    {"profiles": {}},
    {"profiles": "not-a-dict"},
    {"profiles": {"prod": None}},
    {"profiles": {"prod": {"controlKey": None}}},
    {"profiles": {"prod": {"controlKey": "   "}}},
    {"profiles": {"prod": {"controlKey": 42}}},
    {"profiles": {"other": {"controlKey": "x"}, "prod": {}}},
])
def test_config_without_usable_key_is_error(monkeypatch, tmp_path, payload):
    _config_file(monkeypatch, tmp_path, payload)
    with pytest.raises(CredentialError) as excinfo:
        opendesign.locate_opendesign_credentials()
    assert "vela login" in str(excinfo.value)


def test_missing_or_broken_config_file_is_error(monkeypatch, tmp_path):
    home = tmp_path / "amr-home"
    home.mkdir()
    monkeypatch.setenv("AMR_HOME", str(home))   # config.json が無い
    with pytest.raises(CredentialError):
        opendesign.locate_opendesign_credentials()

    (home / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CredentialError):
        opendesign.locate_opendesign_credentials()


def test_amr_home_tilde_is_expanded(monkeypatch, tmp_path):
    home = tmp_path / "amr-home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(_config_payload("tilde-dummy")), encoding="utf-8")
    monkeypatch.setenv("AMR_HOME", "~/amr-home")
    monkeypatch.setenv("HOME", str(tmp_path))

    key, _, _ = opendesign.locate_opendesign_credentials()
    assert key == "tilde-dummy"


# ------------------------------------------------------------- detection

def test_detect_uses_config_file_without_network(monkeypatch, tmp_path):
    _config_file(monkeypatch, tmp_path, _config_payload())

    def no_network(*args, **kwargs):
        raise AssertionError("detect must not make a request")

    monkeypatch.setattr(opendesign, "http_get_json", no_network)
    detection = opendesign.detect()
    assert detection.detected is True
    assert "OpenDesign CLI のログイン" in detection.source
    assert detection.error is None


def test_detect_reports_missing_login_without_network(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("detect must not make a request")

    monkeypatch.setattr(opendesign, "http_get_json", no_network)
    detection = opendesign.detect()
    assert detection.detected is False
    assert detection.source is None
    assert "vela login" in detection.error


# ------------------------------------------------------------------ fetch

def _patch_http(monkeypatch, snapshot=SNAPSHOT,
                current=None, workspace_id="ws-dummy"):
    """http_get_json を workspaces/current → billing/snapshot の順に返す偽物に。"""
    calls = []
    if current is None:
        current = {"workspaceId": workspace_id}

    def fake_http(url, headers, timeout=providers.TIMEOUT):
        calls.append({"url": url, "headers": headers})
        if url.endswith("/api/v1/workspaces/current"):
            return current
        return snapshot

    monkeypatch.setattr(opendesign, "http_get_json", fake_http)
    return calls


def test_fetch_requests_workspace_then_snapshot(monkeypatch):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")
    calls = _patch_http(monkeypatch)

    usage = opendesign.fetch(NOW)

    assert calls[0]["url"] == (
        "https://amr-api.open-design.ai/api/v1/workspaces/current")
    assert calls[1]["url"] == (
        "https://amr-api.open-design.ai/api/v1/workspaces/ws-dummy"
        "/billing/snapshot")
    for call in calls:
        assert call["headers"]["Authorization"] == "Bearer env-dummy"
        assert call["headers"]["Accept"] == "application/json"
        assert call["headers"]["User-Agent"] == "quota-canvas"
    assert usage.provider == "opendesign"
    assert usage.name == "OpenDesign Go"
    assert usage.error is None
    assert [w.id for w in usage.windows] == [
        "opendesign:5h", "opendesign:weekly"]
    assert len(usage.balances) == 1
    assert usage.balances[0].currency == "USD"
    assert usage.balances[0].amount == pytest.approx(2.5)
    assert usage.fetched_at == NOW


def test_fetch_url_quotes_workspace_id(monkeypatch):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")
    calls = _patch_http(monkeypatch, workspace_id="ws id/a+b")

    opendesign.fetch(NOW)
    assert calls[1]["url"] == (
        "https://amr-api.open-design.ai/api/v1/workspaces/"
        "ws%20id%2Fa%2Bb/billing/snapshot")


@pytest.mark.parametrize("code", [401, 403])
def test_fetch_auth_error_is_credential_error(monkeypatch, code):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")

    def unauthorized(url, headers, timeout=providers.TIMEOUT):
        raise urllib.error.HTTPError(url, code, "Denied", None, None)

    monkeypatch.setattr(opendesign, "http_get_json", unauthorized)
    with pytest.raises(CredentialError) as excinfo:
        opendesign.fetch(NOW)
    assert "vela login" in str(excinfo.value)


def test_fetch_propagates_other_http_errors(monkeypatch):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")

    def server_error(url, headers, timeout=providers.TIMEOUT):
        raise urllib.error.HTTPError(url, 500, "Server Error", None, None)

    monkeypatch.setattr(opendesign, "http_get_json", server_error)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        opendesign.fetch(NOW)
    assert excinfo.value.code == 500


def test_fetch_auth_error_on_snapshot_is_credential_error(monkeypatch):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")
    calls = []

    def fake_http(url, headers, timeout=providers.TIMEOUT):
        calls.append(url)
        if url.endswith("/api/v1/workspaces/current"):
            return {"workspaceId": "ws-dummy"}
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(opendesign, "http_get_json", fake_http)
    with pytest.raises(CredentialError):
        opendesign.fetch(NOW)
    assert len(calls) == 2


@pytest.mark.parametrize("current", [
    {},
    {"workspaceId": None},
    {"workspaceId": "  "},
    {"workspaceId": 42},
    "junk",
])
def test_fetch_invalid_workspace_is_error(monkeypatch, current):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")
    _patch_http(monkeypatch, current=current)
    with pytest.raises(ValueError):
        opendesign.fetch(NOW)


def test_fetch_uses_profile_api_url(monkeypatch, tmp_path):
    _config_file(monkeypatch, tmp_path, _config_payload(
        "file-dummy", api_url="https://amr-staging.example.test"))
    calls = _patch_http(monkeypatch)

    opendesign.fetch(NOW)
    assert calls[0]["url"] == (
        "https://amr-staging.example.test/api/v1/workspaces/current")


def test_fetch_keeps_api_url_port_and_path(monkeypatch, tmp_path):
    """apiUrl のポート・パスは保持し、末尾の / は除いてエンドポイントを付ける。"""
    _config_file(monkeypatch, tmp_path, _config_payload(
        "file-dummy", api_url="https://amr.example.test:8443/prefix/"))
    calls = _patch_http(monkeypatch)

    opendesign.fetch(NOW)
    assert calls[0]["url"] == (
        "https://amr.example.test:8443/prefix/api/v1/workspaces/current")
    assert calls[1]["url"] == (
        "https://amr.example.test:8443/prefix/api/v1/workspaces/ws-dummy"
        "/billing/snapshot")


def test_fetch_non_dict_snapshot_is_error(monkeypatch):
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")
    _patch_http(monkeypatch, snapshot=["not", "a", "dict"])
    with pytest.raises(ValueError):
        opendesign.fetch(NOW)


def test_fetch_non_json_snapshot_is_error(monkeypatch):
    """snapshot の応答が JSON でない (decode に失敗する) ときも失敗扱い。"""
    monkeypatch.setenv("VELA_CONTROL_KEY", "env-dummy")

    def fake_http(url, headers, timeout=providers.TIMEOUT):
        if url.endswith("/api/v1/workspaces/current"):
            return {"workspaceId": "ws-dummy"}
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    monkeypatch.setattr(opendesign, "http_get_json", fake_http)
    with pytest.raises(json.JSONDecodeError):
        opendesign.fetch(NOW)


# ---------------------------------------------------------------- provider

def test_provider_metadata():
    assert opendesign.PROVIDER.id == "opendesign"
    assert opendesign.PROVIDER.name == "OpenDesign"
    assert opendesign.PROVIDER.needs_key is False
    assert providers.REGISTRY["opendesign"] is opendesign.PROVIDER
