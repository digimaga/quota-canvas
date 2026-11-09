"""Muse provider: Action ID resolution, RSC parser and offline detect.

Dummy values only; no network or real credentials.
"""

from __future__ import annotations

import itertools
import json
import time
import types
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.providers import CredentialError, muse

NOW = datetime(2026, 11, 9, 6, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc

COOKIE = "session=dummy-cookie-value"          # 明らかな偽物
ACTION_ID = "0" * 40                            # 40 桁 hex の偽物
RESOLVED_ID = "f" * 40                          # 再解決で見つかる別の偽物
RESETS_AT = 1830000000                          # epoch 秒の偽物

SUBSCRIPTION = {
    "subscriptionType": "PAID",
    "tier": {"tierId": "00000000000000000", "name": "ダミープラン",
             "tierCode": "PRODUCT_TIER_9", "isPaid": True, "rank": 90},
    "usage": {"state": "METERED", "percentUsed": 42, "resetsAt": RESETS_AT,
              "quotaStatus": "SUFFICIENT"},
    "topupBalance": 300, "topupTotal": 1000,
    "topupRowLabel": "追加のトークン",
    "topupRowValueLabel": "70%使用済み(残り300トークン)",
}


def _rsc(subscription, prefix_lines=None, suffix_lines=None):
    lines = list(prefix_lines) if prefix_lines else \
        ['0:{"a":"$@1","q":"","i":false}']
    lines.append('1:' + json.dumps({"success": True,
                                    "subscription": subscription}))
    lines.extend(suffix_lines or [])
    return "\n".join(lines)


def _html(*paths):
    return "".join('<script src="%s"></script>' % p for p in paths)


def _js_with_action(action_id, extra=""):
    return ('var m=(0,t.createServerReference)("%s",t.callServer,void 0,'
            't.findSourceMapURL,"fetchSubscriptionAction");' % action_id) + extra


def _install_http(monkeypatch, pages, posts=None):
    """ダミー HTTP。pages: url -> str 応答 (Exception なら raise、無ければ 404)。
    posts: _post_action の応答キュー (str または Exception)。記録を返す。"""
    calls = {"get": [], "post": []}
    posts = list(posts or [])

    def fake_get(url, headers, timeout=muse.TIMEOUT, deadline=None):
        calls["get"].append((url, headers, timeout))
        response = pages.get(url)
        if response is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if isinstance(response, Exception):
            raise response
        return response

    def fake_post(url, headers, body, timeout=muse.TIMEOUT):
        calls["post"].append((url, headers, body))
        if not posts:
            raise AssertionError("unexpected POST")
        response = posts.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(muse, "_get_text", fake_get)
    monkeypatch.setattr(muse, "_post_text", fake_post)
    return calls


def _cookie_keychain(monkeypatch):
    monkeypatch.setattr(muse.keychain, "read", lambda account: COOKIE)


def _http_error(code):
    return urllib.error.HTTPError("https://muse.ai/", code, "err", None, None)


# --------------------------------------------------------------- parser

def test_parse_muse_windows():
    name, windows, balances = muse.parse_muse(_rsc(SUBSCRIPTION), NOW)
    assert name == "Muse ダミープラン"
    assert [w.key for w in windows] == ["weekly"]

    weekly = windows[0]
    assert weekly.provider == "muse"
    assert weekly.id == "muse:weekly"
    assert weekly.label == "週間"
    assert weekly.used_percent == pytest.approx(42.0)
    assert weekly.resets_at == datetime.fromtimestamp(RESETS_AT, tz=UTC)
    assert weekly.length == timedelta(days=7)

    # 追加トークンは窓ではなく残高行 (金額ではなく text で出す)
    assert len(balances) == 1
    topup = balances[0]
    assert topup.currency == "TOKENS"
    assert topup.amount == pytest.approx(30.0)  # 残り % = 300/1000
    assert topup.label == "追加のトークン"
    assert topup.text == "残り 30%"


def test_parse_muse_picks_the_success_line():
    lines = [
        '0:{"a":"$@1","q":"","i":false}',
        '1:{"success":false,"error":"dummy"}',
        '2:{"success":true,"subscription":%s}' % json.dumps(SUBSCRIPTION),
        "not json",
    ]
    name, windows, _ = muse.parse_muse("\n".join(lines), NOW)
    assert name == "Muse ダミープラン"
    assert windows[0].id == "muse:weekly"


def test_parse_muse_hex_line_ids():
    # React Flight の行頭 ID は 16 進数 ("a:", "1f:" など) も受理する
    lines = [
        'a:{"a":"$@1","q":"","i":false}',
        '1f:{"success":true,"subscription":%s}' % json.dumps(SUBSCRIPTION),
    ]
    name, windows, _ = muse.parse_muse("\n".join(lines), NOW)
    assert name == "Muse ダミープラン"
    assert windows[0].id == "muse:weekly"


def test_parse_muse_unlimited_is_zero_percent():
    sub = dict(SUBSCRIPTION, usage={"state": "UNLIMITED", "percentUsed": 33,
                                    "resetsAt": RESETS_AT})
    _, windows, _ = muse.parse_muse(_rsc(sub), NOW)
    weekly = next(w for w in windows if w.key == "weekly")
    assert weekly.used_percent == pytest.approx(0.0)


def test_parse_muse_null_percent_hides_weekly():
    # percentUsed null → 週間窓なし。topup だけなら残高だけの usage になる
    sub = dict(SUBSCRIPTION, usage={"state": "METERED", "percentUsed": None,
                                    "resetsAt": RESETS_AT})
    _, windows, balances = muse.parse_muse(_rsc(sub), NOW)
    assert windows == []
    assert len(balances) == 1
    assert balances[0].currency == "TOKENS"


def test_parse_muse_missing_usage_keeps_topup_only():
    sub = dict(SUBSCRIPTION)
    del sub["usage"]
    _, windows, balances = muse.parse_muse(_rsc(sub), NOW)
    assert windows == []
    assert len(balances) == 1


def test_parse_muse_no_windows_is_error():
    sub = dict(SUBSCRIPTION, usage={"state": "METERED", "percentUsed": None})
    del sub["topupBalance"], sub["topupTotal"]
    with pytest.raises(ValueError):
        muse.parse_muse(_rsc(sub), NOW)


@pytest.mark.parametrize("percent", ["42", True, float("nan"), float("inf")])
def test_parse_muse_invalid_percent_is_error(percent):
    sub = dict(SUBSCRIPTION, usage={"state": "METERED",
                                    "percentUsed": percent})
    with pytest.raises(ValueError):
        muse.parse_muse(_rsc(sub), NOW)


@pytest.mark.parametrize("percent,expected", [
    (0, 0.0), (150, 100.0), (-5, 0.0), (100, 100.0), (120, 100.0),
])
def test_parse_muse_percent_is_clamped(percent, expected):
    sub = dict(SUBSCRIPTION, usage={"state": "METERED",
                                    "percentUsed": percent})
    _, windows, _ = muse.parse_muse(_rsc(sub), NOW)
    weekly = next(w for w in windows if w.key == "weekly")
    assert weekly.used_percent == pytest.approx(expected)


@pytest.mark.parametrize("reset", ["abc", "", "not-a-number", None])
def test_parse_muse_invalid_reset_is_missing(reset):
    sub = dict(SUBSCRIPTION, usage={"state": "METERED", "percentUsed": 40,
                                    "resetsAt": reset})
    _, windows, _ = muse.parse_muse(_rsc(sub), NOW)
    weekly = next(w for w in windows if w.key == "weekly")
    assert weekly.used_percent == pytest.approx(40.0)
    assert weekly.resets_at is None


@pytest.mark.parametrize("total,balance,expected", [
    (1000, 300, 30.0),      # 残り 30%
    (1000, 0, 0.0),         # 使い切り
    (1000, 1500, 100.0),    # 残量が総量を超える異常値は 100% に丸める
    (1000, -5, 0.0),        # 負の残量は 0% に丸める
])
def test_parse_muse_topup_left_percent(total, balance, expected):
    sub = dict(SUBSCRIPTION, topupTotal=total, topupBalance=balance)
    _, _, balances = muse.parse_muse(_rsc(sub), NOW)
    assert balances[0].amount == pytest.approx(expected)
    assert balances[0].text == "残り %s%%" % f"{expected:g}"


@pytest.mark.parametrize("total,balance", [
    (0, 0), (None, 300), ("abc", 300), (True, 300), (float("inf"), 300),
    (1000, None), (1000, "abc"), (1000, True), (1000, float("nan")),
])
def test_parse_muse_invalid_topup_is_skipped(total, balance):
    sub = dict(SUBSCRIPTION, topupTotal=total, topupBalance=balance)
    _, windows, balances = muse.parse_muse(_rsc(sub), NOW)
    assert [w.key for w in windows] == ["weekly"]
    assert balances == []


def test_parse_muse_topup_alone_is_enough():
    # 週間窓が無くても残高だけで usage になる (残高だけのカード表示に乗る)
    sub = dict(SUBSCRIPTION, usage={"state": "METERED", "percentUsed": None})
    _, windows, balances = muse.parse_muse(_rsc(sub), NOW)
    assert windows == []
    assert len(balances) == 1


@pytest.mark.parametrize("tier,expected", [
    ({}, "Muse"),
    (None, "Muse"),
    ({"name": None}, "Muse"),
    ({"name": ""}, "Muse"),
    ({"name": "   "}, "Muse"),
    ({"name": 42}, "Muse"),
    ("not-a-dict", "Muse"),
    ({"name": " パワー "}, "Muse パワー"),
])
def test_parse_muse_name(tier, expected):
    sub = dict(SUBSCRIPTION, tier=tier)
    name, _, _ = muse.parse_muse(_rsc(sub), NOW)
    assert name == expected


@pytest.mark.parametrize("text", [
    "",
    "not rsc at all",
    '0:{"a":"$@1"}',
    '1:{"success":false,"subscription":{}}',
    '1:{"success":true}',                          # subscription 無し
    '1:{"success":true,"subscription":"broken"}',  # subscription が dict でない
])
def test_parse_muse_without_success_line_is_error(text):
    with pytest.raises(ValueError):
        muse.parse_muse(text, NOW)


@pytest.mark.parametrize("data", [[], None, 42, {"subscription": SUBSCRIPTION}])
def test_parse_muse_non_string_is_error(data):
    with pytest.raises(ValueError):
        muse.parse_muse(data, NOW)


# ---------------------------------------------------------- cookie expiry

def test_error_message_has_no_cookie():
    message = providers.t("providers.muse_cookie_expired")
    assert message == "Cookie の期限切れ（set-key muse で入れ直してください）"
    assert COOKIE not in message


# ------------------------------------------------------- action id resolve

def test_resolve_action_id_first_level(monkeypatch):
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/a.js",
                                  "/_next/static/chunks/b.js"),
        "https://muse.ai/_next/static/chunks/a.js": "var x=1;",
        "https://muse.ai/_next/static/chunks/b.js": _js_with_action(ACTION_ID),
    })
    assert muse._resolve_action_id(COOKIE) == ACTION_ID
    # HTML には Cookie を付けるが、静的 JS には付けない
    by_url = {u: h for u, h, _t in calls["get"]}
    assert by_url[muse.MUSE_PAGE_URL]["Cookie"] == COOKIE
    assert "Cookie" not in by_url["https://muse.ai/_next/static/chunks/a.js"]
    assert "Cookie" not in by_url["https://muse.ai/_next/static/chunks/b.js"]


