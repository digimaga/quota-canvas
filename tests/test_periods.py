"""Tests for periods.py + History.record による閉じた期間の記録 (F3)。

ファイルは必ず tmp_path に置き、本物の ~/Library は触らない。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas.history import History
from quotacanvas.models import Window
from quotacanvas.periods import (ClosedPeriod, Periods, decode_line,
                                 encode_period)

UTC = timezone.utc
T0 = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)


def _win(used=10.0, resets=None, key="5h", provider="claude"):
    return Window(provider, key, "5時間", used, resets, timedelta(hours=5))


# ------------------------------------------------------------ Periods 単体

def test_record_closed_and_previous_used(tmp_path):
    periods = Periods(tmp_path / "periods.jsonl")
    closed_at = T0 + timedelta(days=7)
    periods.record_closed("claude:5h", T0 + timedelta(days=7), 64.0, closed_at)
    assert periods.previous_used("claude:5h") == pytest.approx(64.0)
    entries = periods.periods("claude:5h")
    assert len(entries) == 1
    assert entries[0].resets_at == T0 + timedelta(days=7)
    assert entries[0].closed_at == closed_at
    assert entries[0].used == pytest.approx(64.0)


def test_previous_used_returns_latest(tmp_path):
    periods = Periods(tmp_path / "periods.jsonl")
    periods.record_closed("claude:5h", T0, 20.0, T0 + timedelta(days=7))
    periods.record_closed("claude:5h", T0 + timedelta(days=7), 45.5, T0 + timedelta(days=14))
    assert periods.previous_used("claude:5h") == pytest.approx(45.5)


def test_previous_used_unknown_window_is_none(tmp_path):
    periods = Periods(tmp_path / "periods.jsonl")
    assert periods.previous_used("codex:weekly") is None


def test_previous_used_isolated_per_window(tmp_path):
    periods = Periods(tmp_path / "periods.jsonl")
    periods.record_closed("claude:5h", T0, 64.0, T0 + timedelta(days=7))
    periods.record_closed("codex:weekly", T0, 12.0, T0 + timedelta(days=7))
    assert periods.previous_used("claude:5h") == pytest.approx(64.0)
    assert periods.previous_used("codex:weekly") == pytest.approx(12.0)
    assert periods.previous_used("glm:5h") is None


def test_duplicate_resets_at_not_recorded_twice(tmp_path):
    periods = Periods(tmp_path / "periods.jsonl")
    periods.record_closed("claude:5h", T0, 64.0, T0 + timedelta(days=7))
    # API の揺れ (1 分以内) は同じ期間とみなして二重記録しない
    periods.record_closed("claude:5h", T0 + timedelta(seconds=30), 99.0,
                          T0 + timedelta(days=7, minutes=1))
    assert len(periods.periods("claude:5h")) == 1
    assert periods.previous_used("claude:5h") == pytest.approx(64.0)


def test_keeps_latest_20_per_window(tmp_path):
    periods = Periods(tmp_path / "periods.jsonl")
    for i in range(25):
        resets = T0 + timedelta(days=7 * i)
        periods.record_closed("claude:5h", resets, float(i),
                              resets + timedelta(days=7))
    entries = periods.periods("claude:5h")
    assert len(entries) == 20
    assert [e.used for e in entries] == [float(i) for i in range(5, 25)]
    # ほかの窓の行は消えていない
    periods.record_closed("codex:weekly", T0, 7.0, T0 + timedelta(days=7))
    assert len(periods.periods("claude:5h")) == 20
    assert periods.previous_used("codex:weekly") == pytest.approx(7.0)


def test_none_resets_at_roundtrip(tmp_path):
    periods = Periods(tmp_path / "periods.jsonl")
    periods.record_closed("claude:5h", None, 36.0, T0 + timedelta(hours=5))
    entry = periods.periods("claude:5h")[0]
    assert entry.resets_at is None
    assert periods.previous_used("claude:5h") == pytest.approx(36.0)


def test_broken_lines_are_ignored(tmp_path):
    path = tmp_path / "periods.jsonl"
    path.write_text(
        "not json\n"
        + '{"window_id": "claude:5h", "used": 5}\n'  # closed_at 無し
        + "\n"
        + encode_period(ClosedPeriod("claude:5h", T0, 30.0, T0 + timedelta(hours=5))) + "\n",
        encoding="utf-8",
    )
    periods = Periods(path)
    assert periods.previous_used("claude:5h") == pytest.approx(30.0)
    # 追記も壊れた行に邪魔されない
    periods.record_closed("claude:5h", T0 + timedelta(hours=5), 40.0,
                          T0 + timedelta(hours=10))
    assert [p.used for p in periods.periods("claude:5h")] == [30.0, 40.0]


def test_encode_decode_roundtrip():
    resets = T0 + timedelta(days=7)
    closed_at = resets + timedelta(days=7, minutes=3)
    period = ClosedPeriod("codex:weekly", resets, 64.5, closed_at)
    parsed = decode_line(encode_period(period))
    assert parsed == period
    assert decode_line("garbage") is None


def test_creates_missing_directories(tmp_path):
    path = tmp_path / "a" / "b" / "periods.jsonl"
    Periods(path).record_closed("claude:5h", T0, 1.0, T0 + timedelta(hours=5))
    assert path.exists()
    assert not path.with_name("periods.jsonl.tmp").exists()  # 原子的な書き込み


# --------------------------------------------- History.record からの検知 (F3)

def test_history_closes_period_when_resets_change(tmp_path):
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)
    appended = history.record(
        [_win(used=2.0, resets=r1 + timedelta(days=7))], now=T0 + timedelta(days=7))
    assert len(appended) == 1  # 新しい期間の最初のサンプル
    assert history.periods.previous_used("claude:5h") == pytest.approx(64.0)
    entry = history.periods.periods("claude:5h")[0]
    assert entry.resets_at == r1
    assert entry.closed_at == T0 + timedelta(days=7)


def test_history_keeps_no_period_while_reset_unchanged(tmp_path):
    history = History(tmp_path / "history.jsonl")
    resets = T0 + timedelta(days=7)
    history.record([_win(used=10.0, resets=resets)], now=T0)
    history.record([_win(used=20.0, resets=resets)], now=T0 + timedelta(hours=1))
    history.record([_win(used=30.0, resets=resets + timedelta(seconds=10))],
                   now=T0 + timedelta(hours=2))  # 揺れは同じ期間
    assert history.periods.previous_used("claude:5h") is None


def test_history_closes_only_the_previous_period_once(tmp_path):
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    r2 = r1 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)
    history.record([_win(used=2.0, resets=r2)], now=T0 + timedelta(days=7))
    history.record([_win(used=5.0, resets=r2)], now=T0 + timedelta(days=8))
    assert len(history.periods.periods("claude:5h")) == 1


def test_periods_file_lives_next_to_history(tmp_path):
    history = History(tmp_path / "history.jsonl")
    assert history.periods.path == tmp_path / "periods.jsonl"


def test_history_default_periods_path_follows_history_default():
    history = History()
    assert history.periods.path.name == "periods.jsonl"
    assert history.periods.path.parent == history.path.parent


def test_history_periods_path_can_be_overridden(tmp_path):
    history = History(tmp_path / "history.jsonl", periods_path=tmp_path / "p.jsonl")
    assert history.periods.path == tmp_path / "p.jsonl"
    r1 = T0 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)
    history.record([_win(used=2.0, resets=r1 + timedelta(days=7))], now=T0 + timedelta(days=7))
    assert (tmp_path / "p.jsonl").exists()
    assert not (tmp_path / "periods.jsonl").exists()


def test_history_never_touches_default_periods_when_path_given(tmp_path, monkeypatch):
    # history を tmp に置いたら periods も tmp に置かれる (本物の ~/Library は触らない)
    seen = []
    real_record = Periods.record_closed

    def spy(self, window_id, resets_at, used, closed_at):
        seen.append(self.path)
        real_record(self, window_id, resets_at, used, closed_at)

    monkeypatch.setattr(Periods, "record_closed", spy)
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)
    history.record([_win(used=2.0, resets=r1 + timedelta(days=7))], now=T0 + timedelta(days=7))
    assert seen == [tmp_path / "periods.jsonl"]


def test_periods_file_is_jsonl(tmp_path):
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)
    history.record([_win(used=2.0, resets=r1 + timedelta(days=7))], now=T0 + timedelta(days=7))
    lines = (tmp_path / "periods.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj == {
        "window_id": "claude:5h",
        "resets_at": (r1).astimezone(UTC).isoformat(),
        "used": 64.0,
        "closed_at": (T0 + timedelta(days=7)).astimezone(UTC).isoformat(),
    }
