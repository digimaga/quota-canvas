"""ローカル LLM サーバー (providers.local) のテスト。

- /metrics (Prometheus テキスト形式) の解析: ラベル付き行の合計、コメント、壊れ値
- エンジンごとの指標名の既定と上書き、custom の必須
- fetch_one: 成功時の slots、キー (キーチェーン / 環境変数) のヘッダー、失敗理由
- gate: local 枠 4 択の判定 (free / full / queued / unreachable)、stale、off、行表示
- CLI: routing local-check の exit コード (0 / 2 / 3)
- config: [providers.local.<名前>] の検証・保存・JSON 化
- 描画: local カード (成功・失敗) がクラッシュしない

通信はすべて providers.local._http_get_text の差し替えで行い、
キーチェーンも local: アカウントを読ませない (ダミーキーを使う)。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quotacanvas import cli, gate, i18n, providers
from quotacanvas.config import (
    ConfigError,
    LocalProvider,
    config_to_dict,
    format_toml,
    load_config,
)
from quotacanvas.gate import evaluate, format_worker_segments
from quotacanvas.history import History
from quotacanvas.models import LocalSlots, ProviderUsage
from quotacanvas.providers.local import (
    ENGINE_METRICS,
    MetricsError,
    fetch_all,
    fetch_one,
    metric_names,
    metrics_url,
    parse_metrics,
    window_entry,
)
from quotacanvas.render import PanelSpec, render

NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=timezone.utc)
LOCAL_KEY = "local:test-server"

TENSORFOLD_METRICS = """\
# HELP tensorfold:num_requests_running requests being processed
# TYPE tensorfold:num_requests_running gauge
tensorfold:num_requests_running{model="m1",name="a"} 1
tensorfold:num_requests_running{model="m2",name="b"} 2
tensorfold:num_requests_waiting 0
"""


@pytest.fixture(autouse=True)
def _no_real_local_keychain(monkeypatch):
    """local: アカウントのキーチェーンを読ませない (実キーに触れない)。
    local: アカウントには明らかなダミーを返し、ほかは既定の動作に任せる。"""
    from quotacanvas import keychain

    prev = keychain.read
    monkeypatch.setattr(keychain, "read",
                        lambda account: "test-dummy-key"
                        if account.startswith("local:") else prev(account))
    monkeypatch.delenv("TEST_SERVER_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _restore_language():
    prev = i18n.current_language()
    yield
    i18n.set_language(prev)


def entry(**kw):
    """LocalProvider の既定 + 上書き。"""
    base = dict(name="test-server", base_url="http://localhost:8000", capacity=4)
    base.update(kw)
    return LocalProvider(**base)


def _fake_http(monkeypatch, body=None, error=None):
    """providers.local._http_get_text を差し替える。呼び出しを記録する。"""
    calls = []

    def fake(url, headers, timeout):
        calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if error is not None:
            raise error
        return body

    monkeypatch.setattr(providers.local, "_http_get_text", fake)
    return calls


# ---------------------------------------------------------------- /metrics 解析


def test_parse_metrics_sums_labeled_series():
    values, broken = parse_metrics(TENSORFOLD_METRICS)
    assert values["tensorfold:num_requests_running"] == 3.0
    assert values["tensorfold:num_requests_waiting"] == 0.0
    assert broken == set()


def test_parse_metrics_ignores_comments_and_blank_lines():
    text = "\n# comment\n\nother:metric 7\n"
    assert parse_metrics(text) == ({"other:metric": 7.0}, set())


def test_parse_metrics_accepts_timestamp_suffix():
    assert parse_metrics("m 2 1699999999999\n") == ({"m": 2.0}, set())


@pytest.mark.parametrize("line", [
    "no-value-here",            # 値がない
    "{label-first} 1",          # 名前がない
])
def test_parse_metrics_skips_unattributable_lines(line):
    # 名前を引き当てられない壊れた行は黙って読み飛ばす (例外にしない)
    assert parse_metrics(line) == ({}, set())


@pytest.mark.parametrize("line", [
    "m not-a-number",           # 数値でない
    "m NaN",                    # 非有限
    'm{unterminated="1" 2',     # ラベル部が閉じていない
])
def test_parse_metrics_reports_broken_metric_names(line):
    # 名前が取れる壊れた行は 2 つ目の戻り値で報告する (fetch_one が厳密に扱う)
    values, broken = parse_metrics(line)
    assert values == {}
    assert broken == {"m"}


def test_parse_metrics_label_value_may_contain_braces_and_quotes():
    text = ('m{path="/tmp/{x}",q="a\\"b"} 1\n'
            'm{path="plain"} 2\n')
    assert parse_metrics(text) == ({"m": 3.0}, set())


def test_parse_metrics_label_value_may_contain_spaces():
    text = 'm{note="hello world"} 3\n'
    assert parse_metrics(text) == ({"m": 3.0}, set())


def test_parse_metrics_ignores_openmetrics_exemplar_suffix():
    # OpenMetrics の exemplar (値の後の "# {...} <値> [時刻]") を値と混ぜない。
    # 貪欲なラベルマッチだと exemplar 値 (7) を読んでしまう。
    text = ('r{a="x"} 1 # {trace_id="t"} 7 1.7e9\n'
            'r{a="}"} 2 # {t="}"} 9\n'
            'r 4\n')
    values, broken = parse_metrics(text)
    assert values == {"r": 7.0}
    assert broken == set()


def test_parse_metrics_skips_nonfinite_of_other_metrics():
    # 対象外の指標が NaN/-nan/+Inf でも全体は落ちない
    text = ("other:cpu_ratio NaN\n"
            "another:metric -nan\n"
            "third:metric +Inf\n"
            "tensorfold:num_requests_running 2\n"
            "tensorfold:num_requests_waiting 0\n")
    values, broken = parse_metrics(text)
    assert values == {"tensorfold:num_requests_running": 2.0,
                      "tensorfold:num_requests_waiting": 0.0}
    assert broken == {"other:cpu_ratio", "another:metric", "third:metric"}


# ------------------------------------------------------------ エンジンの既定


def test_engine_metric_defaults():
    assert set(ENGINE_METRICS) == {"tensorfold", "vllm", "sglang", "llamacpp"}
    assert metric_names(entry()) == \
        ("tensorfold:num_requests_running", "tensorfold:num_requests_waiting")
    assert metric_names(entry(engine="vllm")) == \
        ("vllm:num_requests_running", "vllm:num_requests_waiting")
    assert metric_names(entry(engine="sglang")) == \
        ("sglang:num_running_reqs", "sglang:num_queue_reqs")
    assert metric_names(entry(engine="llamacpp")) == \
        ("llamacpp:requests_processing", "llamacpp:requests_deferred")


def test_metric_names_override_beats_engine_default():
    got = metric_names(entry(engine="vllm", running_metric="r", waiting_metric="w"))
    assert got == ("r", "w")


def test_metric_names_custom_requires_both():
    with pytest.raises(MetricsError):
        metric_names(entry(engine="custom"))
    assert metric_names(entry(engine="custom", running_metric="r",
                              waiting_metric="w")) == ("r", "w")


def test_metrics_url_joins_base_and_path():
    assert metrics_url(entry()) == "http://localhost:8000/metrics"
    assert metrics_url(entry(base_url="http://localhost:8000/",
                             metrics_path="custom/metrics")) == \
        "http://localhost:8000/custom/metrics"


def test_config_local_accepts_explicit_port(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[providers.local.a]\nbase_url = "http://localhost:8000/v1"\n'
                 'capacity = 2\n', encoding="utf-8")
    got = load_config(p).providers.local["a"]
    assert got.base_url == "http://localhost:8000/v1"


# ------------------------------------------------------------------- fetch_one


def test_fetch_one_success_builds_slots(monkeypatch):
    _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.provider == "local:test-server"
    assert usage.error is None
    assert usage.slots == LocalSlots(running=3, waiting=0, capacity=4)
    assert usage.name == "test-server"
    assert usage.windows == []


def test_fetch_one_label_is_used_as_display_name(monkeypatch):
    _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    usage = fetch_one("test-server", entry(label="GX10"), NOW)
    assert usage.name == "GX10"
    assert usage.provider == "local:test-server"


def test_fetch_one_sends_key_from_keychain_in_both_headers(monkeypatch):
    calls = _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    fetch_one("test-server", entry(), NOW)
    headers = calls[0]["headers"]
    assert headers["x-api-key"] == "test-dummy-key"
    assert headers["Authorization"] == "Bearer test-dummy-key"
    assert calls[0]["url"] == "http://localhost:8000/metrics"
    assert calls[0]["timeout"] == pytest.approx(3.0)


def test_fetch_one_env_var_key_when_keychain_empty(monkeypatch):
    from quotacanvas import keychain

    monkeypatch.setattr(keychain, "read", lambda account: None)
    monkeypatch.setenv("TEST_SERVER_API_KEY", "env-dummy-key")
    calls = _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    fetch_one("test-server", entry(api_key_env="TEST_SERVER_API_KEY"), NOW)
    assert calls[0]["headers"]["Authorization"] == "Bearer env-dummy-key"


def test_fetch_one_sends_no_key_headers_without_key(monkeypatch):
    from quotacanvas import keychain

    monkeypatch.setattr(keychain, "read", lambda account: None)
    calls = _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    fetch_one("test-server", entry(), NOW)
    assert "x-api-key" not in calls[0]["headers"]
    assert "Authorization" not in calls[0]["headers"]


def test_fetch_one_timeout_is_configurable(monkeypatch):
    calls = _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    fetch_one("test-server", entry(timeout=1.5), NOW)
    assert calls[0]["timeout"] == pytest.approx(1.5)


def test_fetch_one_http_error_is_short_reason(monkeypatch):
    _fake_http(monkeypatch, error=urllib.error.HTTPError(
        "url", 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b"secret-body")))
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error == "HTTP 401"
    assert usage.slots is None


def test_fetch_one_network_error_is_short_reason(monkeypatch):
    _fake_http(monkeypatch, error=urllib.error.URLError("refused"))
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error == "network error"


def test_fetch_one_invalid_body(monkeypatch):
    # どの行も値として成立しない = 対象の指標が見つからない
    _fake_http(monkeypatch, body="<html>not prometheus</html>")
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error == "metric not found"


def test_fetch_one_undecodable_body(monkeypatch):
    _fake_http(monkeypatch, error=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"))
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error == "invalid metrics"


def test_fetch_one_missing_metric(monkeypatch):
    _fake_http(monkeypatch, body="something:else 1\n")
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error == "metric not found"


def test_fetch_one_nonfinite_target_metric_is_error(monkeypatch):
    # 対象の指標が非有限なら「壊れた系列」扱いで invalid metrics にする
    _fake_http(monkeypatch, body="tensorfold:num_requests_running NaN\n"
                                 "tensorfold:num_requests_waiting 0\n")
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error == "invalid metrics"


def test_fetch_one_sums_valid_lines_around_garbage(monkeypatch):
    # 対象外の指標の壊れた行は無視して、対象の指標は合計する
    body = ("garbage line\n"
            "other:metric NaN\n"
            "tensorfold:num_requests_running 1\n"
            "tensorfold:num_requests_running 2\n"
            "tensorfold:num_requests_waiting 0\n")
    _fake_http(monkeypatch, body=body)
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.slots == LocalSlots(running=3, waiting=0, capacity=4)


@pytest.mark.parametrize("body", [
    # 対象の指標の系列 1 本でも壊れていたら合計を信用しない
    'tensorfold:num_requests_running{a="x"} 2\ntensorfold:num_requests_running{a="y"} NaN\n'
    'tensorfold:num_requests_waiting 0\n',
    'tensorfold:num_requests_running{a="unterminated 2\ntensorfold:num_requests_waiting 0\n',
])
def test_fetch_one_broken_target_series_is_error(monkeypatch, body):
    _fake_http(monkeypatch, body=body)
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error == "invalid metrics"
    assert usage.slots is None


def test_fetch_all_skips_disabled(monkeypatch):
    _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    entries = {"a": entry(name="a", enabled=True),
               "b": entry(name="b", enabled=False)}
    usages = fetch_all(entries, NOW)
    assert [u.provider for u in usages] == ["local:a"]


def test_fetch_one_keychain_error_falls_back_to_env(monkeypatch):
    from quotacanvas import keychain

    def broken_read(account):
        raise keychain.KeychainError("keychain locked")

    monkeypatch.setattr(keychain, "read", broken_read)
    monkeypatch.setenv("TEST_SERVER_API_KEY", "env-dummy-key")
    calls = _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    usage = fetch_one("test-server", entry(api_key_env="TEST_SERVER_API_KEY"), NOW)
    assert usage.error is None
    assert calls[0]["headers"]["Authorization"] == "Bearer env-dummy-key"


def test_fetch_one_keychain_error_without_env_sends_no_key(monkeypatch):
    from quotacanvas import keychain

    def broken_read(account):
        raise keychain.KeychainError("keychain locked")

    monkeypatch.setattr(keychain, "read", broken_read)
    calls = _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error is None
    assert "x-api-key" not in calls[0]["headers"]
    assert "Authorization" not in calls[0]["headers"]


# --------------------------------------------------------- HTTP の経路とリダイレクト


class _FakeResponse:
    def __init__(self, body=b"m 1\n"):
        self.body = body
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body


def test_http_get_text_uses_guarded_opener_with_read_limit(monkeypatch):
    """/metrics の取得も _base._OPENER (同一 origin リダイレクトしか許さない
    opener) を通り、本文は 1 MiB 上限で読む。"""
    from quotacanvas.providers import _base
    from quotacanvas.providers.local import MAX_METRICS_BYTES

    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        return _FakeResponse(b"m 1\n")

    monkeypatch.setattr(_base._OPENER, "open", open_request)
    text = providers.local._http_get_text(
        "http://localhost:8000/metrics",
        {"Authorization": "Bearer k", "x-api-key": "k"}, timeout=2.5)
    assert text == "m 1\n"
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "http://localhost:8000/metrics"
    assert request.get_header("Authorization") == "Bearer k"
    assert timeout == 2.5


def test_http_get_text_read_limit_is_one_mib(monkeypatch):
    from quotacanvas.providers import _base
    from quotacanvas.providers.local import MAX_METRICS_BYTES

    assert MAX_METRICS_BYTES == 1024 * 1024
    response = _FakeResponse()

    def open_request(request, timeout):
        return response

    monkeypatch.setattr(_base._OPENER, "open", open_request)
    providers.local._http_get_text("http://localhost:8000/metrics", {}, 3.0)
    # 上限 + 1 バイト読んで、超えていたら失敗にできるようにしている
    assert response.read_sizes == [MAX_METRICS_BYTES + 1]


def test_http_get_text_oversized_body_fails(monkeypatch):
    """上限を超える本文は途中で切り詰めるのではなく MetricsError にする
    (壊れた値を合計して表示しないため)。fetch_one 経由では "invalid metrics"。"""
    from quotacanvas.providers import _base
    from quotacanvas.providers.local import MAX_METRICS_BYTES, MetricsError

    def open_request(request, timeout):
        return _FakeResponse(b"m 1\n" + b"x" * MAX_METRICS_BYTES)

    monkeypatch.setattr(_base._OPENER, "open", open_request)
    with pytest.raises(MetricsError):
        providers.local._http_get_text("http://localhost:8000/metrics", {}, 3.0)

    def boom(*args, **kwargs):
        raise MetricsError("invalid metrics")

    monkeypatch.setattr(providers.local, "_http_get_text", boom)
    usage = fetch_one("test-server", entry(), NOW)
    assert usage.error == "invalid metrics"


def test_metrics_cross_origin_redirect_is_rejected_at_handler():
    """認証ヘッダー付きの /metrics 取得が別ホストへリダイレクトされても
    HTTPError になり (ヘッダーは渡らない)、理由は "HTTP 302"。"""
    from quotacanvas.providers import _base

    request = urllib.request.Request(
        "http://localhost:8000/metrics",
        headers={"Authorization": "Bearer k", "X-api-key": "k"})
    with pytest.raises(urllib.error.HTTPError) as raised:
        _base._SameOriginRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "http://other.example.test:8080/metrics")
    assert raised.value.code == 302
    assert providers.local._reason(raised.value) == "HTTP 302"


# ------------------------------------------------------------ window_entry / status


def test_window_entry_success_shape():
    usage = ProviderUsage("local:test-server", "test-server", [], None, NOW,
                          slots=LocalSlots(1, 0, 4))
    item = window_entry(usage)
    assert item["id"] == "local:test-server"
    assert item["provider"] == "local:test-server"
    assert item["key"] == "test-server"
    assert item["part_of"] is None
    assert item["kind"] == "local"
    assert (item["running"], item["waiting"], item["capacity"]) == (1, 0, 4)
    assert item["fetched_at"].startswith(NOW.astimezone().isoformat(timespec="seconds")[:16])
    assert "error" not in item


def test_window_entry_error_shape():
    usage = ProviderUsage("local:test-server", "test-server", [], "HTTP 500", NOW)
    item = window_entry(usage)
    assert item["kind"] == "local"
    assert item["error"] == "HTTP 500"
    assert "running" not in item


def test_build_status_includes_local_windows(tmp_path):
    from quotacanvas.status import build_status

    cfg = load_config(tmp_path / "missing.toml")
    usage = ProviderUsage("local:test-server", "test-server", [], None, NOW,
                          slots=LocalSlots(2, 1, 4))
    status = build_status(cfg, [], [usage], NOW)
    local = [w for w in status["windows"] if w.get("kind") == "local"]
    assert len(local) == 1
    assert local[0]["id"] == "local:test-server"
    assert (local[0]["running"], local[0]["waiting"], local[0]["capacity"]) == (2, 1, 4)


# ------------------------------------------------------------------ gate 判定


def local_win(running, waiting, capacity=4, *, fetched=NOW, error=None):
    item = {
        "id": "local:test-server", "provider": "local:test-server",
        "key": "test-server", "part_of": None, "kind": "local",
        "fetched_at": fetched.isoformat(timespec="seconds"),
    }
    if error is not None:
        item["error"] = error
    else:
        item.update(running=running, waiting=waiting, capacity=capacity)
    return item


def _status(*windows, updated=NOW):
    return {"updated_at": updated.isoformat(timespec="seconds"),
            "windows": list(windows), "errors": [], "reset_credits": {}}


def _routing_cfg():
    from quotacanvas.config import Routing

    return Routing(workers={"deepseek": ["local:test-server"]})


@pytest.mark.parametrize("running,waiting,expected", [
    (0, 0, ("boost", "free")),      # 空きあり → boost
    (3, 0, ("boost", "free")),
    (4, 0, ("reserve", "full")),    # 満杯 → reserve
    (9, 0, ("reserve", "full")),
    (1, 2, ("stop", "queued")),     # 待ちあり → stop
    (4, 1, ("stop", "queued")),     # 待ち > 満杯 を優先
])
def test_judge_local_patterns(running, waiting, expected):
    assert gate.judge_local(local_win(running, waiting)) == expected


def test_judge_local_unreachable_patterns():
    assert gate.judge_local(local_win(1, 0, error="HTTP 401")) == ("stop", "unreachable")
    assert gate.judge_local(local_win(1, 0, error="network error")) == ("stop", "unreachable")
    # 壊れた数値・capacity < 1 も判定不可として止める
    assert gate.judge_local(local_win("x", 0)) == ("stop", "unreachable")
    assert gate.judge_local(local_win(0, 0, capacity=0)) == ("stop", "unreachable")


@pytest.mark.parametrize("running,waiting,verdict", [
    (1, 0, "boost"), (4, 0, "reserve"), (2, 3, "stop"),
])
def test_evaluate_local_worker(running, waiting, verdict):
    result = evaluate(_status(local_win(running, waiting)), _routing_cfg(), {}, NOW)
    worker = result.worker("deepseek")
    assert worker.verdict == verdict
    assert worker.local == {"running": running, "waiting": waiting, "capacity": 4}
    assert worker.windows == ["local:test-server"]
    assert worker.status is None      # 残量 % の概念は無い
    assert "local" in worker.to_dict()


def test_evaluate_local_error_entry_stops():
    result = evaluate(_status(local_win(1, 0, error="network error")),
                      _routing_cfg(), {}, NOW)
    worker = result.worker("deepseek")
    assert (worker.verdict, worker.reason) == ("stop", "unreachable")
    assert worker.local == {"running": 0, "waiting": 0, "capacity": 0}


def test_evaluate_local_stale_is_unknown():
    old = NOW - timedelta(minutes=30)
    result = evaluate(_status(local_win(1, 0, fetched=old)), _routing_cfg(), {}, NOW)
    worker = result.worker("deepseek")
    assert (worker.verdict, worker.reason) == ("unknown", "stale")


def _wildcard_cfg():
    from quotacanvas.config import Routing

    return Routing(workers={"deepseek": ["local:*"]})


def test_evaluate_local_wildcard_matches_windows():
    result = evaluate(_status(local_win(1, 0)), _wildcard_cfg(), {}, NOW)
    worker = result.worker("deepseek")
    assert (worker.verdict, worker.reason) == ("boost", "free")


def test_evaluate_local_wildcard_stale_is_unknown():
    # "local:*" (ワイルドカード) でも _match_windows が拾った窓の stale を判定する
    old = NOW - timedelta(minutes=30)
    result = evaluate(_status(local_win(1, 0, fetched=old)), _wildcard_cfg(), {}, NOW)
    worker = result.worker("deepseek")
    assert (worker.verdict, worker.reason) == ("unknown", "stale")
    assert worker.stale is True


def test_evaluate_local_wildcard_error_stops():
    result = evaluate(_status(local_win(1, 0, error="network error")),
                      _wildcard_cfg(), {}, NOW)
    worker = result.worker("deepseek")
    assert (worker.verdict, worker.reason) == ("stop", "unreachable")


def test_evaluate_local_burn_does_not_upgrade():
    # local 枠だけのワーカーは使い切りモードでも reserve/stop を boost にしない
    until = NOW + timedelta(hours=2)
    for running, waiting, expected in [(4, 0, "reserve"), (2, 3, "stop")]:
        result = evaluate(_status(local_win(running, waiting)), _routing_cfg(),
                          {"deepseek": until}, NOW)
        worker = result.worker("deepseek")
        assert worker.verdict == expected
        assert worker.burn_until is None


def test_evaluate_local_worker_off_keeps_fields():
    result = evaluate(_status(local_win(2, 0)), _routing_cfg(), {}, NOW,
                      off={"deepseek"})
    worker = result.worker("deepseek")
    assert (worker.verdict, worker.reason) == ("stop", "disabled")
    assert worker.local == {"running": 2, "waiting": 0, "capacity": 4}


def test_local_never_becomes_default_worker():
    # workers 未指定の既定は provider 名ワーカー。local:test-server は
    # ワーカー名として使えない (':' を含む) ので現れない。
    result = evaluate(_status(local_win(1, 0)), _no_workers(), {}, NOW)
    assert all(not w.name.startswith("local:") for w in result.workers)


def _no_workers():
    from quotacanvas.config import Routing

    return Routing(workers={})


def test_worker_result_dict_has_no_local_for_cloud_workers():
    from quotacanvas.models import Window

    win = Window("glm", "5h", "5時間", 10.0, NOW + timedelta(days=1),
                 timedelta(hours=5))
    cloud = [{
        "id": "glm:5h", "provider": "glm", "key": "5h", "part_of": None,
        "remaining": 70.0, "status": "spare",
        "resets_at": (NOW + timedelta(days=1)).isoformat(timespec="seconds"),
        "fetched_at": NOW.isoformat(timespec="seconds"),
    }]
    result = evaluate(_status(*cloud), _no_workers(), {}, NOW)
    assert "local" not in result.workers[0].to_dict()


def test_format_worker_segments_local():
    result = evaluate(_status(local_win(1, 0)), _routing_cfg(), {}, NOW)
    assert format_worker_segments(result, NOW) == "deepseek=boost(1/4,待ち0)"
    result = evaluate(_status(local_win(4, 0)), _routing_cfg(), {}, NOW)
    assert format_worker_segments(result, NOW) == "deepseek=reserve(4/4,待ち0)"
    result = evaluate(_status(local_win(2, 3)), _routing_cfg(), {}, NOW)
    assert format_worker_segments(result, NOW) == "deepseek=stop(2/4,待ち3)"


def test_check_message_local_reasons():
    for running, waiting, error, expected in [
        (1, 2, None, "quota-canvas: deepseek=stop（待ち2（キューが消化されるまで停止））。"
                     "QUOTA_GATE_FORCE=1 で無視"),
        (4, 0, None, "quota-canvas: deepseek=reserve（使用中4/4（満杯なので様子見））。"
                     "可能なら他のワーカーを優先"),
        (0, 0, "HTTP 500", "quota-canvas: deepseek=stop（サーバーに届かない）。"
                           "QUOTA_GATE_FORCE=1 で無視"),
        (0, 0, None, None),  # boost は無言
    ]:
        result = evaluate(_status(local_win(running, waiting, error=error)),
                          _routing_cfg(), {}, NOW)
        assert gate.check_message(result.worker("deepseek"), NOW) == expected


# -------------------------------------------------------------- routing local-check


def _local_config(tmp_path, *, extra=""):
    text = """