def test_resolve_action_id_second_level(monkeypatch):
    # 1 段目の JS が遅延チャンク一覧 "static/chunks/*.js" を内包し、2 段目でヒット
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/app.js"),
        "https://muse.ai/_next/static/chunks/app.js":
            'e("static/chunks/lazy-1.js"),e("static/chunks/lazy-2.js")',
        "https://muse.ai/_next/static/chunks/lazy-1.js": "var x=1;",
        "https://muse.ai/_next/static/chunks/lazy-2.js": _js_with_action(ACTION_ID),
    })
    assert muse._resolve_action_id(COOKIE) == ACTION_ID
    urls = [c[0] for c in calls["get"]]
    assert urls == [
        muse.MUSE_PAGE_URL,
        "https://muse.ai/_next/static/chunks/app.js",
        "https://muse.ai/_next/static/chunks/lazy-1.js",
        "https://muse.ai/_next/static/chunks/lazy-2.js",
    ]


def test_resolve_action_id_dedupes_chunks(monkeypatch):
    _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/a.js",
                                  "/_next/static/chunks/a.js",
                                  "https://muse.ai/_next/static/chunks/a.js"),
        "https://muse.ai/_next/static/chunks/a.js": _js_with_action(ACTION_ID),
    })
    assert muse._resolve_action_id(COOKIE) == ACTION_ID


