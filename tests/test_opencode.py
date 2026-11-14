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
        "opencode:5h", "opencode:weekly", "opencode:30d", "opencode:free"]
    assert usage.fetched_at == NOW


def test_fetch_propagates_http_errors(monkeypatch):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: "keychain-dummy")

    def unauthorized(url, headers, timeout=providers.TIMEOUT):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(opencode, "http_get_json", unauthorized)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        opencode.fetch(NOW)
    assert excinfo.value.code == 401


# ------------------------------------------------------ free-model limit
# opencode-run が書く「無料枠の利用制限の戻る時刻」(epoch 秒) のファイル。

FREE_RESET = NOW + timedelta(hours=19)
FREE_LIMIT_TEXT = "%d" % int(FREE_RESET.timestamp())


def _free_file(monkeypatch, tmp_path, text):
    path = tmp_path / "opencode-free-exhausted"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(opencode.FREE_LIMIT_ENV, str(path))
    return path


def test_free_limit_window_while_limited(monkeypatch, tmp_path):
    _free_file(monkeypatch, tmp_path, FREE_LIMIT_TEXT + "\n")
    w = opencode.free_limit_window(NOW)
    assert w.id == "opencode:free"
    assert w.used_percent == 100.0
    assert w.resets_at == FREE_RESET
    assert w.length == FREE_RESET - NOW


@pytest.mark.parametrize("text", ["", "abc", "-5", "1794549690x", "²", "9" * 5000])
def test_free_limit_window_broken_file_is_zero(monkeypatch, tmp_path, text):
    _free_file(monkeypatch, tmp_path, text)
    w = opencode.free_limit_window(NOW)
    assert w.id == "opencode:free"
    assert w.used_percent == 0.0
    assert w.resets_at is None
    assert w.length == opencode.FREE_WINDOW_LENGTH


def test_free_limit_window_reset_exactly_now_is_zero(monkeypatch, tmp_path):
    # 戻る時刻ちょうどは「過ぎている」側 (制限なし) として扱う。
    _free_file(monkeypatch, tmp_path, FREE_LIMIT_TEXT)
    w = opencode.free_limit_window(FREE_RESET)
    assert w.used_percent == 0.0 and w.resets_at is None
    w = opencode.free_limit_window(FREE_RESET - timedelta(seconds=1))
    assert w.used_percent == 100.0 and w.resets_at == FREE_RESET


def test_free_limit_window_missing_file_or_past_is_zero(monkeypatch, tmp_path):
    monkeypatch.setenv(opencode.FREE_LIMIT_ENV, str(tmp_path / "none"))
    w = opencode.free_limit_window(NOW)
    assert w.used_percent == 0.0 and w.resets_at is None
    _free_file(monkeypatch, tmp_path, FREE_LIMIT_TEXT)
    w = opencode.free_limit_window(FREE_RESET + timedelta(seconds=1))
    assert w.used_percent == 0.0 and w.resets_at is None


def test_fetch_always_adds_free_window(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode.keychain, "read", lambda account: "keychain-dummy")
    monkeypatch.setattr(opencode, "http_get_json",
                        lambda url, headers, timeout=providers.TIMEOUT: USAGE)
    usage = opencode.fetch(NOW)
    free = [w for w in usage.windows if w.id == "opencode:free"]
    assert len(free) == 1 and free[0].used_percent == 0.0 and free[0].resets_at is None
    _free_file(monkeypatch, tmp_path, FREE_LIMIT_TEXT)
    usage = opencode.fetch(NOW)
    free = [w for w in usage.windows if w.id == "opencode:free"]
    assert len(free) == 1 and free[0].used_percent == 100.0
    assert free[0].resets_at == FREE_RESET


def test_free_window_is_routing_only(monkeypatch, tmp_path):
    from quotacanvas.cli import all_window_paces, build_cards
    from quotacanvas.history import History
    from quotacanvas.models import ProviderUsage

    _free_file(monkeypatch, tmp_path, FREE_LIMIT_TEXT)
    _, windows = opencode.parse_opencode(USAGE, NOW)
    windows.append(opencode.free_limit_window(NOW))
    usage = ProviderUsage("opencode", "OpenCode Go", windows, None, NOW)
    history = History(tmp_path / "history")
    shown = [pace.window.id for _, pace in build_cards([usage], history, NOW) if pace]
    assert "opencode:free" not in shown and "opencode:5h" in shown
    named = [pace.window.id for _, pace in
             build_cards([usage], history, NOW, order=["opencode:free"]) if pace]
    assert named == ["opencode:free"]
    every = [pace.window.id for _, pace in all_window_paces([usage], history, NOW) if pace]
    assert "opencode:free" in every


