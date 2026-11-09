"""Poller の単体テスト: last-good 永続化と HTTP 429 バックオフ。

providers.fetch_all をモックし、実際の通信は行わない。last-good.json と
history は tmp_path に置く。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import providers
from quotacanvas.cli import Poller
from quotacanvas.history import History
from quotacanvas.lastgood import LastGood
from quotacanvas.models import LocalSlots, ProviderUsage, ResetCredit, Window

NOW = datetime(2026, 10, 25, 9, 0, 0, tzinfo=timezone.utc)
LAST_GOOD = "last-good.json"


def _win(provider, key, used=10.0):
    return Window(provider, key, key, used, NOW + timedelta(days=1), timedelta(days=7))


def _usage(pid, windows=None, error=None, fetched_at=NOW, retry_after=None, name=None):
    return ProviderUsage(pid, name or pid.title(), list(windows or []), error,
                         fetched_at, [], retry_after)


class FakeFetch:
    """providers.fetch_all を差し替える。behavior(pid, now) -> ProviderUsage。

    呼び出し (pid, now, enabled) を記録する。enabled=None は REGISTRY 全体。
    """

    def __init__(self, monkeypatch, behavior):
        self.calls = []
        self.behavior = behavior

        def fake(now=None, enabled=None):
            pids = list(providers.REGISTRY) if enabled is None \
                else [p for p in providers.REGISTRY if p in enabled]
            passed = None if enabled is None else set(enabled)
            for pid in pids:
                self.calls.append((pid, now, passed))
            return [self.behavior(pid, now) for pid in pids]

        monkeypatch.setattr(providers, "fetch_all", fake)

    def times_called(self, pid):
        return sum(1 for c in self.calls if c[0] == pid)


def _poller(tmp_path, **kwargs):
    kwargs.setdefault("enabled", {"claude", "codex"})
    return Poller(History(tmp_path / "history.jsonl"),
                  last_good_path=tmp_path / LAST_GOOD, **kwargs)


def _other(pid, now):
    return _usage(pid, [_win(pid, "weekly")], fetched_at=now)


# ------------------------------------------------------- (A) last-good 永続化

def test_success_saved_and_used_after_restart(tmp_path, monkeypatch):
    good = _usage("claude", [_win("claude", "weekly", 30.0)], name="Claude Max")
    state = {"ok": True}

    def behavior(pid, now):
        return good if state["ok"] else _usage(pid, error="HTTP 500", fetched_at=now)

    fake = FakeFetch(monkeypatch, behavior)
    poller = _poller(tmp_path)
    assert poller.fetch(NOW)[0].error is None
    assert (tmp_path / LAST_GOOD).exists()          # 成功のたびに保存

    restarted = _poller(tmp_path)                   # 新しい Poller で読み込み
    assert restarted.last_good["claude"].name == "Claude Max"
    assert restarted.last_good["claude"].windows == good.windows

    state["ok"] = False
    fake.calls.clear()
    later = NOW + timedelta(minutes=30)
    result = restarted.fetch(later)
    assert fake.times_called("claude") == 1
    assert result[0].error is None                  # 取得失敗時は last_good を返す
    assert result[0].windows == good.windows
    assert result[0].stale_error == "HTTP 500"      # 失敗中であることは stale_error で


@pytest.mark.parametrize("enabled", [{"claude", "codex"}, {"claude"}],
                         ids=["default", "claude-only"])
def test_old_last_good_is_still_used_during_failures(tmp_path, monkeypatch, enabled):
    # 経過時間に関係なく (1 時間以上前でも) 失敗中は last_good を表示し続ける。
    # enabled は既定 (claude+codex) と絞り込み (claude のみ) の両経路で確かめる。
    old = _usage("claude", [_win("claude", "weekly")],
                 fetched_at=NOW - timedelta(hours=5))
    LastGood(tmp_path / LAST_GOOD).set(old)
    on_disk_before = (tmp_path / LAST_GOOD).read_bytes()
    FakeFetch(monkeypatch, lambda pid, now: _usage(pid, error="HTTP 500", fetched_at=now))
    poller = _poller(tmp_path, enabled=enabled)
    result = poller.fetch(NOW)
    assert [u.provider for u in result] == [p for p in providers.REGISTRY
                                            if p in enabled]   # 全件返す (順序も)
    shown = next(u for u in result if u.provider == "claude")
    assert shown.error is None                  # last_good を返す
    assert shown.windows == old.windows
    assert shown.stale_error == "HTTP 500"
    assert shown.fetched_at == old.fetched_at   # 取得時刻は前回成功時のまま
    assert shown is not poller.last_good["claude"]   # 表示用は本体のコピー
    assert poller.last_good["claude"].stale_error is None   # 本体は汚さない
    # 失敗中は last-good.json も history も更新しない (stale_error を書かない)
    assert (tmp_path / LAST_GOOD).read_bytes() == on_disk_before
    on_disk = json.loads(on_disk_before)
    assert "stale_error" not in on_disk["claude"]   # キーとしても存在しない
    assert not (tmp_path / "history.jsonl").exists()
    if "codex" in enabled:
        codex_usage = next(u for u in result if u.provider == "codex")
        assert codex_usage.error == "HTTP 500"  # last_good 無しは従来どおりエラーのまま
        assert codex_usage.stale_error is None


def test_missing_last_good_file_returns_empty(tmp_path):
    assert LastGood(tmp_path / "none.json").load() == {}


def test_broken_last_good_file_still_boots(tmp_path, monkeypatch):
    (tmp_path / LAST_GOOD).write_text("{oops", encoding="utf-8")
    good = _usage("claude", [_win("claude", "weekly")], name="Claude Max")
    FakeFetch(monkeypatch, lambda pid, now: good if pid == "claude" else _other(pid, now))
    poller = _poller(tmp_path)                      # 例外を出さず起動できる
    assert poller.last_good == {}
    result = poller.fetch(NOW)
    assert result[0].name == "Claude Max"           # その後普通に保存できる
    on_disk = json.loads((tmp_path / LAST_GOOD).read_text(encoding="utf-8"))
    assert on_disk["claude"]["name"] == "Claude Max"


def test_last_good_roundtrip_windows_and_credits(tmp_path):
    reset = ResetCredit("週リセット", NOW - timedelta(days=1), NOW + timedelta(days=6))
    jst = timezone(timedelta(hours=9))
    usage = ProviderUsage(
        "codex", "Codex Pro",
        [Window("codex", "weekly", "週間", 42.5,
                datetime(2026, 10, 30, 18, 0, tzinfo=jst), timedelta(days=7)),
         Window("codex", "5h", "5時間", 1.0, None, timedelta(hours=5))],
        None, NOW - timedelta(minutes=5), [reset])
    LastGood(tmp_path / LAST_GOOD).set(usage)
    loaded = LastGood(tmp_path / LAST_GOOD).data["codex"]
    assert loaded == usage                          # Window / ResetCredit / datetime 往復

    limited = _usage("claude", error="HTTP 429", retry_after=123.5)
    LastGood(tmp_path / "other.json").set(limited)
    assert LastGood(tmp_path / "other.json").data["claude"].retry_after == 123.5


# -------------------------------------------- (A2) 履歴はメイン窓だけ

def test_fetch_records_only_main_windows(tmp_path, monkeypatch):
    """内訳窓 (part_of 付き) は history.record の対象外 (メイン窓だけ記録)。"""
    main = _win("grok", "weekly", 22.0)
    build = Window("grok", "weekly_build", "週間 Build", 20.0, main.resets_at,
                   main.length, part_of="weekly")
    monkeypatch.setattr(providers, "fetch_all",
                        lambda now=None, enabled=None:
                        [_usage("grok", [main, build], name="Grok")])
    poller = Poller(History(tmp_path / "history.jsonl"), enabled={"grok"},
                    last_good_path=tmp_path / LAST_GOOD)
    poller.fetch(NOW)
    assert [s.window_id for s in poller.history.samples("grok:weekly")] != []
    assert poller.history.samples("grok:weekly_build") == []
    # last-good.json には内訳も残る (失敗中の描画で塗り分けに使う)
    assert [w.key for w in poller.last_good["grok"].windows] == \
        ["weekly", "weekly_build"]


# ------------------------------------------------------ (B) 429 バックオフ

def test_429_backoff_skips_fetch_then_retries(tmp_path, monkeypatch):
    def behavior(pid, now):
        if pid == "claude":
            return _usage(pid, error="HTTP 429", fetched_at=now)
        return _other(pid, now)

    fake = FakeFetch(monkeypatch, behavior)
    poller = _poller(tmp_path, enabled=None)        # enabled=None (全部) でも動く
    result = poller.fetch(NOW)
    assert [u.provider for u in result] == list(providers.REGISTRY)  # 順序は従来どおり
    assert result[0].error == "HTTP 429"
    assert fake.times_called("claude") == 1

    fake.calls.clear()
    result = poller.fetch(NOW + timedelta(minutes=1))
    assert fake.times_called("claude") == 0         # バックオフ中は取りに行かない
    assert all("claude" not in passed for _, _, passed in fake.calls)  # enabled から外れる
    assert [u.provider for u in result] == list(providers.REGISTRY)
    assert result[0].error == "HTTP 429"            # last_good 無し -> 直近の 429 エラー

    fake.calls.clear()
    poller.fetch(NOW + timedelta(minutes=6))        # 5 分を過ぎたら再取得
    assert fake.times_called("claude") == 1


def test_retry_after_is_respected(tmp_path, monkeypatch):
    fake = FakeFetch(monkeypatch, lambda pid, now:
                     _usage(pid, error="HTTP 429", fetched_at=now, retry_after=1800.0)
                     if pid == "claude" else _other(pid, now))
    poller = _poller(tmp_path, enabled={"claude"})
    poller.fetch(NOW)
    assert fake.times_called("claude") == 1
    poller.fetch(NOW + timedelta(minutes=10))
    poller.fetch(NOW + timedelta(minutes=29))
    assert fake.times_called("claude") == 1         # 30 分未満なので呼ばれない
    poller.fetch(NOW + timedelta(minutes=31))
    assert fake.times_called("claude") == 2         # 指定時刻を過ぎたら再取得


def test_retry_after_is_clamped_to_2min_min(tmp_path, monkeypatch):
    fake = FakeFetch(monkeypatch, lambda pid, now:
                     _usage(pid, error="HTTP 429", fetched_at=now, retry_after=10.0)
                     if pid == "claude" else _other(pid, now))
    poller = _poller(tmp_path, enabled={"claude"})
    poller.fetch(NOW)
    poller.fetch(NOW + timedelta(minutes=1))
    assert fake.times_called("claude") == 1         # 短くても最低 2 分は待つ
    poller.fetch(NOW + timedelta(minutes=2, seconds=1))
    assert fake.times_called("claude") == 2


def test_retry_after_is_capped_at_1h(tmp_path, monkeypatch):
    fake = FakeFetch(monkeypatch, lambda pid, now:
                     _usage(pid, error="HTTP 429", fetched_at=now, retry_after=99999.0)
                     if pid == "claude" else _other(pid, now))
    poller = _poller(tmp_path, enabled={"claude"})
    poller.fetch(NOW)
    poller.fetch(NOW + timedelta(minutes=59))
    assert fake.times_called("claude") == 1         # 長くても最大 1 時間
    poller.fetch(NOW + timedelta(minutes=60, seconds=1))
    assert fake.times_called("claude") == 2


def test_backoff_doubles_and_caps_without_retry_after(tmp_path, monkeypatch):
    FakeFetch(monkeypatch, lambda pid, now:
              _usage(pid, error="HTTP 429", fetched_at=now)
              if pid == "claude" else _other(pid, now))
    poller = _poller(tmp_path, enabled={"claude"})
    t = NOW
    delays = []
    for _ in range(6):
        poller.fetch(t)
        nxt = poller._next_attempt["claude"]
        delays.append((nxt - t).total_seconds() / 60)
        t = nxt + timedelta(seconds=1)
    assert delays == [5, 10, 20, 40, 60, 60]        # 倍々で 60 分上限


def test_success_clears_backoff(tmp_path, monkeypatch):
    state = {"n": 0}

    def behavior(pid, now):
        if pid != "claude":
            return _other(pid, now)
        state["n"] += 1
        if state["n"] == 1:
            return _usage(pid, error="HTTP 429", fetched_at=now)
        if state["n"] == 2:
            return _other(pid, now)                  # 成功で解除
        return _usage(pid, error="HTTP 429", fetched_at=now)

    fake = FakeFetch(monkeypatch, behavior)
    poller = _poller(tmp_path, enabled={"claude"})
    poller.fetch(NOW)                                # 429 -> 5 分
    assert poller.fetch(NOW + timedelta(minutes=1))[0].error == "HTTP 429"
    assert fake.times_called("claude") == 1
    poller.fetch(NOW + timedelta(minutes=6))         # 成功
    assert poller.last_good["claude"].error is None
    poller.fetch(NOW + timedelta(minutes=7))         # すぐまた呼ばれる
    assert fake.times_called("claude") == 3
    nxt = poller._next_attempt["claude"]             # 倍々の回数もリセット済み
    assert (nxt - (NOW + timedelta(minutes=7))).total_seconds() == 300


def test_non_429_failure_resets_backoff_count(tmp_path, monkeypatch):
    state = {"n": 0}

    def behavior(pid, now):
        if pid != "claude":
            return _other(pid, now)
        state["n"] += 1
        if state["n"] == 1:
            return _usage(pid, error="HTTP 429", fetched_at=now)
        if state["n"] == 2:
            return _usage(pid, error="HTTP 503", fetched_at=now)  # 429 以外はリセット
        return _usage(pid, error="HTTP 429", fetched_at=now)

    fake = FakeFetch(monkeypatch, behavior)
    poller = _poller(tmp_path, enabled={"claude"})
    poller.fetch(NOW)                                # 429, count=1
    poller.fetch(NOW + timedelta(minutes=6))         # 503 (回数リセット)
    poller.fetch(NOW + timedelta(minutes=7))         # 429 -> また 5 分から
    nxt = poller._next_attempt["claude"]
    assert (nxt - (NOW + timedelta(minutes=7))).total_seconds() == 300


def test_429_warns_once_not_every_poll(tmp_path, monkeypatch, caplog):
    FakeFetch(monkeypatch, lambda pid, now:
              _usage(pid, error="HTTP 429", fetched_at=now)
              if pid == "claude" else _other(pid, now))
    poller = _poller(tmp_path, enabled={"claude"})
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        poller.fetch(NOW)
        poller.fetch(NOW + timedelta(minutes=1))     # バックオフ中のスキップは debug
        poller.fetch(NOW + timedelta(minutes=2))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "claude" in warnings[0].getMessage() and "429" in warnings[0].getMessage()


def test_backoff_substitute_keeps_last_success_fetch_time(tmp_path, monkeypatch):
    # 429 バックオフ中の代役 (last_good) も、取得時刻は前回成功時のまま
    state = {"ok": True}

    def behavior(pid, now):
        if state["ok"]:
            return _usage(pid, [_win(pid, "weekly")], fetched_at=now)
        return _usage(pid, error="HTTP 429", fetched_at=now)

    FakeFetch(monkeypatch, behavior)
    poller = _poller(tmp_path, enabled={"claude"})
    poller.fetch(NOW)
    state["ok"] = False
    later = NOW + timedelta(minutes=1)
    assert poller.fetch(later)[0].fetched_at == NOW          # 429 を受けた周回
    assert poller.fetch(later + timedelta(minutes=1))[0].fetched_at == NOW  # バックオフ中


# -------------------------------------------- (C) ローカル枠だけの取り直し

def test_fetch_local_without_local_returns_empty(tmp_path, monkeypatch):
    """[providers.local] が無ければ providers.local.fetch_all を呼ばない。"""
    calls = []
    monkeypatch.setattr(providers.local, "fetch_all",
                        lambda entries, now: calls.append((entries, now)) or [])
    poller = _poller(tmp_path)                      # local 未設定 (空)
    assert poller.fetch_local(NOW) == []
    assert calls == []


def test_fetch_local_delegates_to_providers_local(tmp_path, monkeypatch):
    """local があれば providers.local.fetch_all(self.local, now) に委譲する。"""
    entry = object()
    usage = ProviderUsage("local:srv", "Local", [], None, NOW,
                          slots=LocalSlots(running=2, waiting=1, capacity=4))
    calls = []

    def fake(entries, now):
        calls.append((entries, now))
        return [usage]

    monkeypatch.setattr(providers.local, "fetch_all", fake)
    poller = _poller(tmp_path, local={"srv": entry})
    assert poller.fetch_local(NOW) == [usage]
    assert calls == [({"srv": entry}, NOW)]
    assert poller.local == {"srv": entry}           # 設定は書き換えない


def test_fetch_includes_local_after_registry(tmp_path, monkeypatch):
    """全体取得でもローカル枠は従来どおり末尾に並ぶ (fetch_local 経由)。"""
    FakeFetch(monkeypatch, lambda pid, now: _other(pid, now))
    usage = ProviderUsage("local:srv", "Local", [], None, NOW,
                          slots=LocalSlots(running=0, waiting=0, capacity=4))
    monkeypatch.setattr(providers.local, "fetch_all", lambda entries, now: [usage])
    poller = _poller(tmp_path, enabled={"claude"}, local={"srv": object()})
    result = poller.fetch(NOW)
    assert [u.provider for u in result] == ["claude", "local:srv"]


def test_last_good_invalid_utf8_is_treated_as_corrupt(tmp_path, caplog):
    path = tmp_path / LAST_GOOD
    path.write_bytes(b'\xff')
    cache = LastGood(path)
    assert cache.data == {}
    assert "壊れている" in caplog.text
    cache.set(_usage("claude", [_win("claude", "weekly")], fetched_at=NOW))
    assert "claude" in LastGood(path).data


# ------------------------------------------------- リセット時刻を過ぎた窓の繰り越し

def test_window_past_reset_is_rolled_over(tmp_path, monkeypatch):
    # API がリセット後も古い期間 (100% 使用・過去のリセット時刻) を返し続けるケース
    old = Window("codex", "weekly", "weekly", 100.0, NOW - timedelta(minutes=14),
                 timedelta(days=7))
    FakeFetch(monkeypatch, lambda pid, now: _usage(pid, [old], fetched_at=now)
              if pid == "codex" else _other(pid, now))
    poller = _poller(tmp_path)
    codex = next(u for u in poller.fetch(NOW) if u.provider == "codex")
    w = codex.windows[0]
    assert w.used_percent == 0.0
    assert w.resets_at == old.resets_at + timedelta(days=7)
    # 保存する last-good は API の値のまま
    assert LastGood(tmp_path / LAST_GOOD).data["codex"].windows[0].used_percent == 100.0


def test_last_good_past_reset_is_rolled_over(tmp_path, monkeypatch):
    old = Window("codex", "5h", "5h", 100.0, NOW - timedelta(hours=11), timedelta(hours=5))
    state = {"ok": True}
    FakeFetch(monkeypatch, lambda pid, now: (_usage(pid, [old], fetched_at=now) if state["ok"]
              else _usage(pid, error="HTTP 500", fetched_at=now))
              if pid == "codex" else _other(pid, now))
    poller = _poller(tmp_path)
    poller.fetch(NOW - timedelta(hours=12))
    state["ok"] = False
    codex = next(u for u in poller.fetch(NOW) if u.provider == "codex")
    w = codex.windows[0]
    assert codex.stale_error == "HTTP 500"
    assert w.used_percent == 0.0
    # 11 時間前のリセット + 5h * 3 = 4 時間後
    assert w.resets_at == NOW + timedelta(hours=4)


def test_future_reset_window_is_unchanged(tmp_path, monkeypatch):
    # リセット時刻が未来・不明の窓は繰り越さない (条件を逆にして未過ぎの窓まで
    # 0% にする過剰適用を used_percent の実値で検出する)。内訳窓 (part_of) や
    # リセット権もそのまま。no-op なら同一オブジェクトのまま。
    future = Window("claude", "weekly", "weekly", 10.0,
                    NOW + timedelta(days=1), timedelta(days=7))
    build = Window("claude", "weekly_build", "週間 Build", 20.0,
                   future.resets_at, timedelta(days=7), part_of="weekly")
    unknown = Window("claude", "5h", "5h", 30.0, None, timedelta(hours=5))
    credit = ResetCredit("週リセット", NOW - timedelta(days=1), NOW + timedelta(days=6))
    # 期待値は fetch 前に値のコピーとして確保する (処理後に窓やリセット権の
    # フィールドを直接書き換える退行でも期待値が一緒に変わらないように)
    expected_windows = [replace(w) for w in (future, build, unknown)]
    expected_credit = replace(credit)
    built = {}

    def behavior(pid, now):
        usage = (replace(_usage("claude", [future, build, unknown], fetched_at=now),
                         reset_credits=[credit])
                 if pid == "claude" else _other(pid, now))
        built[pid] = usage
        return usage

    FakeFetch(monkeypatch, behavior)
    poller = _poller(tmp_path)
    result = poller.fetch(NOW)
    # fetch が [] を返しても通らないよう、件数と provider 集合 (REGISTRY 順) を見る
    assert [u.provider for u in result] == [p for p in providers.REGISTRY
                                            if p in {"claude", "codex"}]
    claude = result[0]
    assert claude is built["claude"]                # roll 不要なら同一オブジェクト
    assert claude.windows == expected_windows       # 全フィールドが期待値のコピーと一致
    assert [w.used_percent for w in claude.windows] == [10.0, 20.0, 30.0]
    assert [w.resets_at for w in claude.windows] == [NOW + timedelta(days=1),
                                                     NOW + timedelta(days=1), None]
    assert [w.part_of for w in claude.windows] == [None, "weekly", None]  # 内訳は保持
    assert all(not w.reset_estimated for w in claude.windows)
    assert claude.reset_credits == [expected_credit]   # リセット権への影響もなし
    codex = result[1]
    assert codex.windows[0].used_percent == 10.0
    assert codex.windows[0].resets_at == NOW + timedelta(days=1)
    assert not codex.windows[0].reset_estimated


def test_zero_length_window_past_reset_is_reset_with_unknown_next():
    w = Window("x", "k", "k", 100.0, NOW - timedelta(minutes=1), timedelta(0))
    rolled = w.rolled_over(NOW)
    assert (rolled.used_percent, rolled.resets_at, rolled.reset_estimated) == (0.0, None, True)
    assert w.rolled_over(NOW - timedelta(minutes=2)) is w
