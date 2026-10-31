"""Tests for the providers package: pure parsers + fetch_all/detect_all.

Network and keychain access are replaced by monkeypatching the per-service
modules' read_*_credentials / http_get_json, so no real I/O happens.
"""

from __future__ import annotations

import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.models import ProviderUsage, Window
from quotacanvas.providers import (
    CredentialError,
    Detection,
    Provider,
    fetch_all,
    parse_claude,
    parse_codex,
    parse_glm,
)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc


def _dt(y, m, d, h=0, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


# ------------------------------------------------------------ parse_claude

CLAUDE_PAYLOAD = {
    "five_hour": {"utilization": 42.5, "resets_at": "2026-10-24T15:00:00Z"},
    "seven_day": {"utilization": 12.3, "resets_at": "2026-10-27T09:00:00+09:00"},
    "limits": [
        {
            "kind": "weekly_scoped",
            "scope": {"model": {"display_name": "Opus"}},
            "percent": 55.0,
            "resets_at": "2026-10-30T00:00:00Z",
        },
        {  # display_name が null → 飛ばす
            "kind": "weekly_scoped",
            "scope": {"model": {"display_name": None}},
            "percent": 10.0,
            "resets_at": "2026-10-30T00:00:00Z",
        },
        None,  # null エントリ → 飛ばす
        {  # kind違い → 飛ばす
            "kind": "time_scoped",
            "scope": {"model": {"display_name": "Sonnet"}},
            "percent": 1.0,
            "resets_at": "2026-10-24T10:00:00Z",
        },
    ],
}


def test_parse_claude_windows():
    name, windows = parse_claude(CLAUDE_PAYLOAD, NOW, "max")
    assert name == "Claude Max"
    by_key = {w.key: w for w in windows}
    assert set(by_key) == {"5h", "weekly", "weekly_opus"}

    five = by_key["5h"]
    assert five.provider == "claude"
    assert five.label == "5時間"
    assert five.used_percent == pytest.approx(42.5)
    assert five.resets_at == _dt(2026, 10, 24, 15)
    assert five.length == timedelta(hours=5)
    assert five.id == "claude:5h"

    weekly = by_key["weekly"]
    assert weekly.label == "週間"
    assert weekly.used_percent == pytest.approx(12.3)
    assert weekly.resets_at == _dt(2026, 10, 27, 0)  # +09:00 は UTC へ
    assert weekly.length == timedelta(days=7)

    opus = by_key["weekly_opus"]
    assert opus.label == "週間 Opus"
    assert opus.used_percent == pytest.approx(55.0)
    assert opus.resets_at == _dt(2026, 10, 30)
    assert opus.length == timedelta(days=7)


def test_parse_claude_null_windows_are_skipped():
    name, windows = parse_claude({"five_hour": None, "seven_day": None}, NOW, "pro")
    assert name == "Claude Pro"
    assert windows == []


def test_claude_keychain_tier_adds_multiplier(monkeypatch):
    """max は倍率で新しいプラン名 (5x -> Max / 20x -> Ultra) に読み替える。"""
    import json
    import subprocess
    from types import SimpleNamespace
    from quotacanvas.providers import claude as claude_mod

    def keychain(oauth):
        out = json.dumps({"claudeAiOauth": dict(accessToken="tok", **oauth)})
        monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                            SimpleNamespace(returncode=0, stdout=out))
        return claude_mod.read_claude_credentials()[1]

    sub = keychain({"subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x"})
    assert parse_claude({}, NOW, sub)[0] == "Claude Ultra"
    sub = keychain({"subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_5x"})
    assert parse_claude({}, NOW, sub)[0] == "Claude Max"
    sub = keychain({"subscriptionType": "ultra",
                    "rateLimitTier": "default_claude_max_20x"})
    assert parse_claude({}, NOW, sub)[0] == "Claude Ultra"
    sub = keychain({"subscriptionType": "pro",
                    "rateLimitTier": "default_claude_ai"})
    assert parse_claude({}, NOW, sub)[0] == "Claude Pro"
    assert parse_claude({}, NOW, keychain({"subscriptionType": "max"}))[0] \
        == "Claude Max"


def test_parse_claude_resets_at_missing_is_none():
    _, windows = parse_claude({"five_hour": {"utilization": 1.0, "resets_at": None}}, NOW, "max")
    assert windows[0].resets_at is None


