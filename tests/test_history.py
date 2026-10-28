"""Tests for history.py: append conditions, pruning, robustness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas.history import History, decode_line, encode_sample
from quotacanvas.models import Window

UTC = timezone.utc
T0 = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)


def _win(provider="claude", key="5h", used=10.0, resets=None):
    return Window(provider, key, "5時間", used, resets, timedelta(hours=5))


def _sid(window):
    return window.id


def test_record_appends_first_sample(tmp_path):
    history = History(tmp_path / "history.jsonl")
    appended = history.record([_win(used=10.0)], now=T0)
    assert len(appended) == 1
    samples = history.samples(_sid(_win()))
    assert len(samples) == 1
    assert samples[0].window_id == "claude:5h"
    assert samples[0].at == T0
    assert samples[0].used_percent == pytest.approx(10.0)
    assert samples[0].resets_at is None


def test_record_skips_when_nothing_changed_within_gap(tmp_path):
    history = History(tmp_path / "history.jsonl")
    window = _win(used=10.0, resets=T0 + timedelta(hours=2))
    history.record([window], now=T0)
    appended = history.record([window], now=T0 + timedelta(minutes=9, seconds=59))
    assert appended == []
    assert len(history.samples("claude:5h")) == 1


def test_record_appends_on_used_change(tmp_path):
    history = History(tmp_path / "history.jsonl")
    history.record([_win(used=10.0)], now=T0)
    appended = history.record([_win(used=11.0)], now=T0 + timedelta(minutes=1))
    assert len(appended) == 1
    assert [s.used_percent for s in history.samples("claude:5h")] == [10.0, 11.0]


def test_record_appends_on_resets_change(tmp_path):
    history = History(tmp_path / "history.jsonl")
    history.record([_win(used=10.0, resets=T0 + timedelta(hours=2))], now=T0)
    appended = history.record(
        [_win(used=10.0, resets=T0 + timedelta(hours=3))], now=T0 + timedelta(minutes=1)
    )
    assert len(appended) == 1


def test_record_appends_after_gap_even_if_identical(tmp_path):
    history = History(tmp_path / "history.jsonl")
    window = _win(used=10.0, resets=T0 + timedelta(hours=2))
    history.record([window], now=T0)
    appended = history.record([window], now=T0 + timedelta(minutes=10))
    assert len(appended) == 1
    assert len(history.samples("claude:5h")) == 2


def test_samples_sorted_by_time(tmp_path):
    history = History(tmp_path / "history.jsonl")
    history.record([_win(used=30.0)], now=T0 + timedelta(hours=2))
    history.record([_win(used=10.0)], now=T0)
    history.record([_win(used=20.0)], now=T0 + timedelta(hours=1))
    used = [s.used_percent for s in history.samples("claude:5h")]
    assert used == [10.0, 20.0, 30.0]


def test_samples_isolated_per_window(tmp_path):
    history = History(tmp_path / "history.jsonl")
    history.record([_win(key="5h", used=10.0), _win(provider="glm", key="5h", used=50.0)],
                   now=T0)
    assert len(history.samples("claude:5h")) == 1
    assert len(history.samples("glm:5h")) == 1
    assert history.samples("codex:5h") == []


def test_creates_missing_directories(tmp_path):
    path = tmp_path / "a" / "b" / "history.jsonl"
    History(path).record([_win(used=1.0)], now=T0)
    assert path.exists()


def test_broken_lines_are_ignored(tmp_path):
    path = tmp_path / "history.jsonl"
    good1 = encode_line(T0, 10.0)
    good2 = encode_line(T0 + timedelta(minutes=30), 20.0)
    path.write_text(
        "not json\n"
        + '["also", "not", "an", "object"]\n'
        + '{"w": "claude:5h", "t": "not-a-date", "u": 5, "r": null}\n'
        + '{"w": "claude:5h", "u": 5}\n'  # t 無し
        + "\n"
        + good1 + "\n"
        + good2 + "\n",
        encoding="utf-8",
    )
    history = History(path)
    samples = history.samples("claude:5h")
    assert [s.used_percent for s in samples] == [10.0, 20.0]
    # 追記も壊れた行に邪魔されない
    history.record([_win(used=30.0)], now=T0 + timedelta(minutes=60))
    assert [s.used_percent for s in history.samples("claude:5h")] == [10.0, 20.0, 30.0]


def encode_line(at, used, resets=None, window_id="claude:5h"):
    return '{"w": "%s", "t": "%s", "u": %s, "r": %s}' % (
        window_id, at.isoformat(), used,
        '"%s"' % resets.isoformat() if resets else "null",
    )


def test_prune_drops_samples_older_than_8_days(tmp_path):
    history = History(tmp_path / "history.jsonl")
    now = T0
    old_time = now - timedelta(days=9)
    edge_time = now - timedelta(days=7)
    lines = "\n".join([
        encode_line(old_time, 1.0),
        encode_line(edge_time, 2.0),
    ]) + "\n"
    (tmp_path / "history.jsonl").write_text(lines, encoding="utf-8")

    history.record([_win(used=10.0)], now=now)

    samples = history.samples("claude:5h")
    assert [s.used_percent for s in samples] == [2.0, 10.0]  # 9日前の行は消える


def test_prune_rewrites_at_most_once_per_hour(tmp_path):
    path = tmp_path / "history.jsonl"
    history = History(path)
    now = T0

    # 1回目の record で prune が走り、9日前の行が消える
    path.write_text(encode_line(now - timedelta(days=9), 1.0) + "\n", encoding="utf-8")
    history.record([_win(used=10.0)], now=now)
    assert len(history.samples("claude:5h")) == 1  # 古い行は消えた

    # 1時間以内の 2 回目: prune はスロットルされ、古い行が再び混ざっても消えない
    with path.open("a", encoding="utf-8") as fh:
        fh.write(encode_line(now - timedelta(days=9), 99.0) + "\n")
    history.record([_win(used=11.0)], now=now + timedelta(minutes=5))
    assert [s.used_percent for s in history.samples("claude:5h")] == [99.0, 10.0, 11.0]

    # 1時間後の record で prune が再び走り、古い行が消える
    history.record([_win(used=12.0)], now=now + timedelta(hours=1, minutes=1))
    assert [s.used_percent for s in history.samples("claude:5h")] == [10.0, 11.0, 12.0]


def test_decode_and_encode_roundtrip():
    resets = T0 + timedelta(hours=5)
    sample = decode_line(encode_line(T0, 42.5, resets))
    assert sample is not None
    assert sample.window_id == "claude:5h"
    assert sample.at == T0
    assert sample.used_percent == 42.5
    assert sample.resets_at == resets
    assert decode_line("garbage") is None
    assert decode_line('{"w": "a", "t": "bad", "u": 1, "r": null}') is None


def test_default_path_points_to_application_support():
    history = History()
    assert str(history.path).endswith("history.jsonl")
    assert "quota-canvas" in str(history.path)


# ------------------------------------- 閉じた期間の記録 (F3, periods.jsonl)

def test_record_closes_previous_period_when_resets_change(tmp_path):
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)
    history.record([_win(used=2.0, resets=r1 + timedelta(days=7))], now=T0 + timedelta(days=7))
    assert history.periods.previous_used("claude:5h") == pytest.approx(64.0)
    entry = history.periods.periods("claude:5h")[0]
    assert entry.resets_at == r1
    assert entry.closed_at == T0 + timedelta(days=7)


def test_record_keeps_no_period_while_resets_stay_same(tmp_path):
    history = History(tmp_path / "history.jsonl")
    resets = T0 + timedelta(hours=2)
    history.record([_win(used=10.0, resets=resets)], now=T0)
    history.record([_win(used=20.0, resets=resets)], now=T0 + timedelta(minutes=10))
    history.record([_win(used=30.0, resets=resets + timedelta(seconds=5))],
                   now=T0 + timedelta(minutes=20))  # 揺れは同じ期間
    assert history.periods.previous_used("claude:5h") is None
    assert not (tmp_path / "periods.jsonl").exists()


def test_record_period_next_to_history_not_in_home(tmp_path):
    # periods も history と同じディレクトリ (tmp) にできる
    history = History(tmp_path / "nested" / "history.jsonl")
    assert history.periods.path == tmp_path / "nested" / "periods.jsonl"


def test_record_does_not_close_period_when_previous_resets_unknown(tmp_path):
    # 前のサンプルの resets_at が不明: それが本当に前の期間の最後か分からないので閉じない
    history = History(tmp_path / "history.jsonl")
    history.record([_win(used=64.0, resets=None)], now=T0)
    history.record([_win(used=2.0, resets=T0 + timedelta(days=7))], now=T0 + timedelta(days=7))
    assert history.periods.previous_used("claude:5h") is None
    assert not (tmp_path / "periods.jsonl").exists()


def test_record_does_not_close_period_when_new_resets_unknown(tmp_path):
    # 新しいサンプルの resets_at が不明: 同じく閉じない
    history = History(tmp_path / "history.jsonl")
    history.record([_win(used=64.0, resets=T0 + timedelta(days=7))], now=T0)
    history.record([_win(used=2.0, resets=None)], now=T0 + timedelta(days=7))
    assert history.periods.previous_used("claude:5h") is None
    assert not (tmp_path / "periods.jsonl").exists()


def test_invalid_utf8_history_is_ignored(tmp_path):
    path = tmp_path / "history.jsonl"
    history = History(path)
    history.record([_win()], now=T0)
    with path.open("ab") as handle:
        handle.write(b"\xff\n")
    assert len(history.samples("claude:5h")) == 1
    assert "claude:5h" in history.latest_by_window()
