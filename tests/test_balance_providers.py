"""Public API fixtures only; no network or real credentials."""
from datetime import datetime, timedelta, timezone
import http.client
import json
import logging
import urllib.error

import pytest

from quotacanvas import gate, keychain
from quotacanvas.cli import build_cards
from quotacanvas.config import Config, Routing, config_from_dict, config_to_dict
from quotacanvas.history import History
from quotacanvas.lastgood import LastGood
from quotacanvas.models import Balance, ProviderUsage
from quotacanvas.providers import deepseek, kimi, openrouter
from quotacanvas.render import auto_layout, render
from quotacanvas.status import build_status

NOW = datetime(2026, 12, 31, 12, tzinfo=timezone.utc)
DUMMY = "sk-test-dummy"
KEY = {"data": {"limit": 100.0, "limit_remaining": 74.5, "limit_reset": "monthly"}}
CREDITS = {"data": {"total_credits": 100.5, "total_usage": 25.75}}
DEEPSEEK = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "110.00", "granted_balance": "10.00", "topped_up_balance": "100.00"}]}
KIMI = {"code": 0, "data": {"available_balance": 49.58, "voucher_balance": 46.58, "cash_balance": 3.0}, "scode": "0x0", "status": True}


@pytest.mark.parametrize("period,reset,length", [
    ("daily", datetime(2027, 1, 1, tzinfo=timezone.utc), 1),
    ("weekly", datetime(2027, 1, 4, tzinfo=timezone.utc), 7),
    ("monthly", datetime(2027, 1, 1, tzinfo=timezone.utc), 31),
])
def test_openrouter_window(period, reset, length):
    data = {"data": dict(KEY["data"], limit_reset=period)}
    windows, balances = openrouter.parse_key(data, NOW)
    assert not balances
    window, = windows
    assert window.used_percent == 25.5
    assert window.resets_at == reset
    assert window.length == timedelta(days=length)


def test_openrouter_missing_limit_and_remaining():
    windows, balances = openrouter.parse_key({"data": {"limit": None, "usage_monthly": 12.5, "usage": 33}}, NOW)
    assert not windows and balances == [Balance("USD", 12.5, "今月の使用")]
    windows, balances = openrouter.parse_key({"data": {"limit": None, "usage": 33}}, NOW)
    assert not windows and balances == [Balance("USD", 33, "累計の使用")]
    windows, balances = openrouter.parse_key({"data": dict(KEY["data"], limit_remaining=None, usage_monthly=7)}, NOW)
    assert not windows and balances == [Balance("USD", 7, "今月の使用")]
    assert openrouter.parse_key({"data": dict(KEY["data"], limit_remaining=-1)}, NOW)[0][0].used_percent == 100
    assert openrouter.parse_key({"data": dict(KEY["data"], limit_remaining=200)}, NOW)[0][0].used_percent == 0
    assert openrouter.parse_credits(CREDITS).amount == 74.75


@pytest.mark.parametrize("module,env,url,payload", [
    (openrouter, "OPENROUTER_API_KEY", openrouter.KEY_URL, KEY),
    (deepseek, "DEEPSEEK_API_KEY", deepseek.BALANCE_URL, DEEPSEEK),
    (kimi, "MOONSHOT_API_KEY", kimi.BALANCE_URL, KIMI),
])
def test_detect_and_fetch(monkeypatch, module, env, url, payload):
    monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(keychain, "read", lambda provider: None)
    assert not module.detect().detected
    monkeypatch.setenv(env, " " + DUMMY + " ")
    assert module.detect().detected and env in module.detect().source
    calls = []
    def get(endpoint, headers):
        calls.append((endpoint, headers))
        return CREDITS if endpoint == openrouter.CREDITS_URL else payload
    monkeypatch.setattr(module, "http_get_json", get)
    usage = module.fetch(NOW)
    assert calls[0][0] == url and all(c[1]["Authorization"] == "Bearer " + DUMMY for c in calls)
    assert usage.fetched_at == NOW and usage.error is None
    assert usage.balances
    monkeypatch.setattr(keychain, "read", lambda provider: "sk-test-keychain")
    assert "keychain" in module.detect().source.lower() or "キーチェーン" in module.detect().source
    calls.clear()
    module.fetch(NOW)
    assert calls[0][1]["Authorization"] == "Bearer sk-test-keychain"