# ------------------------------------------------------------ parse_codex

CODEX_PAYLOAD = {
    "plan_type": "pro",
    "rate_limit": {
        "primary_window": {
            "used_percent": 12.5,
            "limit_window_seconds": 18000,
            "reset_at": 1792872000,
        },
        "secondary_window": {
            "used_percent": 3.0,
            "limit_window_seconds": 604800,
            "reset_at": 1793318400,
        },
    },
}


def test_parse_codex_windows():
    name, windows = parse_codex(CODEX_PAYLOAD, NOW)
    assert name == "Codex Pro"
    by_key = {w.key: w for w in windows}
    assert set(by_key) == {"5h", "weekly"}

    five = by_key["5h"]
    assert five.label == "5時間"
    assert five.used_percent == pytest.approx(12.5)
    assert five.resets_at == _dt(2026, 10, 24, 20)  # 1792872000
    assert five.length == timedelta(hours=5)

    weekly = by_key["weekly"]
    assert weekly.label == "週間"
    assert weekly.used_percent == pytest.approx(3.0)
    assert weekly.resets_at == _dt(2026, 10, 30)  # 1793318400
    assert weekly.length == timedelta(days=7)


def test_parse_codex_null_secondary_window():
    data = {
        "plan_type": "max",
        "rate_limit": {
            "primary_window": {"used_percent": 1.0, "limit_window_seconds": 18000,
                               "reset_at": 1792872000},
            "secondary_window": None,
        },
    }
    name, windows = parse_codex(data, NOW)
    assert name == "Codex Max"
    assert [w.key for w in windows] == ["5h"]


def test_parse_codex_other_window_lengths_use_seconds():
    data = {
        "plan_type": "team",
        "rate_limit": {
            "primary_window": {"used_percent": 7.0, "limit_window_seconds": 86400,
                               "reset_at": 1792872000},
            "secondary_window": None,
        },
    }
    _, windows = parse_codex(data, NOW)
    assert windows[0].key == "1d"
    assert windows[0].label == "1日"
    assert windows[0].length == timedelta(days=1)


# -------------------------------------------------------------- parse_glm

GLM_PAYLOAD = {
    "level": "pro",
    "data": {
        "level": "pro",
        "limits": [
            {"type": "TOKENS_LIMIT", "percentage": 21.0,
             "nextResetTime": 1792872000000, "unit": 3, "number": 5},  # 5h
            {"type": "TOKENS_LIMIT", "percentage": 8.0,
             "nextResetTime": 1793318400000, "unit": 1, "number": 7},  # 7d
            {"type": "TOKENS_LIMIT", "percentage": 0.0,
             "nextResetTime": 1795478400000, "unit": 5, "number": 1},  # 30d
            {"type": "TIME_LIMIT", "percentage": 50.0,
             "nextResetTime": 1792872000000, "unit": 3, "number": 1},  # MCP は無視
            {"type": "TOKENS_LIMIT", "percentage": 9.0,
             "nextResetTime": 1792872000000, "unit": 99, "number": 1},  # unknown unit
        ],
    },
}


def test_parse_glm_windows():
    name, windows = parse_glm(GLM_PAYLOAD, NOW)
    assert name == "GLM Pro"
    by_key = {w.key: w for w in windows}
    assert set(by_key) == {"5h", "weekly", "30d"}  # TIME_LIMIT と不明 unit は無い

    five = by_key["5h"]
    assert five.label == "5時間"
    assert five.provider == "glm"
    assert five.used_percent == pytest.approx(21.0)
    assert five.resets_at == _dt(2026, 10, 24, 20)  # ms → 1792872000s
    assert five.length == timedelta(hours=5)

    weekly = by_key["weekly"]
    assert weekly.label == "週間"
    assert weekly.used_percent == pytest.approx(8.0)
    assert weekly.length == timedelta(days=7)

    month = by_key["30d"]
    assert month.label == "30日"
    assert month.used_percent == pytest.approx(0.0)
    assert month.length == timedelta(days=30)


def test_parse_glm_empty_data():
    name, windows = parse_glm({}, NOW)
    assert name == "GLM"
    assert windows == []


# -------------------------------------------------------------- fetch_all

