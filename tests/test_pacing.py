"""Tests for quota pacing prediction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas.models import PaceStatus, Sample, Window
from quotacanvas.pacing import compute_pace

UTC = timezone.utc
START = datetime(2026, 10, 20, tzinfo=UTC)  # window start
RESET = START + 7 * timedelta(days=1)
NOW = START + 2 * timedelta(days=1)  # 2 days into a 7-day window


def weekly(used: float) -> Window:
    return Window("claude", "weekly", "週間", used, RESET, 7 * timedelta(days=1))


def samp(at: datetime, used: float, resets: datetime | None = None) -> Sample:
    return Sample("claude:weekly", at, used, RESET if resets is None else resets)


# --- statuses -------------------------------------------------------------


def test_over_user_example():
    """7-day window, 2 days elapsed, 40% used, no history."""
    pace = compute_pace(weekly(40.0), [], NOW)
    assert pace.status is PaceStatus.OVER
    assert pace.elapsed_fraction == pytest.approx(2 / 7)
    assert pace.ideal_used_percent == pytest.approx(200 / 7)  # ~28.57
    assert pace.rate_per_day == pytest.approx(20.0)  # 40% / 2d
    assert pace.projected_end_percent == pytest.approx(140.0)
    assert pace.exhausts_at is not None
    assert abs((pace.exhausts_at - (START + 5 * timedelta(days=1))).total_seconds()) < 1
    assert pace.sustainable_per_day == pytest.approx(12.0)  # 60% / 5d
    assert pace.adjust_percent == pytest.approx(-40.0)


def test_spare():
    pace = compute_pace(weekly(5.0), [], NOW, tz=UTC)
    assert pace.status is PaceStatus.SPARE
    assert pace.projected_end_percent == pytest.approx(17.5)
    assert pace.exhausts_at is None


def test_on_track():
    pace = compute_pace(weekly(27.0), [], NOW)
    assert pace.status is PaceStatus.ON_TRACK
    assert pace.projected_end_percent == pytest.approx(94.5)
    assert pace.exhausts_at is None


def test_tight():
    pace = compute_pace(weekly(28.0), [], NOW)
    assert pace.status is PaceStatus.TIGHT
    assert pace.projected_end_percent == pytest.approx(98.0)
    assert pace.exhausts_at is None


def test_exhausted():
    pace = compute_pace(weekly(100.0), [], NOW)
    assert pace.status is PaceStatus.EXHAUSTED
    assert pace.exhausts_at is None


def test_unknown_when_resets_missing():
    w = Window("claude", "weekly", "週間", 40.0, None, 7 * timedelta(days=1))
    pace = compute_pace(w, [], NOW)
    assert pace.status is PaceStatus.UNKNOWN
    assert pace.rate_per_day is None
    assert pace.projected_end_percent is None
    assert pace.exhausts_at is None
    assert pace.sustainable_per_day is None
    assert pace.adjust_percent is None
    assert pace.daily_used == []


def test_fresh_right_after_reset():
    # 3h into a 7d window (< 3% = ~5h) and only 2% used: FRESH, not UNKNOWN
    pace = compute_pace(weekly(2.0), [], START + timedelta(hours=3))
    assert pace.status is PaceStatus.FRESH
    assert pace.elapsed_fraction == pytest.approx(3 / (7 * 24))
    assert pace.ideal_used_percent == pytest.approx(3 / (7 * 24) * 100)
    assert pace.rate_per_day is None
    assert pace.projected_end_percent is None
    assert pace.exhausts_at is None
    assert pace.burst_rate_per_day is None
    assert pace.burst_exhausts_at is None
    assert pace.daily_used == []


def test_fresh_uses_same_thresholds_as_old_unknown():
    # 5% used (>= 5) is enough data even right after the reset: not FRESH
    pace = compute_pace(weekly(5.0), [], START + timedelta(hours=1))
    assert pace.status is not PaceStatus.FRESH
    # 3.5% elapsed (>= 3% of 7d = ~5.9h) with 4% used: not FRESH either
    pace = compute_pace(weekly(4.0), [], START + timedelta(hours=6))
    assert pace.status is not PaceStatus.FRESH


def test_unknown_when_resets_missing_and_barely_used():
    # F5: FRESH requires resets_at; without it this stays UNKNOWN
    w = Window("claude", "weekly", "週間", 2.0, None, 7 * timedelta(days=1))
    pace = compute_pace(w, [], START + timedelta(hours=3))
    assert pace.status is PaceStatus.UNKNOWN


def test_fresh_pace_carries_burst_fields():
    # F5+F2: FRESH でも直近 2 時間の加速は載る。混合 rate が無いので比較基準
    # は窓全体の平均速度 (2% / 3h = 16/d)。base 0% -> moved 2% -> burst 24/d
    # = 1.5 x 16 で加速とみなす
    now = START + timedelta(hours=3)
    pace = compute_pace(weekly(2.0),
                        [samp(now - timedelta(hours=2), 0.0)], now)
    assert pace.status is PaceStatus.FRESH
    assert pace.rate_per_day is None             # 混合 rate は FRESH のまま None
    assert pace.burst_rate_per_day == pytest.approx(24.0)
    # 98% left at 24/d -> ~4.08d, before the reset (7d - 3h away)
    assert pace.burst_exhausts_at is not None
    assert abs((pace.burst_exhausts_at
                - (now + timedelta(days=98.0 / 24.0))).total_seconds()) < 1


def test_fresh_burst_needs_baseline_ratio():
    # moved 1% -> burst 12/d < 1.5 x 16/d: 平均速度より明らかに速くないので無し
    now = START + timedelta(hours=3)
    pace = compute_pace(weekly(2.0),
                        [samp(now - timedelta(hours=2), 1.0)], now)
    assert pace.status is PaceStatus.FRESH
    assert pace.burst_rate_per_day is None
    assert pace.burst_exhausts_at is None


# --- recent rate ----------------------------------------------------------


def test_recent_rate_ignores_other_period():
    stray = samp(START + timedelta(days=1), 10.0, resets=RESET + timedelta(hours=2))
    own = samp(START + timedelta(days=1, hours=4), 20.0)
    pace = compute_pace(weekly(40.0), [stray, own], NOW)
    # own only: base at now-20h -> (40-20)/(20/24)=24/d, smoothed with 20 -> 22
    assert pace.rate_per_day == pytest.approx(22.0)
    assert pace.projected_end_percent == pytest.approx(150.0)


def test_recent_rate_skipped_when_span_too_short():
    recent = samp(NOW - timedelta(hours=2), 38.0)
    pace = compute_pace(weekly(40.0), [recent], NOW)
    # 2h span < lookback(24h)/2 -> no recent rate, whole-window average only
    assert pace.rate_per_day == pytest.approx(20.0)


def test_recent_rate_short_lookback_for_5h_window():
    start = datetime(2026, 10, 24, tzinfo=UTC)
    reset = start + timedelta(hours=5)
    w = Window("codex", "5h", "5時間", 10.0, reset, timedelta(hours=5))
    now = start + timedelta(hours=2)
    stray = samp(start + timedelta(hours=1), 6.0)  # weekly-period sample: ignored
    own = Sample("codex:5h", start + timedelta(hours=1), 6.0, reset)
    pace = compute_pace(w, [stray, own], now)
    # r_win=120/d, r_recent=(10-6)/(1/24)=96/d -> 108
    assert pace.rate_per_day == pytest.approx(108.0)


# --- burst rate (F2: 直近 2 時間の加速) ------------------------------------


def bsamp(at, used, resets=None):
    return Sample("claude:weekly", at, used, RESET if resets is None else resets)


def test_burst_rate_when_recent_speed_is_high():
    # no history -> whole-window rate 20/d; last-2h moved 3% -> 36/d >= 1.5x
    pace = compute_pace(weekly(40.0), [bsamp(NOW - timedelta(hours=2), 37.0)], NOW)
    assert pace.burst_rate_per_day == pytest.approx(36.0)
    # 60% left at 36/d -> 1.667d, before the reset (5d away)
    assert pace.burst_exhausts_at is not None
    assert abs((pace.burst_exhausts_at
                - (NOW + timedelta(days=60.0 / 36.0))).total_seconds()) < 1
    assert pace.status is PaceStatus.OVER   # status 判定は変わらない


def test_burst_needs_both_conditions():
    # moved only 0.5% in the last 2h (< 1%): no burst even though 6/d < 30/d
    pace = compute_pace(weekly(40.0), [bsamp(NOW - timedelta(hours=2), 39.5)], NOW)
    assert pace.burst_rate_per_day is None
    assert pace.burst_exhausts_at is None
    # moved 1.5% but 18/d < 1.5 x 20/d: no burst either
    pace = compute_pace(weekly(40.0), [bsamp(NOW - timedelta(hours=2), 38.5)], NOW)
    assert pace.burst_rate_per_day is None


def test_burst_exhausts_at_only_before_reset():
    # used 5 -> whole-window rate 2.5/d; last-2h 12/d >= 3.75 but 95% left
    # at 12/d is ~7.9d, past the reset: rate kept, exhaust time dropped
    pace = compute_pace(weekly(5.0), [bsamp(NOW - timedelta(hours=2), 4.0)], NOW)
    assert pace.burst_rate_per_day == pytest.approx(12.0)
    assert pace.burst_exhausts_at is None
    assert pace.status is PaceStatus.SPARE


def test_burst_ignores_other_period_and_sparse_history():
    # sample from another period, and nothing within 2h before now: no base
    stray = bsamp(NOW - timedelta(hours=3), 10.0, resets=RESET + timedelta(hours=2))
    pace = compute_pace(weekly(40.0), [stray], NOW)
    assert pace.burst_rate_per_day is None
    # base exists but is 5h old (> 2x window): the last-2h speed is unknown
    old = bsamp(NOW - timedelta(hours=5), 35.0)
    pace = compute_pace(weekly(40.0), [old], NOW)
    assert pace.burst_rate_per_day is None


# --- daily_used -----------------------------------------------------------

def test_daily_used_values():
    samples = [
        samp(START + timedelta(hours=2), 2.0),
        samp(START + timedelta(hours=27), 10.0),
        samp(START + timedelta(hours=52), 25.0),
        samp(START + timedelta(hours=10), 50.0, resets=RESET + timedelta(hours=3)),  # ignored
    ]
    pace = compute_pace(weekly(30.0), samples, START + timedelta(hours=58))
    assert len(pace.daily_used) == 3
    assert pace.daily_used[0] == pytest.approx(9.04)  # interp at S+24h
    assert pace.daily_used[1] == pytest.approx(13.56)  # interp(S+48h) - interp(S+24h)
    assert pace.daily_used[2] == pytest.approx(7.4)  # now - interp(S+48h)


def test_daily_used_empty_for_5h_window():
    start = datetime(2026, 10, 24, tzinfo=UTC)
    w = Window("codex", "5h", "5時間", 10.0, start + timedelta(hours=5), timedelta(hours=5))
    pace = compute_pace(w, [], start + timedelta(hours=2))
    assert pace.daily_used == []


def test_daily_used_unknown_before_history():
    # history only starts 2 days in: days 1-2 can't be split, so they stay unknown
    pace = compute_pace(
        weekly(9.0), [samp(START + 2 * timedelta(days=1), 8.0)],
        START + 2 * timedelta(days=1, hours=4),
    )
    assert pace.daily_used[:2] == [None, None]
    assert pace.daily_used[2] == pytest.approx(1.0)


def test_daily_used_day1_from_zero():
    # a sample within day 1 makes day 1 exact (window start is 0%)
    pace = compute_pace(
        weekly(6.0), [samp(START + timedelta(hours=20), 3.0), samp(START + timedelta(hours=28), 5.0)],
        START + timedelta(hours=30),
    )
    assert pace.daily_used[0] == pytest.approx(4.0)
    assert pace.daily_used[1] == pytest.approx(2.0)


def test_daily_used_clamps_negative():
    samples = [
        samp(START + timedelta(hours=23), 10.0),
        samp(START + timedelta(hours=25), 1.0),
    ]
    pace = compute_pace(weekly(1.0), samples, START + timedelta(hours=26))
    assert pace.daily_used[0] == pytest.approx(5.5)
    assert pace.daily_used[1] == pytest.approx(0.0)  # negative diff clamped


def test_weekly_high_usage_with_waking_hours_left_is_spare():
    from datetime import time
    reset = datetime(2026, 10, 26, 0, tzinfo=UTC)
    now = reset - timedelta(hours=15)
    win = Window("claude", "weekly", "weekly", 70, reset, timedelta(days=7))
    recent_rate = 48 - 70 * 24 / 97
    samples = [Sample(win.id, now - timedelta(days=1), 70 - recent_rate * 16 / 24, reset)]
    pace = compute_pace(win, samples, now, tz=UTC, sleep=(time(0), time(8)))
    assert pace.projected_end_percent == pytest.approx(85)
    assert pace.projected_leftover == pytest.approx(15)
    assert pace.leftover_tolerance == pytest.approx(2 + 8 * 15 / 112)
    assert pace.status is PaceStatus.SPARE


def test_weekly_20_percent_projected_90_is_spare():
    # Whole-window rate 10/day and recent rate 18/day average to 14/day.
    pace = compute_pace(weekly(20), [samp(NOW - timedelta(days=1), 2)], NOW)
    assert pace.projected_end_percent == pytest.approx(90)
    assert pace.leftover_tolerance == pytest.approx(2 + 8 * 5 / 7)
    assert pace.status is PaceStatus.SPARE


@pytest.mark.parametrize("leftover,expected", [(10, PaceStatus.ON_TRACK),
                                               (5, PaceStatus.TIGHT),
                                               (10.01, PaceStatus.SPARE)])
def test_leftover_boundaries(leftover, expected):
    # Constant tolerances make the boundary exact and independent of rounding.
    pace = compute_pace(weekly((100 - leftover) * 2 / 7), [], NOW,
                        early_tolerance=10, end_tolerance=10)
    assert pace.projected_leftover == pytest.approx(leftover)
    assert pace.status is expected


def test_missing_rate_uses_used_as_projection():
    win = Window("claude", "weekly", "weekly", 20, RESET, timedelta(days=7))
    pace = compute_pace(win, [], START)
    assert pace.rate_per_day is None
    assert pace.projected_end_percent == 20
    assert pace.projected_leftover == 80
    assert pace.leftover_tolerance == 10
    assert pace.status is PaceStatus.SPARE