def test_resolve_action_id_not_found(monkeypatch):
    _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/a.js"),
        "https://muse.ai/_next/static/chunks/a.js":
            'e("static/chunks/lazy.js"),var x=1;',
        "https://muse.ai/_next/static/chunks/lazy.js": "var y=2;",
    })
    with pytest.raises(ValueError):
        muse._resolve_action_id(COOKIE)


def test_resolve_action_id_js_errors_are_skipped(monkeypatch):
    _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/broken.js",
                                  "/_next/static/chunks/good.js"),
        "https://muse.ai/_next/static/chunks/broken.js":
            urllib.error.URLError("dummy network error"),
        "https://muse.ai/_next/static/chunks/good.js": _js_with_action(ACTION_ID),
    })
    assert muse._resolve_action_id(COOKIE) == ACTION_ID


def test_resolve_action_id_page_401_is_cookie_error(monkeypatch):
    _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _http_error(401),
    })
    with pytest.raises(CredentialError) as excinfo:
        muse._resolve_action_id(COOKIE)
    assert "Cookie" in str(excinfo.value)
    assert COOKIE not in str(excinfo.value)


def test_resolve_action_id_page_other_error(monkeypatch):
    _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _http_error(500),
    })
    with pytest.raises(urllib.error.HTTPError):
        muse._resolve_action_id(COOKIE)