@pytest.fixture()
def no_credentials(monkeypatch):
    """Make all credential readers return dummies by default."""
    monkeypatch.setattr(providers.claude, "read_claude_credentials", lambda: ("claude-token", "max"))
    monkeypatch.setattr(providers.codex, "read_codex_credentials", lambda: ("codex-token", "acct-1"))
    monkeypatch.setattr(providers.glm, "read_glm_api_key", lambda: "glm-key")


def _patch_http(monkeypatch, payloads):
    captured = []

    def fake_http(url, headers, timeout=providers.TIMEOUT):
        captured.append((url, headers))
        if url not in payloads:
            raise AssertionError("unexpected url " + url)
        return payloads[url]

    for mod in (providers.claude, providers.codex, providers.glm):
        monkeypatch.setattr(mod, "http_get_json", fake_http)
    return captured


def _patch_http_fn(monkeypatch, fn):
    """http_get_json を 1 関数に差し替える (payload 込みでない場合用)。"""
    for mod in (providers.claude, providers.codex, providers.glm):
        monkeypatch.setattr(mod, "http_get_json", fn)


def test_fetch_all_success_order_and_headers(monkeypatch, no_credentials):
    captured = _patch_http(monkeypatch, {
        providers.CLAUDE_USAGE_URL: CLAUDE_PAYLOAD,
        providers.CODEX_USAGE_URL: CODEX_PAYLOAD,
        providers.GLM_USAGE_URL: GLM_PAYLOAD,
    })

    results = fetch_all(NOW, enabled={"claude", "codex", "glm"})

    assert [p.provider for p in results] == ["claude", "codex", "glm"]
    assert [p.name for p in results] == ["Claude Max", "Codex Pro", "GLM Pro"]
    assert all(p.error is None for p in results)
    assert all(len(p.windows) > 0 for p in results)
    assert all(p.fetched_at == NOW for p in results)

    headers = {url: hdrs for url, hdrs in captured}
    assert headers[providers.CLAUDE_USAGE_URL]["Authorization"] == "Bearer claude-token"
    assert headers[providers.CLAUDE_USAGE_URL]["anthropic-beta"] == "oauth-2025-04-20"
    assert headers[providers.CODEX_USAGE_URL]["Authorization"] == "Bearer codex-token"
    assert headers[providers.CODEX_USAGE_URL]["ChatGPT-Account-Id"] == "acct-1"
    assert headers[providers.GLM_USAGE_URL]["Authorization"] == "glm-key"  # Bearer なし
    assert headers[providers.GLM_USAGE_URL]["Accept-Language"] == "en-US,en"
    for hdrs in headers.values():
        assert hdrs["User-Agent"] == "quota-canvas"


def test_fetch_all_isolates_provider_errors(monkeypatch, no_credentials):
    def selective(url, headers, timeout=providers.TIMEOUT):
        if url == providers.CODEX_USAGE_URL:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)
        return {providers.CLAUDE_USAGE_URL: CLAUDE_PAYLOAD,
                providers.GLM_USAGE_URL: GLM_PAYLOAD}[url]

    _patch_http_fn(monkeypatch, selective)

    results = fetch_all(NOW)
    assert results[0].error is None
    assert results[1].provider == "codex"
    assert results[1].error == "HTTP 401"
    assert results[1].windows == []
    assert results[2].error is None


def test_fetch_all_missing_credentials(monkeypatch, no_credentials):
    def fake_http(url, headers, timeout=providers.TIMEOUT):
        return {providers.CLAUDE_USAGE_URL: CLAUDE_PAYLOAD,
                providers.GLM_USAGE_URL: GLM_PAYLOAD}[url]

    _patch_http_fn(monkeypatch, fake_http)

    def no_auth():
        raise CredentialError("認証情報なし")

    monkeypatch.setattr(providers.codex, "read_codex_credentials", no_auth)
    results = fetch_all(NOW)
    assert results[1].error == "認証情報なし"
    assert results[0].error is None
    assert results[2].error is None


