"""Codex のリセット権 (rate-limit-reset-credits) のテスト。

通信はすべて codex.http_get_json / read_codex_credentials の差し替えで行う
(実ネットワークなし)。リセット権は閲覧 (GET) のみで、権利を消費する API
(…/consume) は URL としても定義せず、呼ばないことを URL 検査で保証する。
"""

from __future__ import annotations

import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas.models import ProviderUsage, ResetCredit, Window
from quotacanvas.providers import TIMEOUT, codex

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc


def _credit(title, expires_at, granted_at="2026-09-29T18:45:33.805405Z",
            status="available", supported=True):
    return {
        "id": "RateLimitResetCredit_" + title,
        "reset_type": "codex_rate_limits",
        "is_supported_by_plan": supported,
        "status": status,
        "granted_at": granted_at,
        "expires_at": expires_at,
        "redeem_started_at": None,
        "redeemed_at": None,
        "title": title,
    }


def _iso(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC).isoformat()


# --------------------------------------------------------- parse_reset_credits

def test_parse_reset_credits_orders_by_expiry():
    data = {"credits": [
        _credit("late", _iso(2026, 11, 6, 23, 17),
                granted_at="2026-10-07T23:17:55.910516Z"),
        _credit("early", _iso(2026, 10, 29, 18, 45),
                granted_at="2026-09-29T18:45:33.805405Z"),
    ]}
    credits = codex.parse_reset_credits(data, NOW)
    assert [c.title for c in credits] == ["early", "late"]  # 期限の早い順
    assert credits[0].expires_at == datetime(2026, 10, 29, 18, 45, tzinfo=UTC)
    assert credits[0].granted_at == datetime(2026, 9, 29, 18, 45, 33, 805405, tzinfo=UTC)


def test_parse_reset_credits_tie_broken_by_granted_at():
    same = _iso(2026, 11, 1)
    data = {"credits": [
        _credit("newer", same, granted_at=_iso(2026, 10, 2)),
        _credit("older", same, granted_at=_iso(2026, 10, 1)),
    ]}
    assert [c.title for c in codex.parse_reset_credits(data, NOW)] == ["older", "newer"]


def test_parse_reset_credits_excludes_expired_and_status():
    data = {"credits": [
        _credit("expired", _iso(2026, 10, 1)),                 # 期限切れ
        _credit("boundary", NOW.isoformat()),                  # expires_at == now も切れ
        _credit("redeemed", _iso(2026, 12, 1), status="redeemed"),
        _credit("unsupported", _iso(2026, 12, 1), supported=False),
        _credit("kept", _iso(2026, 12, 1)),
    ]}
    assert [c.title for c in codex.parse_reset_credits(data, NOW)] == ["kept"]


def test_parse_reset_credits_missing_plan_flag_and_datetimes_are_kept():
    data = {"credits": [{"status": "available", "title": "no flags"}]}
    credits = codex.parse_reset_credits(data, NOW)
    assert len(credits) == 1
    assert credits[0].granted_at is None and credits[0].expires_at is None


def test_parse_reset_credits_empty_and_junk():
    assert codex.parse_reset_credits({}, NOW) == []
    assert codex.parse_reset_credits({"credits": [None, "x", 3]}, NOW) == []
    assert codex.parse_reset_credits({"credits": []}, NOW) == []


# ---------------------------------------------------------------- キャッシュ

CODEX_PAYLOAD = {
    "plan_type": "pro",
    "rate_limit": {
        "primary_window": {"used_percent": 12.5, "limit_window_seconds": 18000,
                           "reset_at": 1792872000},
        "secondary_window": None,
    },
}

CREDITS_PAYLOAD = {
    "credits": [
        _credit("first", _iso(2026, 10, 29, 18, 45)),
        _credit("second", _iso(2026, 11, 6, 23, 17),
                granted_at="2026-10-07T23:17:55.910516Z"),
    ],
    "available_count": 2,
    "total_earned_count": 0,
}


@pytest.fixture()
def fresh_cache(monkeypatch):
    monkeypatch.setattr(codex, "_RESET_CACHE", {"at": None, "credits": None})


@pytest.fixture()
def codex_auth(monkeypatch):
    monkeypatch.setattr(codex, "read_codex_credentials", lambda: ("tok", "acct-1"))


def _capture(monkeypatch, handler):
    calls = []

    def fake_http(url, headers, timeout=TIMEOUT):
        calls.append((url, headers))
        return handler(url, headers)

    monkeypatch.setattr(codex, "http_get_json", fake_http)
    return calls