def test_action_id_cache_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "muse-action.json"
    monkeypatch.setattr(muse, "_action_cache_file", lambda: path)
    assert muse._cached_action() is None
    stamp = int(time.time())
    muse._write_action_cache({"action_id": ACTION_ID, "verified": True,
                              "resolved_at": stamp})
    assert muse._cached_action() == (ACTION_ID, True, stamp)
    muse._write_action_cache({"action_id": ACTION_ID})  # 省略は False / None
    assert muse._cached_action() == (ACTION_ID, False, None)
    assert COOKIE not in path.read_text(encoding="utf-8")


def test_action_id_cache_ignores_broken_files(tmp_path, monkeypatch):
    path = tmp_path / "muse-action.json"
    monkeypatch.setattr(muse, "_action_cache_file", lambda: path)
    path.write_text("{not json", encoding="utf-8")
    assert muse._cached_action() is None
    path.write_text('{"action_id": "short"}', encoding="utf-8")
    assert muse._cached_action() is None
    path.write_text('{"action_id": 42}', encoding="utf-8")
    assert muse._cached_action() is None
    path.write_text('[]', encoding="utf-8")
    assert muse._cached_action() is None


# ------------------------------------------------------------------ fetch

def test_fetch_resolve_post_and_parse(monkeypatch):
    _cookie_keychain(monkeypatch)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js": _js_with_action(ACTION_ID),
    }, posts=[_rsc(SUBSCRIPTION)])

    usage = muse.fetch(NOW)

    assert usage.provider == "muse"
    assert usage.name == "Muse ダミープラン"
    assert usage.error is None
    assert [w.id for w in usage.windows] == ["muse:weekly"]
    assert [b.currency for b in usage.balances] == ["TOKENS"]
    assert usage.fetched_at == NOW

    url, headers, body = calls["post"][0]
    assert url == "https://muse.ai/"
    assert headers["Cookie"] == COOKIE
    assert headers["User-Agent"] == "quota-canvas"
    assert headers["Next-Action"] == ACTION_ID
    assert headers["Accept"] == "text/x-component"
    assert headers["Content-Type"] == "text/plain;charset=UTF-8"
    assert headers["Origin"] == "https://muse.ai"
    assert body == '[{"includeAgreement":false}]'


def test_fetch_uses_cached_action_id(monkeypatch, tmp_path):
    _cookie_keychain(monkeypatch)
    cache = tmp_path / "muse-action.json"
    cache.write_text(json.dumps({"action_id": ACTION_ID, "verified": True}),
                     encoding="utf-8")
    monkeypatch.setattr(muse, "_action_cache_file", lambda: cache)
    calls = _install_http(monkeypatch, {}, posts=[_rsc(SUBSCRIPTION)])

    usage = muse.fetch(NOW)

    assert usage.error is None
    assert calls["get"] == []                    # キャッシュが効くので解決しない
    assert calls["post"][0][1]["Next-Action"] == ACTION_ID


