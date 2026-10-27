"""Gemini (Antigravity CLI `agy` の /usage) プロバイダのテスト。

agy の場所 (_find_agy) と subprocess (Popen / communicate) はすべてモックし、
実機の agy は絶対に実行しない (conftest の autouse も _find_agy と _read_entry
を塞いでいる。このモジュールの fake_agy フィクスチャがさらに上書きする)。
キーチェーンを読むテストは本物の _read_entry に戻して FakeKeychain を使う。
トークンの値はテストに書かない (ダミー文字列だけ)。
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
import signal
import subprocess
import tempfile
import types
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import tomllib

from quotacanvas import providers
from quotacanvas.providers import gemini

# conftest の autouse が _read_entry を塞ぐので、キーチェーンを使うテストでは
# このモジュールで本物に戻す
REAL_READ_ENTRY = gemini._read_entry

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
FRESH_ISO = "2026-10-25T21:15:00+09:00"  # NOW + 75分 (JST 21:15 = UTC 12:15)

RESET_WEEKLY = datetime(2026, 11, 1, 8, 47, 7, tzinfo=timezone.utc)
RESET_ARGON = datetime(2026, 11, 1, 8, 44, 4, tzinfo=timezone.utc)
RESET_5H = datetime(2026, 10, 25, 13, 47, 7, tzinfo=timezone.utc)
RESET_3P_WEEKLY = datetime(2026, 11, 1, 9, 31, 15, tzinfo=timezone.utc)
RESET_3P_5H = datetime(2026, 10, 25, 14, 31, 15, tzinfo=timezone.utc)

# agy /usage の command.data (2026-10-25 の実応答と同じ形・数値はダミー)。
# バケットはグループ名でなく id で識別するので、グループ名・並びは壊れていてもよい。
USAGE_GROUPS = [
    {"name": "Gemini Models", "description": "...", "buckets": [
        {"id": "gemini-weekly", "name": "Weekly Limit Remaining",
         "window": "weekly", "remaining_fraction": 0.99,
         "reset_time": "2026-11-01T08:47:07Z"},
        {"id": "gemini-5h", "name": "Five Hour Limit Remaining",
         "window": "5h", "remaining_fraction": 0.9772,
         "reset_time": "2026-10-25T13:47:07Z"},
    ]},
    {"name": "Claude and GPT models", "buckets": [
        {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.97977,
         "reset_time": "2026-11-01T09:31:15Z"},
        {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.95955,
         "reset_time": "2026-10-25T14:31:15Z"},
    ]},
    {"name": "Gemini 4 Argon", "buckets": [
        {"id": "gemini-argon-wtu-weekly", "name": "Gemini 4 Argon Weekly Limit",
         "window": "weekly", "remaining_fraction": 0.98351,
         "reset_time": "2026-11-01T08:44:04Z"},
    ]},
]

# v1internal:loadCodeAssist (プラン名だけに使う)
ASSIST = {"paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"},
          "currentTier": {"id": "g1-pro-tier", "name": "Google AI Pro"},
          "cloudaicompanionProject": "aicode-consumers"}


def _agy_stdout(groups=None, status="SUCCESS", drop_data=False):
    """agy の stdout 1 回ぶん (JSON 1 個)。"""
    data = {"description": "...", "groups": USAGE_GROUPS if groups is None else groups}
    body = {"conversation_id": "", "status": status, "response": "tsv...",
            "command": {"name": "usage"}}
    if not drop_data:
        body["command"]["data"] = data
    return json.dumps(body).encode("utf-8")


# ------------------------------------------------------------------ fakes

class PopenError(Exception):
    """FakeAgy 用: Popen 自体の失敗 (OSError 相当) を表す。"""

    def __init__(self, exc):
        super().__init__(str(exc))
        self.exc = exc


class FakeProc:
    """Popen の戻り値の置き換え。outcome: communicate の出力 (bytes) か例外。"""

    def __init__(self, outcome):
        self.pid = 42424
        self.outcome = outcome
        self.comm_timeouts = []
        self.waits = []

    def communicate(self, timeout=None):
        self.comm_timeouts.append(timeout)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return (self.outcome or b"", b"")

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return 0

    def poll(self):
        return 0


class FakeAgy:
    """gemini.subprocess の置き換え。outcomes: communicate の出力 (bytes) か
    例外 / PopenError (起動失敗)。足りなければ最後の outcome を使い回す。"""

    def __init__(self, monkeypatch, outcomes=None):
        self.outcomes = list(outcomes) if outcomes is not None else [_agy_stdout()]
        self.calls = []  # (argv, kwargs)
        self.procs = []
        monkeypatch.setattr(gemini, "subprocess", types.SimpleNamespace(
            Popen=self._popen, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired))

    def _popen(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, PopenError):
            raise outcome.exc
        proc = FakeProc(outcome)
        self.procs.append(proc)
        return proc


@pytest.fixture()
def fake_agy(monkeypatch):
    """実機の agy を実行しない: 場所は偽物、実行は FakeAgy (既定は成功応答)。"""
    monkeypatch.setattr(gemini, "_find_agy", lambda path=None: "/fake/bin/agy")
    return FakeAgy(monkeypatch)


@pytest.fixture()
def real_keychain(monkeypatch):
    """conftest の guard を上書きして本物の _read_entry を使う
    (実際の I/O は FakeKeychain が吸収する)。"""
    monkeypatch.setattr(gemini, "_read_entry", REAL_READ_ENTRY)


class FakeKeychain:
    """keychain.read の置き換え。values: 生の値か例外。足りなければ最後を使い回す。"""

    def __init__(self, monkeypatch, values):
        self.values = list(values)
        self.calls = 0
        monkeypatch.setattr(gemini.keychain, "read", self)

    def __call__(self, account, service=None):
        assert (account, service) == (gemini.KEYCHAIN_ACCOUNT, gemini.KEYCHAIN_SERVICE)
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if isinstance(value, BaseException):
            raise value
        return value


def _raw(token="tok", expiry=None):
    payload = {"token": {"access_token": token, "token_type": "Bearer",
                         "refresh_token": "r", "expiry": expiry}}
    return "go-keyring-base64:" + base64.b64encode(
        json.dumps(payload).encode("utf-8")).decode("ascii")


class FakeHttp:
    """http_post_json の置き換え。outcomes: dict (成功) か例外インスタンス。"""

    def __init__(self, monkeypatch, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []  # (url, headers, body)
        monkeypatch.setattr(gemini, "http_post_json", self)

    def __call__(self, url, headers, body, timeout=None):
        self.calls.append((url, headers, body))
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# ------------------------------------------------------------------ agy 実行

def test_usage_env_is_a_whitelist(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "secret")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/usr/original/bin")
    monkeypatch.setenv("EXTRA", "must not pass")
    env = gemini._usage_env()
    assert set(env) <= set(gemini._ENV_KEYS)  # os.environ 全部は渡さない
    assert "GEMINI_API_KEY" not in env and "GOOGLE_API_KEY" not in env
    assert env["PATH"].startswith(gemini._PATH_PREFIX)


def test_agy_usage_success(fake_agy):
    payload = gemini._agy_usage()
    assert payload["groups"] == USAGE_GROUPS
    argv, kwargs = fake_agy.calls[0]
    assert argv == ["/fake/bin/agy", "-p", "/usage", "--output-format", "json"]
    assert kwargs["cwd"] == tempfile.gettempdir()  # 無害な場所で実行
    assert kwargs["start_new_session"] is True     # 時間切れで killpg できるように
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert set(kwargs["env"]) <= set(gemini._ENV_KEYS)
    proc = fake_agy.procs[0]
    assert proc.comm_timeouts == [gemini.USAGE_TIMEOUT]  # 30 秒で見限る


def test_agy_usage_missing():
    # fake_agy を使わず、agy が見つからない状態にする (conftest の autouse と同じ)
    with pytest.raises(gemini.CredentialError) as ei:
        gemini._agy_usage()
    assert str(ei.value) == "Antigravity CLI (agy) が見つからない"


def test_agy_usage_error_status(monkeypatch, fake_agy):
    FakeAgy(monkeypatch, [_agy_stdout(status="ERROR")])
    with pytest.raises(gemini.CredentialError) as ei:
        gemini._agy_usage()
    assert str(ei.value) == "agy が利用枠を返さない"


def test_agy_usage_invalid_output(monkeypatch, fake_agy):
    for bad in (b"", b"not json", _agy_stdout(drop_data=True)):
        FakeAgy(monkeypatch, [bad])
        with pytest.raises(gemini.CredentialError) as ei:
            gemini._agy_usage()
        assert str(ei.value) == "agy の応答を読めない", bad
    # JSON でも dict でなければ (status を確認できない) 失敗扱い
    FakeAgy(monkeypatch, [b"[]"])
    with pytest.raises(gemini.CredentialError) as ei:
        gemini._agy_usage()
    assert str(ei.value) == "agy が利用枠を返さない"


def test_agy_usage_timeout_kills_process_group(monkeypatch):
    """時間切れ: プロセスグループごと SIGKILL して待ってから時間切れのエラー。"""
    FakeAgy(monkeypatch, [subprocess.TimeoutExpired(cmd="agy", timeout=30)])
    monkeypatch.setattr(gemini, "_find_agy", lambda path=None: "/fake/bin/agy")
    killed = []
    monkeypatch.setattr(gemini.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(gemini.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))
    with pytest.raises(gemini.CredentialError) as ei:
        gemini._agy_usage()
    assert str(ei.value) == "agy が時間内に応答しない"
    assert killed == [(42424 + 1, signal.SIGKILL)]


def test_agy_usage_timeout_waits_after_kill(monkeypatch):
    fake = FakeAgy(monkeypatch, [subprocess.TimeoutExpired(cmd="agy", timeout=30)])
    monkeypatch.setattr(gemini, "_find_agy", lambda path=None: "/fake/bin/agy")
    monkeypatch.setattr(gemini.os, "getpgid", lambda pid: 42424)
    monkeypatch.setattr(gemini.os, "killpg", lambda pgid, sig: None)
    with pytest.raises(gemini.CredentialError):
        gemini._agy_usage()
    assert fake.procs[0].waits == [None]  # kill 後に wait で回収する


def test_agy_usage_oserror(monkeypatch, fake_agy):
    FakeAgy(monkeypatch, [PopenError(OSError("cannot start"))])
    with pytest.raises(gemini.CredentialError) as ei:
        gemini._agy_usage()
    assert str(ei.value) == "agy が利用枠を返さない"


# ------------------------------------------------------------------ parser

def test_parse_usage_five_windows():
    windows = gemini.parse_usage({"groups": USAGE_GROUPS})
    assert [w.id for w in windows] == [
        "gemini:weekly", "gemini:argon", "gemini:5h", "gemini:claude",
        "gemini:claude_5h"]
    weekly, argon, five, claude, claude5 = windows
    # メイン窓: 他プロバイダと同じ「週間」
    assert (weekly.key, weekly.label) == ("weekly", "週間")
    assert weekly.used_percent == pytest.approx((1.0 - 0.99) * 100.0)
    assert weekly.resets_at == RESET_WEEKLY
    assert weekly.length == timedelta(days=7)
    # Argon は固定ラベル
    assert (argon.key, argon.label) == ("argon", "Argon")
    assert argon.used_percent == pytest.approx((1.0 - 0.98351) * 100.0)
    assert argon.resets_at == RESET_ARGON
    assert argon.length == timedelta(days=7)
    assert (five.key, five.label) == ("5h", "5時間")
    assert five.used_percent == pytest.approx((1.0 - 0.9772) * 100.0)
    assert five.resets_at == RESET_5H
    assert five.length == timedelta(hours=5)
    # 3p = Antigravity 経由の Claude / GPT。今までは Claude の 5h 相当だったが、
    # 意味は週間 (claude) と 5 時間 (claude_5h) の 2 窓
    assert (claude.key, claude.label) == ("claude", "Claude")
    assert claude.used_percent == pytest.approx((1.0 - 0.97977) * 100.0)
    assert claude.resets_at == RESET_3P_WEEKLY
    assert claude.length == timedelta(days=7)
    assert (claude5.key, claude5.label) == ("claude_5h", "Claude 5時間")
    assert claude5.used_percent == pytest.approx((1.0 - 0.95955) * 100.0)
    assert claude5.resets_at == RESET_3P_5H
    assert claude5.length == timedelta(hours=5)
    # どの窓も内訳ではない
    assert all(w.part_of is None for w in windows)


def test_parse_usage_identifies_by_bucket_id():
    """グループ名・バケット名・並びが変わっても id で集められる。"""
    shuffled = list(reversed(USAGE_GROUPS))
    renamed = [{"name": "Unknown group %d" % i, "buckets": g["buckets"]}
               for i, g in enumerate(shuffled)]
    windows = gemini.parse_usage({"groups": renamed})
    assert [w.id for w in windows] == [
        "gemini:weekly", "gemini:argon", "gemini:5h", "gemini:claude",
        "gemini:claude_5h"]


def test_parse_usage_ignores_unknown_and_broken_buckets():
    groups = [{"name": "G", "buckets": [
        {"id": "future-bucket", "remaining_fraction": 0.5,
         "reset_time": "2026-11-01T00:00:00Z"},        # 未知の id は無視
        {"id": "gemini-weekly", "remaining_fraction": "0.5"},  # 数値でない
        {"id": "gemini-weekly", "remaining_fraction": 0.8},    # 重複は最初の 1 つ…
        {"id": "gemini-5h"},                                   # fraction 無し
        {"remaining_fraction": 0.9},                           # id 無し
        "not-a-dict",
    ]}, "not-a-group", {}]
    windows = gemini.parse_usage({"groups": groups})
    # 数値でない最初の weekly は無視されるが、id 辞書には入ったままなので
    # あとの値には置き換わらない → weekly はこのグループからは作れない
    assert [w.id for w in windows] == []


def test_parse_usage_broken_payload_is_empty():
    for payload in ({}, {"groups": None}, {"groups": []}, None, "oops",
                    {"groups": [{"buckets": []}]}):
        assert gemini.parse_usage(payload) == [], payload


def test_parse_usage_used_percent_is_clamped():
    groups = [{"name": "G", "buckets": [
        {"id": "gemini-weekly", "remaining_fraction": 1.2,
         "reset_time": "2026-11-01T00:00:00Z"},
        {"id": "3p-weekly", "remaining_fraction": -0.2,
         "reset_time": "2026-11-01T00:00:00Z"},
    ]}]
    windows = gemini.parse_usage({"groups": groups})
    assert [w.used_percent for w in windows] == [0.0, 100.0]


def test_parse_usage_labels_english():
    from quotacanvas import i18n

    i18n.set_language("en")
    try:
        windows = gemini.parse_usage({"groups": USAGE_GROUPS})
        assert [w.label for w in windows] == [
            "Weekly", "Argon", "5h", "Claude", "Claude 5h"]
    finally:
        i18n.set_language("ja")


# -------------------------------------------------------------------- fetch

def test_fetch_success(real_keychain, fake_agy, monkeypatch):
    FakeKeychain(monkeypatch, [_raw(expiry=FRESH_ISO)])
    http = FakeHttp(monkeypatch, [ASSIST])

    usage = gemini.fetch(NOW)

    assert usage.provider == "gemini"
    assert usage.name == "Gemini Google AI Pro"
    assert usage.error is None
    assert usage.fetched_at == NOW
    assert [w.id for w in usage.windows] == [
        "gemini:weekly", "gemini:argon", "gemini:5h", "gemini:claude",
        "gemini:claude_5h"]
    # agy は 1 回だけ (モデルは呼ばない)
    assert len(fake_agy.calls) == 1
    # プラン名の HTTP は loadCodeAssist 1 回だけ (fetchAvailableModels は廃止)
    assert [url for url, _, _ in http.calls] == [gemini.LOAD_CODE_ASSIST_URL]
    url, headers, body = http.calls[0]
    assert body == {"metadata": {"ideType": "ANTIGRAVITY"}}
    assert headers["Authorization"] == "Bearer tok"
    assert headers["User-Agent"] == gemini.API_USER_AGENT


def test_fetch_plan_unavailable_still_returns_windows(fake_agy):
    """キーチェーンが読めなくても (プラン名が取れなくても) 枠は失敗にしない。"""
    usage = gemini.fetch(NOW)  # conftest の autouse が _read_entry を塞いでいる
    assert usage.name == "Gemini"
    assert usage.error is None
    assert len(usage.windows) == 5


def test_fetch_plan_http_error_falls_back(real_keychain, fake_agy, monkeypatch):
    FakeKeychain(monkeypatch, [_raw(expiry=FRESH_ISO)])
    FakeHttp(monkeypatch, [urllib.error.URLError("boom")])
    usage = gemini.fetch(NOW)
    assert usage.name == "Gemini"
    assert usage.error is None and len(usage.windows) == 5


def test_fetch_plan_expired_token_falls_back(real_keychain, fake_agy, monkeypatch):
    FakeKeychain(monkeypatch, [_raw(expiry="2026-10-25T11:00:00+09:00")])  # NOW より前
    http = FakeHttp(monkeypatch, [])
    usage = gemini.fetch(NOW)
    assert usage.name == "Gemini"
    assert http.calls == []  # 期限切れのトークンで HTTP は叩かない


def test_fetch_no_buckets_is_an_error(monkeypatch, fake_agy):
    FakeAgy(monkeypatch, [_agy_stdout(groups=[])])
    usage = gemini.fetch(NOW)
    assert usage.windows == []
    assert usage.error == "利用枠の情報を取得できない"


def test_fetch_agy_failure_propagates_reason():
    with pytest.raises(gemini.CredentialError) as ei:
        gemini.fetch(NOW)  # agy 無し (conftest の autouse)
    assert str(ei.value) == "Antigravity CLI (agy) が見つからない"


# ------------------------------------------------------- プラン名のキャッシュ

class FakeClock:
    """gemini.time の置き換え (プラン名キャッシュの TTL の進み方の確認用)。"""

    def __init__(self, monkeypatch, start=1000.0):
        self.now = start
        monkeypatch.setattr(gemini, "time",
                            types.SimpleNamespace(monotonic=lambda: self.now))

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _reset_plan_cache(monkeypatch):
    """プラン名のキャッシュをテスト間で持ち越さない。"""
    monkeypatch.setattr(gemini, "_plan_cache", None)
    monkeypatch.setattr(gemini, "_plan_fetched_at", None)


def test_plan_name_is_cached_for_6h(real_keychain, fake_agy, monkeypatch):
    """成功したプラン名は 6 時間は取り直さない (常駐ループで毎回 HTTP しない)。"""
    FakeKeychain(monkeypatch, [_raw(expiry=FRESH_ISO)])
    http = FakeHttp(monkeypatch, [ASSIST])
    clock = FakeClock(monkeypatch)

    assert gemini.fetch(NOW).name == "Gemini Google AI Pro"
    assert gemini.fetch(NOW).name == "Gemini Google AI Pro"  # キャッシュで HTTP なし
    assert len(http.calls) == 1

    clock.advance(gemini._PLAN_TTL + 1.0)
    assert gemini.fetch(NOW).name == "Gemini Google AI Pro"  # 6 時間経過で取り直し
    assert len(http.calls) == 2


def test_plan_name_failure_is_not_cached(real_keychain, fake_agy, monkeypatch):
    """失敗はキャッシュしない: 次の取得で再試行する。"""
    FakeKeychain(monkeypatch, [_raw(expiry=FRESH_ISO)])
    http = FakeHttp(monkeypatch, [urllib.error.URLError("boom"), ASSIST])
    FakeClock(monkeypatch)

    assert gemini.fetch(NOW).name == "Gemini"
    assert gemini.fetch(NOW).name == "Gemini Google AI Pro"
    assert len(http.calls) == 2


# ------------------------------------------------------------------- detect

def test_detect_success(fake_agy):
    det = gemini.detect()
    assert det.detected is True
    assert det.source == "Antigravity CLI (agy) の /usage"
    assert fake_agy.calls == []  # /usage は実行しない (利用枠の取得は fetch の 1 回)


def test_detect_missing_agy():
    det = gemini.detect()  # agy 無し (conftest の autouse)
    assert det.detected is False
    assert det.error == "Antigravity CLI (agy) が見つからない"


def test_detect_all_reports_gemini(real_keychain, fake_agy, monkeypatch):
    """REGISTRY に加わっていることの確認: detect --json に plan / windows が出る。"""
    for pid in ("claude", "codex", "glm", "grok"):
        def detect_off(pid=pid):
            return providers.Detection(False, None, "テスト")
        monkeypatch.setitem(providers.REGISTRY, pid,
                            replace(providers.REGISTRY[pid],
                                              detect=detect_off))
    FakeKeychain(monkeypatch, [_raw(expiry=FRESH_ISO)])
    FakeHttp(monkeypatch, [ASSIST])

    entries = {e["provider"]: e for e in providers.detect_all(NOW)}
    entry = entries["gemini"]
    assert entry["detected"] is True
    assert entry["name"] == "Gemini (Antigravity)"
    assert entry["plan"] == "Google AI Pro"
    assert entry["windows"] == [
        {"key": "weekly", "label": "週間"},
        {"key": "argon", "label": "Argon"},
        {"key": "5h", "label": "5時間"},
        {"key": "claude", "label": "Claude"},
        {"key": "claude_5h", "label": "Claude 5時間"},
    ]
    assert entry["needs_key"] is False


# ------------------------------------------------- 互換: token_refresh 設定

def test_set_token_refresh_is_accepted_but_unused():
    """providers.gemini.token_refresh は過去の設定ファイルとの互換で受けるだけ。"""
    gemini.set_token_refresh("cli")
    gemini.set_token_refresh("off")
    with pytest.raises(ValueError):
        gemini.set_token_refresh("auto")


# ------------------------------------------------------- config / 例ファイル

def test_config_gemini_defaults(tmp_path):
    from quotacanvas.config import config_to_dict, format_toml, load_config

    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.providers.gemini is True
    assert cfg.providers.gemini_token_refresh == "cli"
    parsed = tomllib.loads(format_toml(cfg))
    assert parsed["providers"]["gemini"]["enabled"] is True
    assert parsed["providers"]["gemini"]["token_refresh"] == "cli"
    assert config_to_dict(cfg)["providers"]["gemini"]["token_refresh"] == "cli"


def test_config_example_and_brand_color():
    root = Path(__file__).resolve().parents[1]
    example = tomllib.loads((root / "config.example.toml").read_text(encoding="utf-8"))
    assert example["providers"]["gemini"]["enabled"] is True

    from quotacanvas.render import BRAND_COLORS
    assert BRAND_COLORS["gemini"] == "#8E75F0"