[providers.local.test-server]
base_url = "http://localhost:8000"
capacity = 4
""" + extra
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _worker_config(tmp_path):
    """local 枠だけのワーカー (deepseek) を持つ設定。"""
    return _local_config(tmp_path, extra="""
[routing.workers]
deepseek = ["local:test-server"]
""")


def _run_cli(argv, config, env=None, stdin=""):
    import os

    old_env = {}
    if env:
        for key, value in env.items():
            old_env[key] = os.environ.get(key)
            os.environ[key] = value
    buf_out, buf_err = io.StringIO(), io.StringIO()
    old_streams = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin), buf_out, buf_err
    try:
        rc = cli.main(["--config", str(config)] + list(argv))
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_streams
        if env:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return rc, buf_out.getvalue(), buf_err.getvalue()


def _metrics(running, waiting):
    return ("tensorfold:num_requests_running{model=\"m1\"} %d\n"
            "tensorfold:num_requests_waiting %d\n" % (running, waiting))


@pytest.mark.parametrize("running,waiting,rc,verdict", [
    (0, 0, 0, "boost"),    # 空きあり
    (3, 0, 0, "boost"),
    (4, 0, 0, "reserve"),  # 満杯 = 起動可だが優先度下げ
])
def test_local_check_free_and_full_exit0(tmp_path, monkeypatch,
                                         running, waiting, rc, verdict):
    _fake_http(monkeypatch, body=_metrics(running, waiting))
    cfg = _local_config(tmp_path)
    code, out, err = _run_cli(["routing", "local-check", "test-server"], cfg)
    assert code == rc
    assert out == ""
    assert err == ("quota-canvas: local:test-server=%s(%d/4,待ち%d)\n"
                   % (verdict, running, waiting))


def test_local_check_queued_exit3(tmp_path, monkeypatch):
    _fake_http(monkeypatch, body=_metrics(2, 3))
    cfg = _local_config(tmp_path)
    code, out, err = _run_cli(["routing", "local-check", "test-server"], cfg)
    assert code == 3
    assert out == ""
    assert "stop" in err and "待ち3" in err


def test_local_check_unreachable_exit3(tmp_path, monkeypatch):
    _fake_http(monkeypatch, error=urllib.error.URLError("refused"))
    cfg = _local_config(tmp_path)
    code, _, err = _run_cli(["routing", "local-check", "test-server"], cfg)
    assert code == 3
    assert "stop" in err and "届かない" in err


def test_local_check_unknown_name_exit2(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("local-check が通信した")

    monkeypatch.setattr(providers.local, "_http_get_text", _boom)
    cfg = _local_config(tmp_path)
    code, _, err = _run_cli(["routing", "local-check", "no-such"], cfg)
    assert code == 2
    assert "no-such" in err


def test_local_check_disabled_name_exit2(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("local-check が無効なサーバーに通信した")

    monkeypatch.setattr(providers.local, "_http_get_text", _boom)
    cfg = _local_config(tmp_path, extra='enabled = false\n')
    code, _, err = _run_cli(["routing", "local-check", "test-server"], cfg)
    assert code == 2
    assert "test-server" in err


def test_local_check_force_bypasses(tmp_path, monkeypatch):
    _fake_http(monkeypatch, error=urllib.error.URLError("refused"))
    cfg = _local_config(tmp_path)
    code, _, err = _run_cli(["routing", "local-check", "test-server"], cfg,
                            env={"QUOTA_GATE_FORCE": "1"})
    assert code == 0


def test_local_check_uses_engine_metric_defaults(tmp_path, monkeypatch):
    body = "vllm:num_requests_running 2\nvllm:num_requests_waiting 0\n"
    _fake_http(monkeypatch, body=body)
    cfg = _local_config(tmp_path, extra='engine = "vllm"\n')
    code, _, err = _run_cli(["routing", "local-check", "test-server"], cfg)
    assert code == 0
    assert "boost(2/4" in err


# ------------------------------------------------------------------- set-key


def test_set_key_local_stores_under_local_account(monkeypatch, tmp_path):
    from quotacanvas import keychain

    stored = {}

    def fake_write(account, value):
        stored[account] = value

    monkeypatch.setattr(keychain, "write", fake_write)
    code, out, err = _run_cli(["set-key", "local:my-server"],
                              _local_config(tmp_path), stdin="dummy-key\n")
    assert code == 0
    assert stored == {"local:my-server": "dummy-key"}
    assert "local:my-server" in out
    assert "dummy-key" not in out + err


def test_set_key_local_delete(monkeypatch, tmp_path):
    from quotacanvas import keychain

    calls = []
    monkeypatch.setattr(keychain, "delete",
                        lambda account: calls.append(account) or True)
    code, out, _ = _run_cli(["set-key", "local:my-server", "--delete", "--json"],
                            _local_config(tmp_path))
    assert code == 0
    assert calls == ["local:my-server"]
    assert json.loads(out) == {"provider": "local:my-server", "deleted": True}


def test_set_key_local_rejects_bad_name(monkeypatch, tmp_path):
    from quotacanvas import keychain

    def boom(*args, **kwargs):
        raise AssertionError("不正な名前でキーチェーンに書いてはいけない")

    monkeypatch.setattr(keychain, "write", boom)
    code, out, _ = _run_cli(["set-key", "local:bad name"],
                            _local_config(tmp_path), stdin="k\n")
    assert code == 1
    assert "local:bad name" in json.loads(out)["error"]


# ------------------------------------------------- burn は local 枠だけのワーカーに効かない


def test_routing_burn_on_local_only_worker_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "BURN_PATH", tmp_path / "burn.json")
    monkeypatch.setattr(gate, "OFF_PATH", tmp_path / "off.json")
    cfg = _worker_config(tmp_path)
    code, out, err = _run_cli(["routing", "burn", "deepseek", "on"], cfg)
    assert code == 2
    assert "deepseek" in (out + err)
    assert not (tmp_path / "burn.json").exists()   # 書いていない


def test_routing_mode_burn_on_local_only_worker_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "BURN_PATH", tmp_path / "burn.json")
    monkeypatch.setattr(gate, "OFF_PATH", tmp_path / "off.json")
    cfg = _worker_config(tmp_path)
    code, out, err = _run_cli(["routing", "mode", "deepseek", "burn"], cfg)
    assert code == 2
    assert "deepseek" in (out + err)
    assert not (tmp_path / "burn.json").exists()


def test_routing_mode_off_and_list_note_for_local_only_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "BURN_PATH", tmp_path / "burn.json")
    monkeypatch.setattr(gate, "OFF_PATH", tmp_path / "off.json")
    cfg = _worker_config(tmp_path)
    # off (監視専用) は local 枠でも有効
    code, _, _ = _run_cli(["routing", "mode", "deepseek", "off"], cfg)
    assert code == 0
    # 一覧には burn が効かない注記が付く
    code, out, _ = _run_cli(["routing", "mode"], cfg)
    assert code == 0
    assert any(line.startswith("deepseek: off") and "local" in line
               for line in out.splitlines())


# ------------------------------------------------------------------- config


FULL_LOCAL = """
[providers.local.myserver]
enabled = true
label = "My Server"
base_url = "http://localhost:8000"
engine = "vllm"
capacity = 4
metrics_path = "/custom-metrics"
running_metric = "my:running"
waiting_metric = "my:waiting"
api_key_env = "MY_SERVER_API_KEY"
timeout = 2.5
color = "#AABBCC"