def test_fetch_saves_resolved_action_id(monkeypatch, tmp_path):
    _cookie_keychain(monkeypatch)
    cache = tmp_path / "muse-action.json"
    monkeypatch.setattr(muse, "_action_cache_file", lambda: cache)
    _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js": _js_with_action(ACTION_ID),
    }, posts=[_rsc(SUBSCRIPTION)])

    muse.fetch(NOW)
    # 解決直後は verified=false、Server Action 成功で verified=true に更新
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["action_id"] == ACTION_ID
    assert data["verified"] is True
    assert isinstance(data["resolved_at"], int)


def test_fetch_reresolves_after_verified_cached_id_404(monkeypatch, tmp_path):
    _cookie_keychain(monkeypatch)
    cache = tmp_path / "muse-action.json"
    cache.write_text(json.dumps({"action_id": ACTION_ID, "verified": True}),
                     encoding="utf-8")
    monkeypatch.setattr(muse, "_action_cache_file", lambda: cache)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js":
            _js_with_action(RESOLVED_ID),
    }, posts=[_http_error(404), _rsc(SUBSCRIPTION)])

    usage = muse.fetch(NOW)

    assert usage.error is None
    assert len(calls["post"]) == 2
    assert calls["post"][0][1]["Next-Action"] == ACTION_ID
    assert calls["post"][1][1]["Next-Action"] == RESOLVED_ID
    assert json.loads(cache.read_text(encoding="utf-8"))["action_id"] \
        == RESOLVED_ID


def test_fetch_unverified_cached_id_404_is_cookie_error(monkeypatch,
                                                      tmp_path):
    # verified=false でも解決から 6 時間以内なら再解決せず Cookie 切れ
    _cookie_keychain(monkeypatch)
    cache = tmp_path / "muse-action.json"
    cache.write_text(json.dumps({"action_id": ACTION_ID, "verified": False,
                                 "resolved_at": int(time.time())}),
                     encoding="utf-8")
    monkeypatch.setattr(muse, "_action_cache_file", lambda: cache)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js":
            _js_with_action(RESOLVED_ID),
    }, posts=[_http_error(404)])
    with pytest.raises(CredentialError) as excinfo:
        muse.fetch(NOW)
    assert "Cookie" in str(excinfo.value)
    assert calls["get"] == []                    # 再解決しない
    assert len(calls["post"]) == 1


def test_fetch_stale_unverified_cached_id_404_reresolves(monkeypatch,
                                                         tmp_path):
    # verified=false でも解決から 6 時間過ぎていれば失敗時に 1 回だけ再解決
    _cookie_keychain(monkeypatch)
    cache = tmp_path / "muse-action.json"
    stale = int(time.time() - muse.RESOLVE_BACKOFF - 1)
    cache.write_text(json.dumps({"action_id": ACTION_ID, "verified": False,
                                 "resolved_at": stale}),
                     encoding="utf-8")
    monkeypatch.setattr(muse, "_action_cache_file", lambda: cache)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js":
            _js_with_action(RESOLVED_ID),
    }, posts=[_http_error(404), _rsc(SUBSCRIPTION)])
    usage = muse.fetch(NOW)
    assert usage.error is None
    assert len(calls["post"]) == 2
    assert calls["post"][0][1]["Next-Action"] == ACTION_ID
    assert calls["post"][1][1]["Next-Action"] == RESOLVED_ID


def test_fetch_cached_id_without_resolved_at_reresolves(monkeypatch,
                                                        tmp_path):
    # resolved_at が無い古いキャッシュは期限切れ扱い (失敗で再解決する)
    _cookie_keychain(monkeypatch)
    cache = tmp_path / "muse-action.json"
    cache.write_text(json.dumps({"action_id": ACTION_ID}), encoding="utf-8")
    monkeypatch.setattr(muse, "_action_cache_file", lambda: cache)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js":
            _js_with_action(RESOLVED_ID),
    }, posts=[_http_error(404), _rsc(SUBSCRIPTION)])
    usage = muse.fetch(NOW)
    assert usage.error is None
    assert len(calls["post"]) == 2
    assert calls["post"][1][1]["Next-Action"] == RESOLVED_ID