def test_fetch_reset_credits_url_headers_and_cache(monkeypatch, fresh_cache, codex_auth):
    calls = _capture(monkeypatch, lambda url, headers: CREDITS_PAYLOAD)

    first = codex.fetch_reset_credits(NOW)
    assert [c.title for c in first] == ["first", "second"]
    assert len(calls) == 1
    url, headers = calls[0]
    assert url == codex.CODEX_RESET_CREDITS_URL
    assert url == "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["ChatGPT-Account-Id"] == "acct-1"
    assert headers["User-Agent"] == "quota-canvas"

    # TTL 内は前回の結果を返し、HTTP しに行かない
    again = codex.fetch_reset_credits(NOW + timedelta(minutes=5))
    assert again == first
    assert len(calls) == 1

    # TTL 超過で再取得
    codex.fetch_reset_credits(NOW + timedelta(minutes=15, seconds=1))
    assert len(calls) == 2


def test_fetch_reset_credits_failure_returns_previous(monkeypatch, fresh_cache, codex_auth):
    state = {"ok": True}
    calls = _capture(monkeypatch, lambda url, headers: CREDITS_PAYLOAD
                     if state["ok"] else (_ for _ in ()).throw(
                         urllib.error.HTTPError(url, 500, "boom", None, None)))

    assert len(codex.fetch_reset_credits(NOW)) == 2
    state["ok"] = False
    later = NOW + timedelta(minutes=16)  # キャッシュ期限切れ → 再取得するが失敗
    assert len(codex.fetch_reset_credits(later)) == 2  # 前回の結果
    assert len(calls) == 2


def test_credits_failure_keeps_usage_intact(monkeypatch, fresh_cache, codex_auth):
    def handler(url, headers):
        if url == codex.CODEX_RESET_CREDITS_URL:
            raise urllib.error.HTTPError(url, 500, "boom", None, None)
        return CODEX_PAYLOAD

    _capture(monkeypatch, handler)
    usage = codex.fetch(NOW)
    assert usage.error is None
    assert [w.key for w in usage.windows] == ["5h"]
    assert usage.name == "Codex Pro"
    assert usage.reset_credits == []


# ------------------------------------------------- consume を絶対に呼ばない

def test_fetch_never_calls_anything_but_the_two_get_urls(monkeypatch, fresh_cache,
                                                         codex_auth):
    payloads = {codex.CODEX_USAGE_URL: CODEX_PAYLOAD,
                codex.CODEX_RESET_CREDITS_URL: CREDITS_PAYLOAD}
    calls = _capture(monkeypatch, lambda url, headers: payloads[url])

    usage = codex.fetch(NOW)
    assert usage.error is None
    assert len(usage.reset_credits) == 2
    assert len(calls) == 2
    allowed = {codex.CODEX_USAGE_URL, codex.CODEX_RESET_CREDITS_URL}
    for url, _headers in calls:
        assert url in allowed                    # 許可した 2 URL のみ
        assert "consume" not in url              # 権利を消費する API は呼ばない


def test_reset_credits_module_declares_no_consume_url():
    # URL 定数としても consume を定義しない (うっかり呼べないように)
    for name in dir(codex):
        if name.startswith("CODEX_") and name.endswith("_URL"):
            assert "consume" not in getattr(codex, name)


# ------------------------------------------------------------ レンダリング

def test_render_panels_with_credits_smoke():
    """大パネル (見出し+行) と小パネル (1 行) の両方で例外なく描ける。"""
    from quotacanvas.pacing import compute_pace
    from quotacanvas.render import PanelSpec, render

    win = Window("codex", "weekly", "週間", 10.0,
                 NOW + timedelta(days=5), timedelta(days=7))
    pu = ProviderUsage("codex", "Codex Pro", [win], None, NOW, reset_credits=[
        ResetCredit("Full reset", NOW - timedelta(days=1), NOW + timedelta(hours=20)),
        ResetCredit("Full reset", NOW - timedelta(days=2), NOW + timedelta(days=5)),
    ])
    cards = [(pu, compute_pace(win, [], NOW))]
    now_local = NOW.astimezone()
    img = render(cards, now_local, layout=[
        PanelSpec("codex:weekly"),
        PanelSpec("codex:weekly", size="small"),
    ])
    assert img.size == (1920, 462)


def test_credit_rows_details_only_two_nearest():
    """詳しい行は期限の近い 2 件まで、残りは「ほか N 回」の 1 行にまとめる。"""
    from quotacanvas.render import _credit_rows

    credits = [ResetCredit("Full reset", None, NOW + timedelta(days=d))
               for d in (1, 4, 12, 15, 20)]
    rows = _credit_rows(credits, NOW)
    assert len(rows) == 3
    assert "3" in rows[-1][0]
    assert len(_credit_rows(credits[:2], NOW)) == 2   # 2 件なら「ほか」行なし