def test_credits_forbidden_keeps_key_result(monkeypatch, caplog):
    monkeypatch.setattr(keychain, "read", lambda _: DUMMY)
    monkeypatch.setattr(openrouter, "_credits_warning_logged", False)
    def get(url, headers):
        if url == openrouter.CREDITS_URL:
            raise urllib.error.HTTPError(url, 403, DUMMY, {}, None)
        return KEY
    monkeypatch.setattr(openrouter, "http_get_json", get)
    result = openrouter.fetch(NOW)
    assert result.windows and not result.balances and result.error is None
    assert "403" in caplog.text and DUMMY not in caplog.text


def test_deepseek_and_kimi_balance():
    assert deepseek.parse_balance(DEEPSEEK) == [Balance("CNY", 110.0)]
    assert deepseek.parse_balance(dict(DEEPSEEK, is_available=False))[0].label
    assert kimi.parse_balance(KIMI) == [Balance("USD", 49.58)]
    assert kimi.PROVIDER.shown_name == "Kimi (Moonshot)"


@pytest.mark.parametrize("fn,data", [
    (lambda d: openrouter.parse_key(d, NOW), {}),
    (openrouter.parse_credits, {"data": {"total_credits": "nan", "total_usage": 0}}),
    (deepseek.parse_balance, {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "broken"}]}),
    (kimi.parse_balance, dict(KIMI, code=1, message=DUMMY)),
    (kimi.parse_balance, dict(KIMI, status=False)),
    (kimi.parse_balance, {"code": 0, "status": True, "data": {}}),
])
def test_bad_responses(fn, data):
    with pytest.raises((ValueError, KeyError, TypeError)) as exc:
        fn(data)
    assert DUMMY not in str(exc.value)


def test_balance_display_status_lastgood_and_routing(tmp_path, monkeypatch):
    from quotacanvas import render as render_module
    usage = ProviderUsage("deepseek", "DeepSeek", fetched_at=NOW, balances=deepseek.parse_balance(DEEPSEEK))
    cards = build_cards([usage], History(tmp_path / "history"), NOW)
    assert len(cards) == 1 and cards[0][1] is None
    assert auto_layout(cards)[0].size == "small"
    text = []
    original = render_module._S.text
    def record(self, pos, value, *args, **kwargs):
        text.append(value)
        return original(self, pos, value, *args, **kwargs)
    monkeypatch.setattr(render_module._S, "text", record)
    render(cards, NOW)
    assert any("¥110.00" in str(v) for v in text)
    payload = build_status(Config(), cards, [usage], NOW)
    assert payload["balances"][0]["amount"] == 110
    assert payload["balances"][0]["fetched_at"]
    assert not payload["errors"] and not payload["windows"]
    store = LastGood(tmp_path / "lastgood.json")
    store.set(usage)
    assert LastGood(tmp_path / "lastgood.json").data["deepseek"].balances == usage.balances
    assert not gate.default_workers({"windows": [{"provider": "openrouter", "id": "openrouter:monthly"}], "errors": [{"provider": "deepseek"}, {"provider": "kimi"}]})
    assert set(config_to_dict(config_from_dict({}))["providers"]) >= {"openrouter", "deepseek", "kimi"}


def test_routing_explicit_workers_ignore_balance_providers():
    status = {"updated_at": NOW.isoformat(), "windows": [
        {"provider": "openrouter", "id": "openrouter:daily", "remaining": 1, "status": "exhausted"},
        {"provider": "claude", "id": "claude:weekly", "remaining": 80, "status": "spare", "fetched_at": NOW.isoformat()},
    ]}
    cfg = Routing(workers={"openrouter": ["openrouter:daily"], "mixed": ["openrouter:daily", "claude:weekly"]})
    workers = gate.evaluate(status, cfg, {}, NOW).workers
    assert [w.name for w in workers] == ["mixed"]
    assert workers[0].windows == ["claude:weekly"]


@pytest.mark.parametrize("pid", ["openrouter", "deepseek", "kimi"])
def test_set_key(monkeypatch, capsys, pid):
    from quotacanvas import cli
    from types import SimpleNamespace
    saved = []
    monkeypatch.setattr(cli, "_read_secret", lambda prompt: DUMMY)
    monkeypatch.setattr(keychain, "write", lambda provider, key: saved.append((provider, key)))
    assert cli._cmd_set_key(SimpleNamespace(provider=pid, delete=False, json=True)) == 0
    assert saved == [(pid, DUMMY)]
    assert DUMMY not in capsys.readouterr().out