def test_fetch_all_never_leaks_tokens(monkeypatch, no_credentials):
    secret = "sk-ant-SUPERSECRET-TOKEN"

    def leaking(url, headers, timeout=providers.TIMEOUT):
        if url == providers.GLM_USAGE_URL:
            raise RuntimeError("bad request for " + secret)
        return {providers.CLAUDE_USAGE_URL: CLAUDE_PAYLOAD,
                providers.CODEX_USAGE_URL: CODEX_PAYLOAD}[url]

    _patch_http_fn(monkeypatch, leaking)
    results = fetch_all(NOW)
    assert results[2].error == "RuntimeError"
    for usage in results:
        assert secret not in (usage.error or "")
        for window in usage.windows:
            assert secret not in window.label


def test_fetch_all_timeout_reason(monkeypatch, no_credentials):
    import socket

    def slow(url, headers, timeout=providers.TIMEOUT):
        if url == providers.GLM_USAGE_URL:
            raise socket.timeout("timed out")
        return {providers.CLAUDE_USAGE_URL: CLAUDE_PAYLOAD,
                providers.CODEX_USAGE_URL: CODEX_PAYLOAD}[url]

    _patch_http_fn(monkeypatch, slow)
    results = fetch_all(NOW)
    assert results[2].error == "timeout"


def test_window_identity_variants():
    assert providers._window_identity(604800) == ("weekly", "週間", timedelta(days=7))
    assert providers._window_identity(18000) == ("5h", "5時間", timedelta(hours=5))
    assert providers._window_identity(2 * 86400) == ("2d", "2日", timedelta(days=2))
    assert providers._window_identity(3 * 3600) == ("3h", "3時間", timedelta(hours=3))


# ------------------------------------------------- Provider / REGISTRY 構造

def test_registry_shape():
    assert list(providers.REGISTRY) == ["claude", "codex", "glm", "grok", "gemini", "openrouter", "deepseek", "kimi"]
    for pid, provider in providers.REGISTRY.items():
        assert provider.id == pid
        assert callable(provider.detect) and callable(provider.fetch)
        assert isinstance(provider.needs_key, bool)
    assert providers.REGISTRY["glm"].needs_key is True
    assert providers.REGISTRY["glm"].shown_name == "GLM (Z.ai)"
    assert providers.REGISTRY["gemini"].shown_name == "Gemini (Antigravity)"
    assert providers.REGISTRY["claude"].needs_key is False


def _install(monkeypatch, pid, detection=None, usage=None):
    """REGISTRY の 1 サービスをテスト用の detect/fetch に差し替える。"""
    def detect():
        return detection if detection is not None else Detection(True, "テストの認証情報")

    def fetch(now):
        return usage if usage is not None else ProviderUsage(pid, pid.title(), [], None, now)

    monkeypatch.setitem(providers.REGISTRY, pid,
                        replace(providers.REGISTRY[pid], detect=detect, fetch=fetch))


# ---------------------------------------------------------------- detect_all

def test_detect_all_reports_plan_and_windows(monkeypatch):
    win = Window("codex", "weekly", "週間", 3.0, NOW + timedelta(days=7), timedelta(days=7))
    _install(monkeypatch, "codex",
             Detection(True, "Codex CLI のログイン（~/.codex/auth.json）"),
             ProviderUsage("codex", "Codex Pro", [win], None, NOW))
    _install(monkeypatch, "claude",
             Detection(False, None, "認証情報なし"), None)
    _install(monkeypatch, "glm",
             Detection(False, None, "API キーが未設定"), None)

    entries = {e["provider"]: e for e in providers.detect_all(NOW)}

    codex = entries["codex"]
    assert codex["name"] == "Codex" and codex["detected"] is True
    assert codex["source"] == "Codex CLI のログイン（~/.codex/auth.json）"
    assert codex["needs_key"] is False and codex["error"] is None
    assert codex["plan"] == "Pro"
    assert codex["windows"] == [{"key": "weekly", "label": "週間"}]

    claude = entries["claude"]
    assert claude["detected"] is False and claude["source"] is None
    assert claude["error"] == "認証情報なし"

    glm = entries["glm"]
    assert glm["name"] == "GLM (Z.ai)"
    assert glm["needs_key"] is True and glm["plan"] is None
    assert glm["windows"] == [] and glm["error"] == "API キーが未設定"