def test_fetch_fresh_id_404_is_cookie_error_without_reresolve(monkeypatch):
    _cookie_keychain(monkeypatch)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js":
            _js_with_action(RESOLVED_ID),
    }, posts=[_http_error(404)])
    # 解決したばかりの ID (verified=false) で 404 → 解決し直さず Cookie 切れ
    with pytest.raises(CredentialError) as excinfo:
        muse.fetch(NOW)
    assert "Cookie" in str(excinfo.value)
    assert len(calls["post"]) == 1
    assert len(calls["get"]) == 2  # ページ + 1 本の JS のみ (再解決なし)


def test_fetch_fresh_id_unexpected_response_is_cookie_error(monkeypatch):
    _cookie_keychain(monkeypatch)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js":
            _js_with_action(RESOLVED_ID),
    }, posts=['0:{"a":"$@1","q":"","i":false}'])
    # 解決したばかりの ID (verified=false) で success 行無し → 再解決せず
    # Cookie 切れ
    with pytest.raises(CredentialError) as excinfo:
        muse.fetch(NOW)
    assert "Cookie" in str(excinfo.value)
    assert len(calls["post"]) == 1
    assert len(calls["get"]) == 2  # 再解決なし


def test_fetch_cached_id_unexpected_response_reresolve_succeeds(monkeypatch,
                                                               tmp_path):
    _cookie_keychain(monkeypatch)
    cache = tmp_path / "muse-action.json"
    cache.write_text(json.dumps({"action_id": ACTION_ID, "verified": True}),
                     encoding="utf-8")
    monkeypatch.setattr(muse, "_action_cache_file", lambda: cache)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js":
            _js_with_action(RESOLVED_ID),
    }, posts=['0:{"a":"$@1","q":"","i":false}', _rsc(SUBSCRIPTION)])
    # verified のキャッシュ ID の応答が想定外 → 解決し直せば成功する
    usage = muse.fetch(NOW)
    assert usage.error is None
    assert len(calls["post"]) == 2
    assert calls["post"][1][1]["Next-Action"] == RESOLVED_ID


def test_resolve_failure_is_cached_and_throttled(monkeypatch, tmp_path):
    _cookie_keychain(monkeypatch)
    cache = tmp_path / "muse-action.json"
    monkeypatch.setattr(muse, "_action_cache_file", lambda: cache)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js": "var x=1;",
    })
    with pytest.raises(ValueError):
        muse.fetch(NOW)
    stamp = json.loads(cache.read_text(encoding="utf-8"))["resolve_failed_at"]
    assert isinstance(stamp, int)

    # 抑制中は JS を取り直さず短いエラー
    calls["get"].clear()
    with pytest.raises(CredentialError) as excinfo:
        muse.fetch(NOW)
    assert str(excinfo.value) == \
        "Muse の取得先を解決できない（数時間後に再試行）"
    assert calls["get"] == []

    # 抑制期間を過ぎれば再解決する
    monkeypatch.setattr(muse.time, "time",
                        lambda: stamp + muse.RESOLVE_BACKOFF + 1)
    with pytest.raises(ValueError):
        muse.fetch(NOW)
    assert len(calls["get"]) == 2


def test_resolve_aborts_after_time_budget(monkeypatch):
    _cookie_keychain(monkeypatch)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/a.js",
                                  "/_next/static/chunks/b.js"),
        "https://muse.ai/_next/static/chunks/a.js": "var x=1;",
        "https://muse.ai/_next/static/chunks/b.js": _js_with_action(ACTION_ID),
    })
    ticks = itertools.count(0, 1000)  # monotonic が大きく進む → 打ち切り
    monkeypatch.setattr(muse.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ValueError):
        muse._resolve_action_id(COOKIE)
    # 1 本目の JS も取得せず打ち切る
    assert [c[0] for c in calls["get"]] == [muse.MUSE_PAGE_URL]


def test_resolve_uses_short_js_timeout(monkeypatch):
    _cookie_keychain(monkeypatch)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/a.js"),
        "https://muse.ai/_next/static/chunks/a.js": _js_with_action(ACTION_ID),
    })
    assert muse._resolve_action_id(COOKIE) == ACTION_ID
    timeouts = {u: t for u, _h, t in calls["get"]}
    assert timeouts["https://muse.ai/_next/static/chunks/a.js"] \
        == muse.JS_TIMEOUT


