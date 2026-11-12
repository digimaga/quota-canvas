"""指摘対応の回帰テスト: 非有限値の拒否・空の成功の拒否・HTTP 読み取り上限。"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.providers import _base, claude, codex, gemini, glm, grok, openrouter

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
RESET = "2026-11-19T12:00:00Z"
NON_FINITE = [float("nan"), float("inf"), float("-inf"), "nan", "inf", "1e999"]


# ------------------------------------------------------- 非有限値 (parse)

@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize("field", ["five_hour", "seven_day"])
def test_parse_claude_rejects_non_finite_utilization(field, value):
    data = {field: {"utilization": value, "resets_at": RESET}}
    with pytest.raises(ValueError):
        claude.parse_claude(data, NOW, "max")


@pytest.mark.parametrize("value", NON_FINITE)
def test_parse_claude_rejects_non_finite_scoped_percent(value):
    data = {"limits": [{"kind": "weekly_scoped",
                        "scope": {"model": {"display_name": "Opus"}},
                        "percent": value, "resets_at": RESET}]}
    with pytest.raises(ValueError):
        claude.parse_claude(data, NOW, "max")


@pytest.mark.parametrize("value", NON_FINITE)
def test_parse_codex_rejects_non_finite_used_percent(value):
    data = {"rate_limit": {"primary_window": {
        "used_percent": value, "limit_window_seconds": 18000}}}
    with pytest.raises(ValueError):
        codex.parse_codex(data, NOW)


@pytest.mark.parametrize("value", NON_FINITE)
def test_parse_glm_rejects_non_finite_percentage(value):
    data = {"data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": value,
                                 "unit": 3, "number": 5}]}}
    with pytest.raises(ValueError):
        glm.parse_glm(data, NOW)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_parse_gemini_rejects_non_finite_before_clamping(value):
    groups = [{"buckets": [{"id": "gemini-weekly",
                            "remaining_fraction": value,
                            "reset_time": RESET}]}]
    with pytest.raises(ValueError):
        gemini.parse_usage({"groups": groups})


def test_parse_gemini_still_clamps_finite_values():
    groups = [{"buckets": [
        {"id": "gemini-weekly", "remaining_fraction": 1.2},
        {"id": "3p-weekly", "remaining_fraction": -0.2},
    ]}]
    assert [w.used_percent for w in gemini.parse_usage({"groups": groups})] == [0.0, 100.0]


# --------------------------------------------- grok: 窓 0 個は成功にしない

def _grok_http(monkeypatch, payload):
    monkeypatch.setattr(grok, "_read_entry",
                        lambda now=None: ("tok", NOW + timedelta(hours=8)))
    monkeypatch.setattr(grok, "http_get_json", lambda url, headers: payload)


@pytest.mark.parametrize("config", [
    {},
    {"currentPeriod": {"start": "2026-11-19T00:00:00Z",
                       "end": "2026-11-12T00:00:00Z"},
     "creditUsagePercent": 10.0},  # 期間が逆
])
def test_grok_fetch_without_identified_window_is_an_error(monkeypatch, config):
    _grok_http(monkeypatch, {"config": config})
    usage = grok.fetch(NOW)
    assert usage.windows == []
    assert usage.error == "利用枠の情報を取得できない"


def test_grok_fetch_omitted_zero_percent_is_valid(monkeypatch):
    """proto3 は 0% の項目を省略する。期間があるときの欠落は正常な 0% として扱う。"""
    _grok_http(monkeypatch, {"config": {
        "currentPeriod": {"start": "2026-11-12T00:00:00Z",
                          "end": "2026-11-19T00:00:00Z"},
    }})
    usage = grok.fetch(NOW)
    assert usage.error is None
    assert [w.used_percent for w in usage.windows] == [0.0]


def test_grok_fetch_zero_percent_window_is_valid(monkeypatch):
    _grok_http(monkeypatch, {"config": {
        "currentPeriod": {"start": "2026-11-12T00:00:00Z",
                          "end": "2026-11-19T00:00:00Z"},
        "creditUsagePercent": 0.0,
    }})
    usage = grok.fetch(NOW)
    assert usage.error is None
    assert [w.used_percent for w in usage.windows] == [0.0]


# ------------------------------------------- openrouter: 欠けた使用額を省く

def test_openrouter_missing_limit_remaining_and_usage_is_empty_not_keyerror():
    windows, balances = openrouter.parse_key(
        {"data": {"limit": None, "limit_remaining": None}}, NOW)
    assert windows == [] and balances == []


def test_openrouter_key_without_usage_still_fetches_credits(monkeypatch):
    monkeypatch.setattr(openrouter, "locate_api_key",
                        lambda provider, variable: ("dummy", "test"))
    payloads = {
        openrouter.KEY_URL: {"data": {"limit": None, "limit_remaining": None}},
        openrouter.CREDITS_URL: {"data": {"total_credits": 10.0, "total_usage": 2.5}},
    }
    monkeypatch.setattr(openrouter, "http_get_json",
                        lambda url, headers: payloads[url])
    usage = openrouter.fetch(NOW)
    assert usage.error is None and usage.windows == []
    assert [b.amount for b in usage.balances] == [7.5]


@pytest.mark.parametrize("credits_ok", [True, False])
def test_openrouter_without_any_displayable_data_is_an_error(monkeypatch, credits_ok):
    monkeypatch.setattr(openrouter, "locate_api_key",
                        lambda provider, variable: ("dummy", "test"))

    def get(url, headers):
        if url == openrouter.CREDITS_URL:
            if not credits_ok:
                raise ValueError("credits broken")
            return {"data": {"total_credits": 1.0, "total_usage": 0.5}}
        return {"data": {"limit": None, "limit_remaining": None}}

    monkeypatch.setattr(openrouter, "http_get_json", get)
    if credits_ok:
        usage = openrouter.fetch(NOW)  # credits だけで表示できる
        assert usage.error is None and usage.balances
    else:
        with pytest.raises(ValueError):
            openrouter.fetch(NOW)


# ------------------------------------------ HTTP: サイズ上限と読み取り時間

class _FakeResponse:
    def __init__(self, body=b"{}", clock=None, step=0.0, endless=False):
        self.body = body
        self.pos = 0
        self.clock = clock
        self.step = step
        self.endless = endless
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        if self.clock is not None:
            self.clock.now += self.step
        if self.endless:
            return b"x"
        if n is None or n < 0:
            n = len(self.body) - self.pos
        chunk = self.body[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # pytest の出力を汚さない


class _TruncatedHandler(_QuietHandler):
    """Content-Length: 100 と宣言して 2 バイトだけ送り、接続を閉じる。"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b"{}")
        self.wfile.flush()
        self.close_connection = True


