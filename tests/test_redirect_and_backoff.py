"""認証付き HTTP のリダイレクトと 429 バックオフの境界値。通信しない。"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.cli import BACKOFF_BASE, BACKOFF_MAX, Poller
from quotacanvas.history import History
from quotacanvas.models import ProviderUsage
from quotacanvas.providers import _base, gemini

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("target", [
    "https://api.example.test/next",
    "https://api.example.test:443/next",
    "https://API.EXAMPLE.TEST:443/next",
])
def test_same_origin_redirect_keeps_headers(target):
    request = urllib.request.Request(
        "https://api.example.test/usage",
        headers={"Authorization": "Bearer test-token", "X-Api-Key": "test-key"},
    )
    redirected = _base._SameOriginRedirectHandler().redirect_request(
        request, None, 302, "Found", {}, target,
    )
    assert redirected.full_url == target
    assert redirected.get_header("Authorization") == "Bearer test-token"
    assert redirected.get_header("X-api-key") == "test-key"


@pytest.mark.parametrize("target", [
    "https://other.example.test/next",  # 別 host
    "https://api.example.test:444/next",  # 別 port
    "http://api.example.test/next",  # HTTPS から HTTP への格下げ
])
def test_cross_origin_redirect_returns_original_http_error(target):
    request = urllib.request.Request(
        "https://api.example.test/usage",
        headers={"Authorization": "Bearer test-token"},
    )
    with pytest.raises(urllib.error.HTTPError) as raised:
        _base._SameOriginRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, target,
        )
    assert raised.value.code == 302
    assert raised.value.url == request.full_url
    assert _base._short_reason(raised.value) == "HTTP 302"


def test_get_and_post_share_the_guarded_opener(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def open_request(request, timeout):
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr(_base._OPENER, "open", open_request)
    assert _base.http_get_json("https://api.example.test/usage",
                               {"Authorization": "Bearer get"}, timeout=3.0) == {"ok": True}
    assert gemini.http_post_json("https://api.example.test/usage",
                                 {"Authorization": "Bearer post"}, {"key": "value"},
                                 timeout=7.0) == {"ok": True}
    assert [(request.get_method(), timeout) for request, timeout in calls] == [
        ("GET", 3.0), ("POST", 7.0),
    ]
    assert [request.get_header("Authorization") for request, _ in calls] == [
        "Bearer get", "Bearer post",
    ]
    assert calls[1][0].data == b'{"key": "value"}'


@pytest.mark.parametrize("raw", ["1e309", "inf", "nan"])
def test_nonfinite_retry_after_is_ignored(raw):
    error = urllib.error.HTTPError("https://api.example.test/usage", 429,
                                   "Too Many Requests", {"Retry-After": raw}, None)
    assert providers._retry_after_seconds(error, NOW) is None


def test_large_finite_retry_after_is_parsed():
    error = urllib.error.HTTPError("https://api.example.test/usage", 429,
                                   "Too Many Requests", {"Retry-After": "1e20"}, None)
    assert providers._retry_after_seconds(error, NOW) == 1e20


@pytest.mark.parametrize("retry_after, expected", [
    (float("inf"), BACKOFF_MAX),
    (1e20, BACKOFF_MAX),
    (float("nan"), BACKOFF_BASE),
])
def test_direct_nonfinite_or_huge_retry_after_is_safe(tmp_path, retry_after, expected):
    poller = Poller(History(tmp_path / "history.jsonl"),
                    last_good_path=tmp_path / "last-good.json")
    usage = ProviderUsage("claude", "Claude", [], "HTTP 429", NOW, [], retry_after)
    poller._start_backoff(usage, NOW)
    assert poller._next_attempt["claude"] - NOW == expected


def test_backoff_count_100_stays_capped(tmp_path):
    poller = Poller(History(tmp_path / "history.jsonl"),
                    last_good_path=tmp_path / "last-good.json")
    poller._rate_limited["claude"] = 99
    usage = ProviderUsage("claude", "Claude", [], "HTTP 429", NOW)
    poller._start_backoff(usage, NOW)
    assert poller._rate_limited["claude"] == 100
    assert poller._next_attempt["claude"] - NOW == BACKOFF_MAX