def test_detect_all_skips_part_of_windows(monkeypatch):
    """内訳窓 (part_of 付き) は detect_all の windows (窓ピッカー・窓チェック
    の一覧) に出さない。"""
    _install(monkeypatch, "claude", Detection(False, None, "テスト"), None)
    _install(monkeypatch, "codex", Detection(False, None, "テスト"), None)
    _install(monkeypatch, "glm", Detection(False, None, "テスト"), None)
    main = Window("grok", "weekly", "週間", 22.0, NOW + timedelta(days=5),
                  timedelta(days=7))
    build = Window("grok", "weekly_build", "週間 Build", 20.0, main.resets_at,
                   main.length, part_of="weekly")
    _install(monkeypatch, "grok", Detection(True, "Grok CLI のログイン"),
             ProviderUsage("grok", "Grok", [main, build], None, NOW))

    entries = {e["provider"]: e for e in providers.detect_all(NOW)}

    assert entries["grok"]["detected"] is True
    assert entries["grok"]["windows"] == [{"key": "weekly", "label": "週間"}]


def test_detect_all_never_raises(monkeypatch):
    def detect():
        raise RuntimeError("boom")

    _install(monkeypatch, "codex", Detection(False, None, "テスト"), None)
    _install(monkeypatch, "glm", Detection(False, None, "テスト"), None)
    monkeypatch.setitem(providers.REGISTRY, "claude",
                        replace(providers.REGISTRY["claude"], detect=detect))
    entries = providers.detect_all(NOW)
    assert entries[0]["provider"] == "claude"
    assert entries[0]["error"] == "RuntimeError"


def test_detect_all_fetch_error_is_reported(monkeypatch):
    def fetch(now):
        raise urllib.error.HTTPError(providers.CODEX_USAGE_URL, 401, "Unauthorized", None, None)

    _install(monkeypatch, "claude", Detection(False, None, "テスト"), None)
    _install(monkeypatch, "glm", Detection(False, None, "テスト"), None)
    _install(monkeypatch, "codex", Detection(True, "s"), None)
    monkeypatch.setitem(providers.REGISTRY, "codex",
                        replace(providers.REGISTRY["codex"], fetch=fetch))
    entries = {e["provider"]: e for e in providers.detect_all(NOW)}
    assert entries["codex"]["detected"] is True
    assert entries["codex"]["plan"] is None and entries["codex"]["windows"] == []
    assert entries["codex"]["error"] == "HTTP 401"


def test_fetch_all_picks_up_registry_additions(monkeypatch):
    """新しいモジュール (例 grok.py) を REGISTRY に足すだけで fetch_all に乗る。"""
    for pid in ("claude", "codex", "glm", "gemini"):
        _install(monkeypatch, pid, Detection(False, None, "テスト"), None)

    def fetch(now):
        return ProviderUsage("grok", "Grok", [], None, now)

    dummy = Provider("grok", "Grok", False, lambda: Detection(True, "テスト"), fetch)
    monkeypatch.setitem(providers.REGISTRY, "grok", dummy)
    results = providers.fetch_all(NOW)
    assert [r.provider for r in results] == ["claude", "codex", "glm", "grok", "gemini", "openrouter", "deepseek", "kimi"]
    assert next(r for r in results if r.provider == "grok").name == "Grok"


# ------------------------------------------------------- GLM キーの探索順

def test_glm_key_order_keychain_first(monkeypatch):
    monkeypatch.setattr(providers.glm.keychain, "read", lambda account: "from-keychain")
    key, source = providers.glm.locate_glm_key()
    assert key == "from-keychain"
    assert "キーチェーン" in source


def test_glm_key_order_env(monkeypatch):
    monkeypatch.setattr(providers.glm.keychain, "read", lambda account: None)
    monkeypatch.setenv("ZAI_API_KEY", "from-env")
    key, source = providers.glm.locate_glm_key()
    assert key == "from-env" and source == "環境変数 ZAI_API_KEY"

    # 環境変数が空白だけなら「無い」扱い
    monkeypatch.setenv("ZAI_API_KEY", " ")
    with pytest.raises(CredentialError):
        providers.glm.locate_glm_key()

    # 環境変数も無い → CredentialError
    monkeypatch.delenv("ZAI_API_KEY")
    with pytest.raises(CredentialError):
        providers.glm.locate_glm_key()