def test_openrouter_balance_and_window_render(tmp_path, monkeypatch):
    from quotacanvas import render as render_module
    usage = ProviderUsage("openrouter", "OpenRouter", openrouter.parse_key(KEY, NOW)[0], fetched_at=NOW,
                          balances=[openrouter.parse_credits(CREDITS)])
    cards = build_cards([usage], History(tmp_path / "history"), NOW)
    captured = []
    original = render_module._S.text
    def record(self, pos, value, *args, **kwargs):
        captured.append(value)
        return original(self, pos, value, *args, **kwargs)
    monkeypatch.setattr(render_module._S, "text", record)
    render(cards, NOW)
    assert any("$74.75" in str(v) for v in captured)
    assert build_status(Config(), cards, [usage], NOW)["balances"][0]["amount"] == 74.75


def test_stale_insufficient_balance_is_visible(tmp_path, monkeypatch):
    from dataclasses import replace
    from quotacanvas import render as render_module
    balances = deepseek.parse_balance(dict(DEEPSEEK, is_available=False))
    original_usage = ProviderUsage("deepseek", "DeepSeek", fetched_at=NOW, balances=balances)
    store = LastGood(tmp_path / "lastgood.json")
    store.set(original_usage)
    usage = replace(LastGood(tmp_path / "lastgood.json").data["deepseek"], stale_error="HTTP 429")
    cards = build_cards([usage], History(tmp_path / "history"), NOW)
    texts = []
    original = render_module._S.text
    def record(self, pos, value, *args, **kwargs):
        texts.append(value)
        return original(self, pos, value, *args, **kwargs)
    monkeypatch.setattr(render_module._S, "text", record)
    render(cards, NOW)
    assert any("残高不足" in str(v) for v in texts)
    status_balance = build_status(Config(), cards, [usage], NOW)["balances"][0]
    assert status_balance["stale_error"] == "HTTP 429"
    assert status_balance["fetched_at"] == NOW.astimezone().isoformat(timespec="seconds")


@pytest.mark.parametrize("period", [None, "new-period"])
def test_openrouter_nonrecurring_limit_is_balance(period):
    windows, balances = openrouter.parse_key({"data": dict(KEY["data"], limit_reset=period)}, NOW)
    assert windows == []
    assert balances == [Balance("USD", 74.5, "上限まで残り")]


@pytest.mark.parametrize("year,month,days", [(2026, 2, 28), (2028, 2, 29), (2026, 4, 30), (2026, 12, 31)])
def test_openrouter_monthly_actual_calendar_length(year, month, days):
    now = datetime(year, month, 15, 12, tzinfo=timezone.utc)
    windows, _ = openrouter.parse_key(KEY, now)
    window, = windows
    assert window.length == timedelta(days=days)
    assert window.starts_at == datetime(year, month, 1, tzinfo=timezone.utc)


def test_openrouter_credits_transport_failure_logs_once_and_keeps_usage(monkeypatch, caplog):
    monkeypatch.setattr(keychain, "read", lambda _: DUMMY)
    monkeypatch.setattr(openrouter, "_credits_warning_logged", False)
    def get(url, headers):
        if url == openrouter.CREDITS_URL:
            raise http.client.HTTPException(DUMMY)
        return {"data": {"limit": None, "usage_monthly": 25.5, "usage": 99}}
    monkeypatch.setattr(openrouter, "http_get_json", get)
    with caplog.at_level(logging.DEBUG, logger="quotacanvas"):
        one = openrouter.fetch(NOW)
        two = openrouter.fetch(NOW)
    assert one.balances == two.balances == [Balance("USD", 25.5, "今月の使用")]
    assert [r.levelname for r in caplog.records] == ["WARNING", "DEBUG"]
    assert DUMMY not in caplog.text and "HTTPException" in caplog.text


def test_openrouter_key_balance_and_credits_are_both_kept(monkeypatch):
    monkeypatch.setattr(keychain, "read", lambda _: DUMMY)
    monkeypatch.setattr(openrouter, "http_get_json", lambda url, headers:
                        CREDITS if url == openrouter.CREDITS_URL else {"data": {"limit": None, "usage_monthly": 10}})
    assert openrouter.fetch(NOW).balances == [Balance("USD", 10, "今月の使用"), Balance("USD", 74.75)]


@pytest.mark.parametrize("data,label,amount", [
    ({"limit": None, "usage_monthly": 12.5}, "今月の使用", 12.5),
    ({"limit": None, "usage": 33}, "累計の使用", 33),
    (dict(KEY["data"], limit_reset=None), "上限まで残り", 74.5),
])
def test_key_balance_label_in_status_and_render(tmp_path, monkeypatch, data, label, amount):
    from quotacanvas import render as render_module
    windows, balances = openrouter.parse_key({"data": data}, NOW)
    usage = ProviderUsage("openrouter", "OpenRouter", windows, fetched_at=NOW, balances=balances)
    cards = build_cards([usage], History(tmp_path / "history"), NOW)
    texts = []
    original = render_module._S.text
    def record(self, pos, value, *args, **kwargs):
        texts.append(value)
        return original(self, pos, value, *args, **kwargs)
    monkeypatch.setattr(render_module._S, "text", record)
    render(cards, NOW)
    assert f"{label} ${amount:.2f}" in texts
    assert build_status(Config(), cards, [usage], NOW)["balances"][0]["label"] == label