def test_resolve_js_429_propagates(monkeypatch):
    _cookie_keychain(monkeypatch)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/a.js",
                                  "/_next/static/chunks/b.js"),
        "https://muse.ai/_next/static/chunks/a.js": _http_error(429),
        "https://muse.ai/_next/static/chunks/b.js": _js_with_action(ACTION_ID),
    })
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        muse._resolve_action_id(COOKIE)
    assert excinfo.value.code == 429
    # 429 で探索を中断する (b.js には行かない)
    assert not any(c[0].endswith("b.js") for c in calls["get"])


# ---------------------------------------------------------- bounded reads

class _FakeResponse:
    """_open が返す応答の最小 fake (read(n) と with 対応)。"""

    def __init__(self, data):
        self._data, self._pos = data, 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_get_text_aborts_over_limit(monkeypatch):
    data = b"x" * (muse.READ_LIMIT + 1)
    monkeypatch.setattr(muse, "_open",
                        lambda req, timeout=None: _FakeResponse(data))
    with pytest.raises(ValueError):
        muse._get_text("https://muse.ai/x", {})


def test_get_text_aborts_on_deadline(monkeypatch):
    monkeypatch.setattr(muse, "_open",
                        lambda req, timeout=None: _FakeResponse(b"x" * 100))
    # 期限は既に過ぎている → 1 バイトも読まず打ち切り
    with pytest.raises(TimeoutError):
        muse._get_text("https://muse.ai/x", {}, deadline=-1.0)


def test_post_text_aborts_over_limit(monkeypatch):
    data = b"x" * (muse.READ_LIMIT + 1)
    monkeypatch.setattr(muse, "_open",
                        lambda req, timeout=None: _FakeResponse(data))
    with pytest.raises(ValueError):
        muse._post_text("https://muse.ai/", {}, "body")


class _Read1Response(_FakeResponse):
    """read1 だけを持つ応答 (read は呼ばれたら失敗)。"""
    def read(self, n=-1):
        raise AssertionError("read must not be used")

    def read1(self, n=-1):
        return _FakeResponse.read(self, n)


def test_read_bounded_uses_read1(monkeypatch):
    # read() は内部で受信を繰り返すので read1() で各塊の前に期限を確認する
    monkeypatch.setattr(muse, "_open",
                        lambda req, timeout=None: _Read1Response(b"ok"))
    assert muse._get_text("https://muse.ai/x", {}) == "ok"


class _TrickleResponse(_Read1Response):
    """read1 ごとに monotonic を step だけ進める応答 (少量ずつ届く想定)。"""

    def __init__(self, data, clock, step):
        super().__init__(data)
        self._clock, self._step = clock, step

    def read1(self, n=-1):
        self._clock[0] += self._step
        return _FakeResponse.read(self, n)


def test_read_bounded_checks_deadline_each_chunk(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(muse.time, "monotonic", lambda: clock[0])
    response = _TrickleResponse(b"x" * (muse.READ_CHUNK * 3), clock, 10.0)
    monkeypatch.setattr(muse, "_open", lambda req, timeout=None: response)
    # 2 塊読んだ時点で期限超過 → 残りを待たず打ち切る
    with pytest.raises(TimeoutError):
        muse._get_text("https://muse.ai/x", {}, deadline=15.0)


def test_post_text_has_own_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(muse.time, "monotonic", lambda: clock[0])
    response = _TrickleResponse(b"x" * (muse.READ_CHUNK * 3), clock,
                                muse.POST_DEADLINE + 1)
    monkeypatch.setattr(muse, "_open", lambda req, timeout=None: response)
    with pytest.raises(TimeoutError):
        muse._post_text("https://muse.ai/", {}, "body")


def test_read_bounded_sets_socket_timeout(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(muse.time, "monotonic", lambda: clock[0])
    timeouts = []
    sock = types.SimpleNamespace(settimeout=timeouts.append)
    response = _Read1Response(b"x")
    response.fp = types.SimpleNamespace(
        raw=types.SimpleNamespace(_sock=sock))
    monkeypatch.setattr(muse, "_open", lambda req, timeout=None: response)
    muse._get_text("https://muse.ai/x", {}, timeout=5.0)
    assert timeouts and all(t == 5.0 for t in timeouts)  # 既定はそのまま
    timeouts.clear()
    muse._get_text("https://muse.ai/x", {}, timeout=5.0, deadline=3.0)
    assert timeouts and all(t == 3.0 for t in timeouts)  # 残り時間で絞る


def test_resolve_deadline_counts_from_page_fetch(monkeypatch):
    # deadline は HTML 取得の前から数える: ページを読み終えた時点で
    # 予算超過なら JS は 1 本も取らない
    _cookie_keychain(monkeypatch)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/a.js"),
        "https://muse.ai/_next/static/chunks/a.js": _js_with_action(ACTION_ID),
    })
    clock = [0.0]
    monkeypatch.setattr(muse.time, "monotonic", lambda: clock[0])
    patched_get = muse._get_text

    def slow_get(url, headers, timeout=muse.TIMEOUT, deadline=None):
        result = patched_get(url, headers, timeout=timeout, deadline=deadline)
        clock[0] += 1000.0  # 読了時点で RESOLVE_BUDGET を大きく超過
        return result

    monkeypatch.setattr(muse, "_get_text", slow_get)
    with pytest.raises(ValueError):
        muse._resolve_action_id(COOKIE)
    assert [c[0] for c in calls["get"]] == [muse.MUSE_PAGE_URL]