def test_glm_detect_sources(monkeypatch):
    monkeypatch.setattr(providers.glm.keychain, "read", lambda account: "k")
    det = providers.glm.detect()
    assert det.detected and "キーチェーン" in det.source

    monkeypatch.setattr(providers.glm.keychain, "read", lambda account: None)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    det = providers.glm.detect()
    assert det.detected is False and det.error == "API キーが未設定"


# ------------------------------------------------- 英語表示 (i18n / en)

def test_window_labels_english():
    """en のとき窓ラベルは英語 (Weekly / 5h / 2d / 3h) になる。"""
    from quotacanvas import i18n

    i18n.set_language("en")
    try:
        assert providers._window_identity(604800) == ("weekly", "Weekly", timedelta(days=7))
        assert providers._window_identity(18000) == ("5h", "5h", timedelta(hours=5))
        assert providers._window_identity(2 * 86400) == ("2d", "2d", timedelta(days=2))
        assert providers._window_identity(3 * 3600) == ("3h", "3h", timedelta(hours=3))

        _, windows = parse_claude(CLAUDE_PAYLOAD, NOW, "max")
        by_key = {w.key: w for w in windows}
        assert by_key["5h"].label == "5h"
        assert by_key["weekly"].label == "Weekly"
        assert by_key["weekly_opus"].label == "Weekly Opus"

        _, codex_windows = parse_codex(CODEX_PAYLOAD, NOW)
        assert [w.label for w in codex_windows] == ["5h", "Weekly"]
    finally:
        i18n.set_language("ja")


def test_credential_errors_english(monkeypatch, tmp_path):
    """en のとき認証情報まわりのエラー文・取得元説明が英語になる。"""
    from quotacanvas import i18n

    monkeypatch.setenv("HOME", str(tmp_path))  # どこにも認証情報がない状態
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setattr(providers.glm.keychain, "read", lambda account: None)
    i18n.set_language("en")
    try:
        with pytest.raises(CredentialError) as codex_exc:
            providers.codex.read_codex_credentials()
        assert str(codex_exc.value) == "No credentials"

        with pytest.raises(CredentialError) as glm_exc:
            providers.glm.read_glm_api_key()
        assert str(glm_exc.value) == "API key not set"

        with pytest.raises(CredentialError) as grok_exc:
            providers.grok.read_grok_credentials()
        assert str(grok_exc.value) == "No credentials"

        # 取得元の説明 (detect の source) も英語
        monkeypatch.setattr(providers.glm.keychain, "read", lambda account: "k")
        det = providers.glm.detect()
        assert det.detected
        assert det.source == "GLM (Z.ai) API key (keychain quota-canvas/glm)"

        grok_det = providers.grok.detect()
        assert grok_det.detected is False
        assert grok_det.error == "No credentials"
    finally:
        i18n.set_language("ja")


def test_claude_token_expired_error_english(monkeypatch):
    """en のとき 401 (CLI 更新なし) の案内文が英語になる。"""
    from quotacanvas import i18n

    monkeypatch.setattr(providers.claude, "read_claude_credentials", lambda: ("tok", "max"))
    monkeypatch.setattr(providers.claude, "_refresh_mode", None)  # run 以外 = CLI を起動しない

    def unauthorized(url, headers, timeout=providers.TIMEOUT):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(providers.claude, "http_get_json", unauthorized)
    i18n.set_language("en")
    try:
        with pytest.raises(CredentialError) as excinfo:
            providers.claude.fetch(NOW)
        assert str(excinfo.value) == "Token expired (run claude once)"
    finally:
        i18n.set_language("ja")


def test_detect_all_fallback_error_english(monkeypatch):
    """en のとき detect_all の既定エラー (認証情報なし) も英語になる。"""
    from quotacanvas import i18n

    _install(monkeypatch, "codex", Detection(False, None, None), None)
    _install(monkeypatch, "glm", Detection(False, None, None), None)
    _install(monkeypatch, "claude", Detection(False, None, None), None)
    # gemini は実機の agy を見に行くので、既定エラーの確認では差し替える
    _install(monkeypatch, "gemini", Detection(False, None, None), None)
    for pid in ("openrouter", "deepseek", "kimi"):
        _install(monkeypatch, pid, Detection(False, None, None), None)
    i18n.set_language("en")
    try:
        entries = providers.detect_all(NOW)
    finally:
        i18n.set_language("ja")
    assert all(e["error"] == "No credentials" for e in entries)