# --------------------------------- 期限切れ絞り込み / アカウント切替 (キャッシュ)

def test_fetch_reset_credits_cache_hit_drops_expired(monkeypatch, fresh_cache,
                                                     codex_auth):
    payload = {"credits": [
        _credit("soon", _iso(2026, 10, 24, 9, 3)),   # NOW + 3分で期限切れ
        _credit("long", _iso(2026, 10, 29, 18, 45)),
    ]}
    calls = _capture(monkeypatch, lambda url, headers: payload)

    first = codex.fetch_reset_credits(NOW)
    assert [c.title for c in first] == ["soon", "long"]

    # TTL 内のキャッシュヒットでも、期限が来た件は落ちる
    again = codex.fetch_reset_credits(NOW + timedelta(minutes=5))
    assert [c.title for c in again] == ["long"]
    assert len(calls) == 1


def test_fetch_reset_credits_failure_drops_expired_from_previous(
        monkeypatch, fresh_cache, codex_auth):
    payload = {"credits": [
        _credit("soon", _iso(2026, 10, 24, 9, 10)),  # 再取得時点で期限切れ
        _credit("long", _iso(2026, 10, 29, 18, 45)),
    ]}
    state = {"ok": True}
    calls = _capture(monkeypatch, lambda url, headers: payload
                     if state["ok"] else (_ for _ in ()).throw(
                         urllib.error.HTTPError(url, 500, "boom", None, None)))

    assert [c.title for c in codex.fetch_reset_credits(NOW)] == ["soon", "long"]
    state["ok"] = False
    later = NOW + timedelta(minutes=16)  # TTL 超過 → 再取得するが失敗
    again = codex.fetch_reset_credits(later)
    assert [c.title for c in again] == ["long"]      # 前回値からも期限切れを落とす
    assert len(calls) == 2


def test_fetch_reset_credits_refetches_when_account_changes(monkeypatch,
                                                            fresh_cache):
    account = {"id": "acct-1"}
    monkeypatch.setattr(codex, "read_codex_credentials",
                        lambda: ("tok", account["id"]))
    seen = []

    def handler(url, headers):
        seen.append(headers["ChatGPT-Account-Id"])
        return {"credits": [_credit("for-" + headers["ChatGPT-Account-Id"],
                                    _iso(2026, 12, 1))]}

    _capture(monkeypatch, handler)

    assert [c.title for c in codex.fetch_reset_credits(NOW)] == ["for-acct-1"]

    account["id"] = "acct-2"  # アカウントを切り替え
    second = codex.fetch_reset_credits(NOW + timedelta(minutes=5))
    assert [c.title for c in second] == ["for-acct-2"]  # TTL 内でも取り直し
    assert seen == ["acct-1", "acct-2"]


def test_fetch_reset_credits_failure_after_account_switch_returns_empty(
        monkeypatch, fresh_cache):
    account = {"id": "acct-1"}
    monkeypatch.setattr(codex, "read_codex_credentials",
                        lambda: ("tok", account["id"]))
    state = {"ok": True}

    def handler(url, headers):
        if state["ok"]:
            return {"credits": [_credit("old", _iso(2026, 12, 1))]}
        raise urllib.error.HTTPError(url, 500, "boom", None, None)

    _capture(monkeypatch, handler)

    assert len(codex.fetch_reset_credits(NOW)) == 1
    account["id"] = "acct-2"  # アカウントを切り替えてから失敗させる
    state["ok"] = False
    assert codex.fetch_reset_credits(NOW + timedelta(minutes=5)) == []


def test_fetch_reset_credits_without_credentials_returns_expired_filtered(
        monkeypatch, fresh_cache):
    auth_ok = {"on": True}

    def read_auth():
        if auth_ok["on"]:
            return "tok", "acct-1"
        raise codex.CredentialError("認証情報なし")

    monkeypatch.setattr(codex, "read_codex_credentials", read_auth)
    payload = {"credits": [
        _credit("soon", _iso(2026, 10, 24, 9, 3)),   # NOW + 3分で期限切れ
        _credit("long", _iso(2026, 10, 29, 18, 45)),
    ]}
    calls = _capture(monkeypatch, lambda url, headers: payload)

    assert len(codex.fetch_reset_credits(NOW)) == 2
    auth_ok["on"] = False
    # 認証情報を読めない (アカウント不明) → 前回値を期限で絞って返す
    later = codex.fetch_reset_credits(NOW + timedelta(minutes=5))
    assert [c.title for c in later] == ["long"]
    assert len(calls) == 1  # 通信はしに行かない