class _SlowChunkedHandler(_QuietHandler):
    """chunk サイズ行を 1 バイトずつ送る (read1 の内部 recv が複数回になる)。"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for byte in b"0000000000000002\r\n":
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.1)
            self.wfile.write(b"{}\r\n0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # クライアントが期限で切った


@contextlib.contextmanager
def _serve(handler):
    """127.0.0.1 の空きポートで http.server を立て、URL を返す。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/usage" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("method", ["get", "post"])
def test_http_json_rejects_oversized_body(monkeypatch, method):
    response = _FakeResponse(b"x" * (_base.READ_LIMIT + 1))
    monkeypatch.setattr(_base._OPENER, "open", lambda request, timeout: response)
    call = _base.http_get_json if method == "get" else \
        (lambda url, headers, timeout=_base.TIMEOUT:
         _base.http_post_json(url, headers, {}, timeout))
    with pytest.raises(ValueError):
        call("https://example.invalid/x", {})


def test_read_bounded_times_out_on_endless_trickle(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(_base.time, "monotonic", clock.monotonic)
    response = _FakeResponse(clock=clock, step=_base.READ_BUDGET / 3.0, endless=True)

    with pytest.raises(TimeoutError):
        _base._read_bounded(response)

    assert response.reads == 3  # 30 秒を 10 秒ずつ: 4 回目の前に打ち切る


def test_http_get_json_reads_body_normally(monkeypatch):
    response = _FakeResponse(b'{"ok": true}')
    monkeypatch.setattr(_base._OPENER, "open", lambda request, timeout: response)
    assert _base.http_get_json("https://example.invalid/x", {}) == {"ok": True}


def test_http_get_json_rejects_body_shorter_than_content_length():
    """read1() は EOF で空 bytes を返す。宣言長に届かない {} を成功にしない。"""
    with _serve(_TruncatedHandler) as url:
        with pytest.raises(http.client.IncompleteRead) as excinfo:
            _base.http_get_json(url, {}, timeout=5.0)
    assert excinfo.value.expected == 98  # 宣言 100 のうち 2 バイトだけ届いた
    assert providers._short_reason(excinfo.value) == "IncompleteRead"


def test_read_bounded_applies_budget_inside_one_chunked_read():
    """chunked のサイズ行を少しずつ送られても、read1() の内部受信で総時間を
    超えない (外側の期限確認まで戻るのを待たない)。"""
    started = time.monotonic()
    with _serve(_SlowChunkedHandler) as url:
        request = urllib.request.Request(url, method="GET")
        with _base._open(request, 5.0) as response:
            with pytest.raises(TimeoutError):
                _base._read_bounded(response, timeout=5.0, budget=0.3)
    assert time.monotonic() - started < 1.2  # 遅いと 1.8 秒かかる


def test_error_reason_for_bounded_read_failures_is_short():
    assert providers._short_reason(TimeoutError("slow")) == "timeout"
    assert providers._short_reason(ValueError("large")) == "ValueError"


def test_fetch_all_turns_bounded_read_failure_into_error(monkeypatch):
    """http_get_json の打ち切りは fetch_all で ProviderUsage.error になる。"""
    monkeypatch.setattr(_base, "READ_LIMIT", 1)
    response = _FakeResponse(b"{}")
    monkeypatch.setattr(_base._OPENER, "open", lambda request, timeout: response)
    monkeypatch.setattr(openrouter, "locate_api_key",
                        lambda provider, variable: ("dummy", "test"))
    usage = providers.fetch_all(NOW, enabled={"openrouter"})[0]
    assert usage.error == "ValueError"


# ---------------------- リダイレクト途中の応答にも読み取り上限を適用 (本物の opener)

class _RedirectHandler(_QuietHandler):
    """Location へ 302 で転送する。途中の応答の本文 (body) は chunk_delay 秒に
    1 バイトの間隔で送る (0 なら一気に)。/final は小さい JSON を返す。"""

    body = b"pad"
    chunk_delay = 0.0
    location = "/final"
    locations = {}   # パスごとの転送先 (無ければ location)
    hits = []        #受け取ったパス
    auths = []       # /final に届いた Authorization ヘッダ

    def _location(self):
        return self.locations.get(self.path, self.location)

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == "/final":
            type(self).auths.append(self.headers.get("Authorization"))
            payload = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body, delay = self.body, self.chunk_delay
        self.send_response(302)
        self.send_header("Location", self._location())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if delay:
                for i in range(len(body)):
                    self.wfile.write(body[i:i + 1])
                    self.wfile.flush()
                    time.sleep(delay)
            else:
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # クライアントが上限で打ち切った


def _redirect_handler(**attrs):
    """_RedirectHandler の亜種。テストごとに hits / auths を独立させる。"""
    return type("RedirectHandler", (_RedirectHandler,), dict(hits=[], auths=[], **attrs))


def test_http_json_follows_same_origin_redirect_with_small_body():
    """途中の応答の本文が小さければ、リダイレクトは今までどおり成功する。"""
    handler = _redirect_handler()
    with _serve(handler) as url:
        assert _base.http_get_json(
            url, {"Authorization": "Bearer test-token"}, timeout=5.0) == {"ok": True}
    assert handler.hits == ["/usage", "/final"]
    # 同一 origin への転送なら認証ヘッダーも渡る (安全策は壊れていない)
    assert handler.auths == ["Bearer test-token"]


def test_http_json_rejects_huge_redirect_body(monkeypatch):
    """リダイレクト途中の応答の本文が大きすぎたら、最終応答と同じく失敗する。"""
    monkeypatch.setattr(_base, "READ_LIMIT", 64 * 1024)
    handler = _redirect_handler(body=b"x" * (1 << 20))
    with _serve(handler) as url:
        with pytest.raises(ValueError):
            _base.http_get_json(url, {}, timeout=10.0)
    assert handler.hits == ["/usage"]  # 転送先へは行かない


def test_http_json_times_out_on_slowly_trickling_redirect_body(monkeypatch):
    """途中の応答の本文が少しずつ届き続けても、総時間上限で打ち切る。"""
    monkeypatch.setattr(_base, "READ_BUDGET", 0.3)
    handler = _redirect_handler(body=b"x" * 600, chunk_delay=0.05)  # 全部で 30 秒
    started = time.monotonic()
    with _serve(handler) as url:
        with pytest.raises(TimeoutError):
            _base.http_get_json(url, {}, timeout=5.0)
    assert time.monotonic() - started < 3.0  # 0.3 秒の上限 + 余裕で終わる
    assert handler.hits == ["/usage"]        # 転送先へは行かない


def test_http_json_redirect_discard_budget_is_shared_across_hops(monkeypatch):
    """読み捨ての総時間はリダイレクト全体で共有する (ホップごとにやり直さない)。

    各ホップの本文は 0.3 秒で読み終わるが、2 ホップ目で合計 0.6 秒 > 上限 0.5 秒。
    ホップごとに上限をやり直す実装だと成功してしまう。
    """
    monkeypatch.setattr(_base, "READ_BUDGET", 0.5)
    handler = _redirect_handler(body=b"x" * 30, chunk_delay=0.01,
                                locations={"/usage": "/hop1"})
    with _serve(handler) as url:
        with pytest.raises(TimeoutError):
            _base.http_get_json(url, {}, timeout=5.0)
    assert handler.hits == ["/usage", "/hop1"]  # 2 ホップ目の読み捨てで切れる


def test_http_json_rejects_cross_origin_redirect_end_to_end():
    """転送先が別 origin なら読み取りをせず HTTPError 302 (本物の opener でも)。"""
    other = _redirect_handler()
    with _serve(other) as other_url:
        handler = _redirect_handler(location=other_url)
        with _serve(handler) as url:
            with pytest.raises(urllib.error.HTTPError) as raised:
                _base.http_get_json(url, {"Authorization": "Bearer test-token"},
                                    timeout=5.0)
    assert raised.value.code == 302
    assert providers._short_reason(raised.value) == "HTTP 302"
    assert other.hits == []  # 転送先の origin には 1 バイトも送らない


def test_bounded_discard_fp_passes_amt_reads_through():
    """差し替えた fp の amt 指定の読み出しは本物に渡す (全文読み捨てのみ上限付き)。"""
    response = _FakeResponse(b"abcdef")
    wrapped = _base._BoundedDiscardFP(response, 5.0, time.monotonic() + 10.0)
    assert wrapped.read(3) == b"abc"     # amt 指定はそのまま
    assert wrapped.read() == b"def"      # 上限付きの読み捨て (_read_bounded)