[routing.workers]
deepseek = ["local:myserver"]
"""


def test_config_parses_local_providers(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(FULL_LOCAL, encoding="utf-8")
    cfg = load_config(p)
    got = cfg.providers.local["myserver"]
    assert got.name == "myserver"
    assert got.label == "My Server"
    assert got.base_url == "http://localhost:8000"
    assert got.engine == "vllm"
    assert got.capacity == 4
    assert got.metrics_path == "/custom-metrics"
    assert got.running_metric == "my:running"
    assert got.waiting_metric == "my:waiting"
    assert got.api_key_env == "MY_SERVER_API_KEY"
    assert got.timeout == pytest.approx(2.5)
    assert got.color == "#AABBCC"
    assert got.enabled is True
    assert got.shown_name() == "My Server"
    assert cfg.routing.workers["deepseek"] == ["local:myserver"]


def test_config_local_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[providers.local.a]\nbase_url = "http://localhost:8000"\ncapacity = 1\n',
                 encoding="utf-8")
    got = load_config(p).providers.local["a"]
    assert (got.engine, got.enabled, got.metrics_path, got.label, got.color) == \
        ("tensorfold", True, "/metrics", "", "")
    assert got.running_metric is None and got.waiting_metric is None
    assert got.api_key_env is None
    assert got.timeout == pytest.approx(3.0)
    assert got.shown_name() == "a"


@pytest.mark.parametrize("table", [
    'capacity = 4\n',                                    # base_url なし
    'base_url = "http://localhost:8000"\n',              # capacity なし
    'base_url = "ftp://localhost:8000"\ncapacity = 4\n', # スキーム
    'base_url = "http://"\ncapacity = 4\n',              # ホスト名なし
    'base_url = "http://localhost:8000"\ncapacity = 0\n',
    'base_url = "http://localhost:8000"\ncapacity = 4\nengine = "unknown"\n',
    'base_url = "http://localhost:8000"\ncapacity = 4\nengine = "custom"\n',
    'base_url = "http://localhost:8000"\ncapacity = 4\nmetrics_path = "metrics"\n',
    'base_url = "http://localhost:8000"\ncapacity = 4\napi_key_env = "1BAD"\n',
    'base_url = "http://localhost:8000"\ncapacity = 4\ntimeout = 0\n',
    'base_url = "http://localhost:8000"\ncapacity = 4\ntimeout = 31\n',
    'base_url = "http://localhost:8000"\ncapacity = 4\nlabel = ""\n',
    'base_url = "http://user:pass@localhost:8000"\ncapacity = 4\n',   # userinfo
    'base_url = "http://localhost:8000/m?x=1"\ncapacity = 4\n',       # query
    'base_url = "http://localhost:8000/#frag"\ncapacity = 4\n',       # fragment
    'base_url = "http://localhost:99999/"\ncapacity = 4\n',           # ポート範囲外
    'base_url = "http://localhost:bad/"\ncapacity = 4\n',             # ポート非数値
    'base_url = "http://localhost:8000"\ncapacity = 4\ncolor = "red"\n',
    'base_url = "http://localhost:8000"\ncapacity = 4\ncolor = "#GGGGGG"\n',
])
def test_config_local_rejects_bad_tables(tmp_path, table):
    p = tmp_path / "config.toml"
    p.write_text("[providers.local.bad]\n" + table, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


@pytest.mark.parametrize("name", ["", "a b", "a/b", "x" * 41, "日本語",
                                  " lead", "trail ", "a\tb"])
def test_config_local_rejects_bad_names(tmp_path, name):
    p = tmp_path / "config.toml"
    p.write_text('[providers.local."%s"]\nbase_url = "http://localhost:8000"\ncapacity = 2\n'
                 % name, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_config_local_rejects_whitespace_variant_of_existing_name(tmp_path):
    # "a" と " a " が strip で黙って上書きし合わないよう、前後空白は拒否する
    p = tmp_path / "config.toml"
    p.write_text('[providers.local.a]\nbase_url = "http://localhost:8000"\ncapacity = 2\n'
                 '\n[providers.local." a"]\nbase_url = "http://localhost:8001"\ncapacity = 2\n',
                 encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_config_local_unknown_key_warns_and_ignores(tmp_path, caplog):
    p = tmp_path / "config.toml"
    p.write_text('[providers.local.a]\nbase_url = "http://localhost:8000"\n'
                 'capacity = 2\nno_such_key = 1\n', encoding="utf-8")
    with caplog.at_level("WARNING"):
        cfg = load_config(p)
    assert "providers.local.a.no_such_key" in caplog.text
    assert "a" in cfg.providers.local


def test_format_toml_roundtrip_local(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(FULL_LOCAL, encoding="utf-8")
    cfg = load_config(p)
    text = format_toml(cfg)
    assert '[providers.local.myserver]' in text
    assert 'color = "#AABBCC"' in text
    p2 = tmp_path / "roundtrip.toml"
    p2.write_text(text, encoding="utf-8")
    cfg2 = load_config(p2)
    assert cfg2.providers.local["myserver"] == cfg.providers.local["myserver"]
    assert cfg2.routing.workers == cfg.routing.workers


def test_format_toml_omits_empty_label(tmp_path):
    # label 未指定のときは label 行を書かない (空 label = 既定の名前表示)
    p = tmp_path / "config.toml"
    p.write_text('[providers.local.a]\nbase_url = "http://localhost:8000"\ncapacity = 2\n',
                 encoding="utf-8")
    text = format_toml(load_config(p))
    local_part = text.split("[providers.local.a]", 1)[1]
    assert "label" not in local_part.split("[", 1)[0]
    assert "color" not in local_part.split("[", 1)[0]


def test_config_to_dict_includes_local(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(FULL_LOCAL, encoding="utf-8")
    data = config_to_dict(load_config(p))
    local = data["providers"]["local"]["myserver"]
    assert local["base_url"] == "http://localhost:8000"
    assert local["capacity"] == 4
    assert local["engine"] == "vllm"
    assert local["enabled"] is True
    assert local["metrics_path"] == "/custom-metrics"
    assert local["timeout"] == pytest.approx(2.5)
    # 指定のあるオプションだけ載る (label / 指標名 / api_key_env / color)
    for key in ("label", "running_metric", "waiting_metric", "api_key_env", "color"):
        assert key in local
    # JSON として読み戻せる (app の config --set-json 経路)
    from quotacanvas.config import config_from_dict

    cfg2 = config_from_dict(json.loads(json.dumps(data)))
    assert cfg2.providers.local["myserver"].capacity == 4
    assert cfg2.providers.local["myserver"].color == "#AABBCC"


def test_local_color_reaches_render_and_status(tmp_path):
    from quotacanvas.cli import _brand_colors
    from quotacanvas.render import _brand_color
    from quotacanvas.status import _colors_payload

    p = tmp_path / "config.toml"
    p.write_text('[providers.local.a]\nbase_url = "http://localhost:8000"\ncapacity = 2\n'
                 'color = "#AABBCC"\n', encoding="utf-8")
    cfg = load_config(p)
    assert _brand_colors(cfg) == {"local:a": "#AABBCC"}
    assert _brand_color(_brand_colors(cfg), "local:a") == (0xAA, 0xBB, 0xCC)
    assert _colors_payload(cfg)["local:a"] == "#AABBCC"


def test_config_to_dict_without_local_has_no_local_key(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("", encoding="utf-8")
    assert "local" not in config_to_dict(load_config(p))["providers"]


# -------------------------------------------------------------------- Poller


def _local_usage(**kw):
    return ProviderUsage("local:test-server", "test-server", [],
                         kw.get("error"), NOW,
                         slots=kw.get("slots"))


def test_poller_appends_local_fetches(tmp_path, monkeypatch):
    _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    history = History(tmp_path / "history.jsonl")
    poller = cli.Poller(history, enabled=set(),
                        last_good_path=tmp_path / "last-good.json",
                        local={"test-server": entry()})
    usages = poller.fetch(NOW)
    assert [u.provider for u in usages] == ["local:test-server"]
    assert usages[0].slots == LocalSlots(3, 0, 4)
    # 成功しても last-good の対象にしない (毎回生の値を使う)
    assert "local:test-server" not in poller.last_good


def test_poller_local_failure_is_not_smoothed(tmp_path, monkeypatch):
    _fake_http(monkeypatch, body=TENSORFOLD_METRICS)
    history = History(tmp_path / "history.jsonl")
    poller = cli.Poller(history, enabled=set(),
                        last_good_path=tmp_path / "last-good.json",
                        local={"test-server": entry()})
    ok = poller.fetch(NOW.replace(minute=1))
    assert ok[0].slots is not None
    _fake_http(monkeypatch, error=urllib.error.URLError("refused"))
    broken = poller.fetch(NOW.replace(minute=2))
    # 失敗時も last-good を差さず、error のまま返す
    assert broken[0].error == "network error"
    assert broken[0].slots is None
    assert "local:test-server" not in poller.last_good


# --------------------------------------------------------------------- 描画


def test_render_local_card_large_smoke(monkeypatch):
    usage = _local_usage(slots=LocalSlots(1, 0, 4))
    img = render([(usage, None)], NOW)
    assert img.size[0] > 0
    assert img.size[1] > 0


def test_render_local_card_small_smoke():
    usage = _local_usage(slots=LocalSlots(4, 0, 4))
    img = render([(usage, None)], NOW,
                 layout=[PanelSpec(main="local:test-server", size="small")])
    assert img.size[1] > 0


def test_render_local_error_card_smoke():
    usage = _local_usage(error="HTTP 500")
    img = render([(usage, None)], NOW)
    assert img.size[1] > 0


def test_render_local_states_dont_crash():
    from quotacanvas.render import _local_state

    assert _local_state(LocalSlots(0, 0, 4)) == "free"
    assert _local_state(LocalSlots(4, 0, 4)) == "full"
    assert _local_state(LocalSlots(2, 1, 4)) == "queued"
    for slots in (LocalSlots(0, 0, 1), LocalSlots(1, 0, 1), LocalSlots(0, 1, 1),
                  LocalSlots(9, 9, 4)):
        img = render([(_local_usage(slots=slots), None)], NOW)
        assert img.size == (1920, 462)


def test_build_cards_makes_one_local_card(tmp_path):
    from quotacanvas.cli import build_cards

    ok = _local_usage(slots=LocalSlots(1, 0, 4))
    broken = _local_usage(error="network error")
    cards = build_cards([ok, broken], History(tmp_path / "history.jsonl"), NOW)
    assert [c[0].provider for c in cards] == ["local:test-server", "local:test-server"]
    assert [c[1] for c in cards] == [None, None]