def test_ocfree_is_stop_while_free_limited(monkeypatch, tmp_path):
    from quotacanvas.cli import all_window_paces
    from quotacanvas.config import Routing
    from quotacanvas.gate import evaluate
    from quotacanvas.history import History
    from quotacanvas.models import ProviderUsage
    from quotacanvas.status import _window_item

    _free_file(monkeypatch, tmp_path, FREE_LIMIT_TEXT)
    _, windows = opencode.parse_opencode(USAGE, NOW)
    windows.append(opencode.free_limit_window(NOW))
    usage = ProviderUsage("opencode", "OpenCode Go", windows, None, NOW)
    history = History(tmp_path / "history")
    items = [_window_item(u, p) for u, p in all_window_paces([usage], history, NOW) if p]
    data = {"updated_at": NOW.isoformat(), "windows": items}
    routing = Routing(workers={"ocfree": ["opencode:free"], "ocpaid": ["opencode:*"]})
    result = evaluate(data, routing, {}, NOW)
    assert result.worker("ocfree").verdict == "stop"
    # 有料の窓 ("opencode:*") は無料枠の制限に巻き込まない
    paid = result.worker("ocpaid")
    assert paid.verdict != "stop"
    assert "opencode:free" not in paid.windows

    # 制限が無ければ 0% の窓が出て、unknown ではなく通常判定になる
    no_limit = opencode.free_limit_window(FREE_RESET + timedelta(seconds=1))
    usage_free = ProviderUsage("opencode", "OpenCode Go", windows[:-1] + [no_limit], None, NOW)
    items_free = [_window_item(u, p) for u, p in
                  all_window_paces([usage_free], history, NOW) if p]
    data_free = {"updated_at": NOW.isoformat(), "windows": items_free}
    oc = evaluate(data_free, routing, {}, NOW).worker("ocfree")
    assert oc.verdict in ("go", "boost")


def test_free_window_survives_api_failure_and_skips_recommend(monkeypatch, tmp_path):
    """API の取得失敗で前回値 (stale) を出していても、制限中なら ocfree は stop。
    「今使う先」の推奨は opencode:free で OpenCode を使えない扱いにしない。"""
    from dataclasses import replace

    from quotacanvas.cli import all_window_paces
    from quotacanvas.config import Routing
    from quotacanvas.gate import evaluate
    from quotacanvas.history import History
    from quotacanvas.models import ProviderUsage
    from quotacanvas.recommend import recommend
    from quotacanvas.status import _window_item

    _, windows = opencode.parse_opencode(USAGE, NOW)
    prev = ProviderUsage("opencode", "OpenCode Go", windows, None, NOW)
    # 前回値には制限の窓が無く、今のファイルでは制限中
    _free_file(monkeypatch, tmp_path, FREE_LIMIT_TEXT)
    stale = replace(prev, stale_error="HTTP 500")
    u = opencode.with_free_limit(stale, NOW)
    assert [w for w in u.windows if w.id == "opencode:free"][0].used_percent == 100.0
    history = History(tmp_path / "history")
    items = [_window_item(x, p) for x, p in all_window_paces([u], history, NOW) if p]
    data = {"updated_at": NOW.isoformat(), "windows": items,
            "errors": [{"provider": "opencode", "error": "HTTP 500"}]}
    routing = Routing(workers={"ocfree": ["opencode:free"], "ocpaid": ["opencode:*"]})
    result = evaluate(data, routing, {}, NOW)
    assert result.worker("ocfree").verdict == "stop"
    assert result.worker("ocpaid").verdict == "unknown"  # 有料枠は取得失敗のまま

    # 制限が明けたら 0%・リセットなしの窓になり、判定は go / boost に戻る
    monkeypatch.setenv(opencode.FREE_LIMIT_ENV, str(tmp_path / "none"))
    cleared = opencode.with_free_limit(u, FREE_RESET)
    free = [w for w in cleared.windows if w.id == "opencode:free"]
    assert len(free) == 1 and free[0].used_percent == 0.0 and free[0].resets_at is None
    items_cleared = [_window_item(x, p) for x, p in
                     all_window_paces([cleared], history, FREE_RESET) if p]
    data_cleared = {"updated_at": FREE_RESET.isoformat(), "windows": items_cleared,
                    "errors": [{"provider": "opencode", "error": "HTTP 500"}]}
    oc = evaluate(data_cleared, routing, {}, FREE_RESET).worker("ocfree")
    assert oc.verdict in ("go", "boost")

    # 推奨の材料にしない: 窓の一覧から opencode:free を除いた場合と同じ結果
    ok_usage = opencode.with_free_limit(prev, NOW)
    with_free = recommend([ok_usage], history, NOW)
    without = recommend([prev], history, NOW)
    assert with_free == without


def test_with_free_limit_leaves_error_card_alone(monkeypatch, tmp_path):
    from quotacanvas.models import ProviderUsage

    _free_file(monkeypatch, tmp_path, FREE_LIMIT_TEXT)
    err = ProviderUsage("opencode", "OpenCode Go", [], "HTTP 500", NOW)
    assert opencode.with_free_limit(err, NOW) is err
    other = ProviderUsage("glm", "GLM", [], None, NOW)
    assert opencode.with_free_limit(other, NOW) is other


def test_config_accepts_opencode_free_window():
    from quotacanvas.config import config_from_dict

    cfg = config_from_dict({"routing": {"workers": {"ocfree": ["opencode:free"]}}})
    assert cfg.routing.workers == {"ocfree": ["opencode:free"]}


# ---------------------------------------------------------------- provider

def test_provider_metadata():
    assert opencode.PROVIDER.id == "opencode"
    assert opencode.PROVIDER.name == "OpenCode"
    assert opencode.PROVIDER.display_name == "OpenCode Go"
    assert opencode.PROVIDER.needs_key is True
    assert providers.REGISTRY["opencode"] is opencode.PROVIDER