@pytest.mark.parametrize("code", [401, 403])
def test_fetch_cookie_expired_http_error(monkeypatch, code):
    _cookie_keychain(monkeypatch)
    calls = _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js": _js_with_action(ACTION_ID),
    }, posts=[_http_error(code)])
    with pytest.raises(CredentialError) as excinfo:
        muse.fetch(NOW)
    assert str(excinfo.value) == \
        "Cookie の期限切れ（set-key muse で入れ直してください）"
    assert COOKIE not in str(excinfo.value)
    assert len(calls["post"]) == 1               # 認証失敗では再試行しない


def test_fetch_other_http_error_propagates(monkeypatch):
    _cookie_keychain(monkeypatch)
    _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js": _js_with_action(ACTION_ID),
    }, posts=[_http_error(500)])
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        muse.fetch(NOW)
    assert excinfo.value.code == 500


def test_fetch_action_id_not_found_is_error(monkeypatch):
    _cookie_keychain(monkeypatch)
    _install_http(monkeypatch, {
        muse.MUSE_PAGE_URL: _html("/_next/static/chunks/main.js"),
        "https://muse.ai/_next/static/chunks/main.js": "var x=1;",
    })
    with pytest.raises(ValueError):
        muse.fetch(NOW)


def test_fetch_without_cookie_is_credential_error(monkeypatch):
    monkeypatch.setattr(muse.keychain, "read", lambda account: None)
    monkeypatch.delenv("MUSE_COOKIE", raising=False)
    with pytest.raises(CredentialError):
        muse.fetch(NOW)


# ------------------------------------------------------------- detection

def test_detect_uses_keychain_without_network(monkeypatch):
    _cookie_keychain(monkeypatch)

    def no_network(*args, **kwargs):
        raise AssertionError("detect must not make a request")

    monkeypatch.setattr(muse, "_get_text", no_network)
    monkeypatch.setattr(muse, "_post_text", no_network)
    detection = muse.detect()
    assert detection.detected is True
    assert "Muse" in detection.source
    assert "キーチェーン" in detection.source
    assert detection.error is None


def test_detect_reports_missing_cookie_without_network(monkeypatch):
    monkeypatch.setattr(muse.keychain, "read", lambda account: None)
    monkeypatch.delenv("MUSE_COOKIE", raising=False)
    detection = muse.detect()
    assert detection.detected is False
    assert detection.source is None
    assert detection.error == "API キーが未設定"


def test_detect_env_variable(monkeypatch):
    monkeypatch.setattr(muse.keychain, "read", lambda account: None)
    monkeypatch.setenv("MUSE_COOKIE", COOKIE)
    detection = muse.detect()
    assert detection.detected is True
    assert "MUSE_COOKIE" in detection.source


# ---------------------------------------------------------------- provider

def test_provider_metadata():
    assert muse.PROVIDER.id == "muse"
    assert muse.PROVIDER.name == "Muse"
    assert muse.PROVIDER.display_name == "Muse (Meta)"
    assert muse.PROVIDER.needs_key is True
    assert providers.REGISTRY["muse"] is muse.PROVIDER