@pytest.mark.parametrize("size", ["large", "small"])
def test_window_renders_advice_and_balance(tmp_path, monkeypatch, size):
    from quotacanvas import render as render_module
    from quotacanvas.render import PanelSpec, advice_text
    window = openrouter.parse_key(KEY, NOW)[0][0]
    usage = ProviderUsage("openrouter", "OpenRouter", [window], fetched_at=NOW,
                          balances=[Balance("USD", 74.75, "テスト")])
    cards = build_cards([usage], History(tmp_path / "history"), NOW)
    advice = advice_text(cards[0][1], size == "small", NOW)
    texts = []
    original = render_module._S.text
    def record(self, pos, value, *args, **kwargs):
        texts.append(value)
        return original(self, pos, value, *args, **kwargs)
    monkeypatch.setattr(render_module._S, "text", record)
    render(cards, NOW, layout=[PanelSpec(main=window.id, size=size)])
    assert any("テスト $74.75" in str(v) for v in texts)
    assert advice in texts


def test_lastgood_skips_only_broken_balances(tmp_path):
    path = tmp_path / "last-good.json"
    path.write_text(json.dumps({"openrouter": {"name": "OpenRouter", "fetched_at": NOW.isoformat(),
        "balances": [None, {"currency": "USD", "amount": "broken"},
                     {"currency": "USD", "amount": 3.5, "label": "ok"},
                     {"amount": 1}, {"currency": "USD", "amount": "nan"}]}}, ensure_ascii=False))
    usage = LastGood(path).data["openrouter"]
    assert usage.name == "OpenRouter" and usage.fetched_at == NOW
    assert usage.balances == [Balance("USD", 3.5, "ok")]


def test_example_config_registers_services():
    import tomllib
    from pathlib import Path
    data = tomllib.loads(Path("config.example.toml").read_text())
    assert all(data["providers"][pid]["enabled"] for pid in ("openrouter", "deepseek", "kimi"))


@pytest.mark.parametrize("size", ["large", "small"])
def test_window_advice_and_balance_fit_four_columns(tmp_path, monkeypatch, size):
    from dataclasses import replace
    from quotacanvas import render as render_module
    from quotacanvas.render import PanelSpec, advice_text
    window = openrouter.parse_key(KEY, NOW)[0][0]
    usage = ProviderUsage("openrouter", "OpenRouter", [window], fetched_at=NOW,
                          balances=[Balance("USD", 74.75)])
    others = [ProviderUsage(pid, pid, [replace(window, provider=pid)], fetched_at=NOW)
              for pid in ("claude", "codex", "glm")]
    cards = build_cards([usage] + others, History(tmp_path / "history"), NOW)
    target = next(p for u, p in cards if u.provider == "openrouter")
    from quotacanvas.models import PaceStatus
    target.status = PaceStatus.OVER
    text = []
    original = render_module._S.text
    def record(self, pos, value, *args, **kwargs):
        text.append(value)
        return original(self, pos, value, *args, **kwargs)
    monkeypatch.setattr(render_module._S, "text", record)
    specs = [PanelSpec(main=window.id, size=size)] + [PanelSpec(main=p) for p in ("claude", "codex", "glm")]
    render(cards, NOW, layout=specs)
    assert any("残高 $74.75" in str(v) for v in text)
    assert advice_text(target, True, NOW) in text


@pytest.mark.parametrize("pid", ["openrouter", "deepseek", "kimi"])
def test_missing_key_providers_do_not_render_errors(tmp_path, pid):
    from quotacanvas.i18n import t
    usage = ProviderUsage(pid, pid, error=t("providers.api_key_not_set"), fetched_at=NOW)
    assert build_cards([usage], History(tmp_path / "history"), NOW) == []


def test_openrouter_labels_follow_english_locale():
    from quotacanvas import i18n
    i18n.set_language("en")
    assert openrouter.parse_key({"data": {"limit": None, "usage_monthly": 2}}, NOW)[1][0].label == "Used this month"
    assert openrouter.parse_key({"data": {"limit": None, "usage": 3}}, NOW)[1][0].label == "Total usage"
    assert openrouter.parse_key({"data": dict(KEY["data"], limit_reset=None)}, NOW)[1][0].label == "Remaining to limit"
