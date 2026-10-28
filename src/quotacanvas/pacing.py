"""Quota pacing: will the window run out before it resets, at the current rate?"""

from __future__ import annotations

import math
import os
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from .models import Pace, PaceStatus, Sample, Window

_DAY = timedelta(days=1)
_BURST_WINDOW = timedelta(hours=2)   # F2: 直近の加速を見る区間

# 睡眠帯 (pacing.sleep): (start, end) のローカル時刻。None = 機能オフ。
Sleep = Tuple[time, time]


def _as_days(delta: timedelta) -> float:
    return delta.total_seconds() / 86400.0


def _local_zone():
    """Use the system's regional zone, including future offset changes."""
    zone = os.environ.get("TZ")
    if zone:
        return ZoneInfo(zone.lstrip(":"))
    with open("/etc/localtime", "rb") as localtime:
        return ZoneInfo.from_file(localtime)


def _sleep_bands(a: datetime, b: datetime, sleep: Sleep, tz) -> List[Tuple[datetime, datetime]]:
    """Sleep bands clipped to [a, b), in UTC so elapsed time includes DST."""
    band_start, band_end = sleep
    if band_start == band_end:
        return []
    zone = tz if tz is not None else _local_zone()
    a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
    loc_a, loc_b = a.astimezone(zone), b.astimezone(zone)
    bands: List[Tuple[datetime, datetime]] = []
    day = loc_a.date() - _DAY
    while day <= loc_b.date():
        begin = datetime.combine(day, band_start, tzinfo=zone).astimezone(timezone.utc)
        finish = datetime.combine(day + (_DAY if band_start > band_end else timedelta(0)),
                                  band_end, tzinfo=zone).astimezone(timezone.utc)
        lo, hi = max(a, begin), min(b, finish)
        if lo < hi:
            bands.append((lo, hi))
        day += _DAY
    return bands


def _active_span(a: datetime, b: datetime, sleep: Optional[Sleep], tz) -> timedelta:
    """Elapsed awake time in [a, b), measured on the UTC timeline."""
    if sleep is None:
        return b - a
    a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
    if b <= a:
        return timedelta(0)
    out = b - a
    for s, e in _sleep_bands(a, b, sleep, tz):
        out -= e - s
    return out


def _advance_active(at: datetime, days: float, sleep: Optional[Sleep], tz) -> datetime:
    """Advance by awake time, skipping local sleep bands on the UTC timeline."""
    if sleep is None:
        return at + timedelta(days=days)
    zone = tz if tz is not None else _local_zone()
    remaining = timedelta(days=max(days, 0.0))
    cur = at.astimezone(timezone.utc)
    while remaining > timedelta(0):
        horizon = cur + remaining + 2 * _DAY
        for s, e in _sleep_bands(cur, horizon, sleep, zone):
            lead = max(timedelta(0), s - cur)
            if lead >= remaining:
                return (cur + remaining).astimezone(at.tzinfo)
            remaining -= lead
            cur = max(cur, e)
        if cur + remaining <= horizon:
            return (cur + remaining).astimezone(at.tzinfo)
    return cur.astimezone(at.tzinfo)


def _same_period(sample: Sample, resets_at: datetime) -> bool:
    """True when the sample belongs to the same window period (resets_at within 1 min)."""
    return (
        sample.resets_at is not None
        and abs((sample.resets_at - resets_at).total_seconds()) <= 60.0
    )


def _recent_rate(
    used: float, samples: List[Sample], now: datetime, wall_elapsed: timedelta,
    length: timedelta, span=None,
) -> Optional[float]:
    """Recent consumption speed (%/day) over the lookback span, or None.

    lookback (1h / 24h) と基準点探しは壁時計のまま (`wall_elapsed` は壁時計の
    経過で clamp する)。割る span だけ `span` (「長さを測る関数」、睡眠あり
    なら起きている時間) に置き換える。起きている時間が lookback の半分未満
    (短すぎて分母が縮みすぎる) なら None。
    """
    if not samples:
        return None
    cap = timedelta(hours=1) if length < _DAY else timedelta(hours=24)
    lookback = min(cap, wall_elapsed)
    if lookback <= timedelta(0):
        return None
    cutoff = now - lookback
    older = [s for s in samples if s.at <= cutoff]
    base = older[-1] if older else samples[0]  # samples are sorted by at
    wall = now - base.at
    if wall < lookback / 2 or wall.total_seconds() <= 0:
        return None
    active = wall if span is None else span(base.at, now)
    if active < lookback / 2 or active.total_seconds() <= 0:
        return None
    return max(0.0, (used - base.used_percent) / _as_days(active))


def _burst_rate(
    used: float, samples: List[Sample], now: datetime, span=None
) -> Tuple[Optional[float], float]:
    """直近 2 時間 (同一期間のサンプル) の速度 (%/day) とその間の移動量 (%)。

    基準は now-2h 以前の最後のサンプル。サンプルが近くに無い (基準が
    4 時間より前) と「直近 2 時間」の計測にならないので None を返す。
    基準点探しは壁時計のまま、割る span だけ `span` (睡眠ありなら起きて
    いる時間) に置き換える。起きている時間が 1 時間未満 (基準が睡眠の
    すぐ手前だと分母が縮んで速度が爆発する) なら None。
    戻り値は (rate_or_None, moved_percent)。
    """
    cutoff = now - _BURST_WINDOW
    base = None
    for s in samples:                       # samples は at 順
        if s.at <= cutoff:
            base = s
        else:
            break
    if base is None:
        return None, 0.0
    wall = now - base.at
    if wall.total_seconds() <= 0 or wall > 2 * _BURST_WINDOW:
        return None, 0.0
    moved = used - base.used_percent
    active = wall if span is None else span(base.at, now)
    if active < timedelta(hours=1) or active.total_seconds() <= 0:
        return None, moved
    if moved < 1.0:                          # 直近 2 時間で 1% 未満は加速と呼ばない
        return None, moved
    return max(0.0, moved / _as_days(active)), moved


def _interpolated(points: List[Tuple[datetime, float]], at: datetime) -> float:
    """Linearly interpolated used% at `at` over sorted (time, used) points."""
    if at <= points[0][0]:
        return points[0][1]
    for (a_at, a_used), (b_at, b_used) in zip(points, points[1:]):
        if at <= b_at:
            if b_at <= a_at:
                return b_used
            share = (at - a_at) / (b_at - a_at)
            return a_used + (b_used - a_used) * share
    return points[-1][1]


def _daily_used(
    window: Window, samples: List[Sample], start: datetime, now: datetime, used: float
) -> List[Optional[float]]:
    """Per-day consumption (%) since the window started; the start point counts as 0%.

    A day is None unless history covers its start (or it is day 1, which starts at 0%);
    otherwise the gap before the first sample would be smeared evenly over the days.
    """
    if window.length < _DAY:
        return []
    n_days = math.ceil(_as_days(window.length))
    elapsed = min(now - start, window.length)
    n_elapsed = min(n_days, int(elapsed // _DAY) + 1)
    if not samples or n_elapsed <= 0:
        return [None] * max(n_elapsed, 0)
    first = samples[0].at
    points = [(start, 0.0)] + [(s.at, s.used_percent) for s in samples] + [(now, used)]
    days: List[Optional[float]] = []
    for i in range(n_elapsed):
        day_start = start + i * _DAY
        day_end = min(day_start + _DAY, now)
        known_from = day_end if i == 0 else day_start
        if first > known_from:
            days.append(None)
            continue
        value = _interpolated(points, day_end) - _interpolated(points, day_start)
        days.append(max(0.0, value))
    return days


def _burst_fields(
    burst_rate: Optional[float], baseline: Optional[float],
    used: float, now: datetime, resets: datetime, advance=None
) -> Tuple[Optional[float], Optional[datetime]]:
    """F2 の 1.5 倍条件を適用し (加速後の) burst と尽きる時刻を返す。

    baseline は比較基準の速度 (混合 rate、FRESH では窓全体の平均速度)。
    None / 0 のときは基準が無いので、_burst_rate 側の「1% 以上動いた」
    条件だけを満たせば burst とみなす。尽きる時刻はリセット前のときだけ。
    `advance` があれば起きている時間ぶんだけ進めた壁時計の時刻になる。
    """
    if burst_rate is not None and baseline is not None and baseline > 0 \
            and burst_rate < 1.5 * baseline:
        burst_rate = None
    exhausts_at = None
    if burst_rate is not None and used < 100:
        days = (100.0 - used) / burst_rate
        reached = now + timedelta(days=days) if advance is None else advance(days)
        if reached < resets:
            exhausts_at = reached
    return burst_rate, exhausts_at


def compute_pace(window: Window, samples: List[Sample], now: datetime, tz=None,
                 early_tolerance: float = 10.0, end_tolerance: float = 2.0,
                 sleep: Optional[Sleep] = None,
                 bedtime: bool = False) -> Pace:
    """Pacing prediction for one window. Day splits are window-start based; tz is
    the local zone (default: system local), kept for display-side use.
    Tolerance for predicted leftover interpolates from early_tolerance to
    end_tolerance according to the remaining active fraction of the window.
    `sleep` は (start, end) のローカル時刻の睡眠帯。あるときは経過時間・残り
    時間などの「長さ」を起きている時間に置き換えて判定する (None なら従来
    どおりの壁時計)。`advance` 系の時刻は睡眠帯を飛ばして進める。
    `bedtime` は pacing.sleep の advice = "bedtime" フラグ。sleep と一緒に
    渡されたときだけ、次に寝る時刻がリセットより先なら Pace.bedtime_at に
    載せる (判定そのものは変わらない、表示側の締切差し替え用)。"""
    if sleep is not None and tz is None:
        tz = _local_zone()
    used = window.used_percent
    resets = window.resets_at
    if resets is None:
        return Pace(
            window=window, status=PaceStatus.UNKNOWN,
            elapsed_fraction=0.0, ideal_used_percent=0.0,
            rate_per_day=None, projected_end_percent=None,
            exhausts_at=None, sustainable_per_day=None, adjust_percent=None,
        )

    def span(a: datetime, b: datetime) -> timedelta:
        # 「長さを測る関数」: sleep があれば起きている時間、無ければ壁時計。
        return _active_span(a, b, sleep, tz)

    def advance(days: float) -> datetime:
        # now から起きている時間で days 日分進めた壁時計の時刻。
        return _advance_active(now, days, sleep, tz)

    # advice = "bedtime": 次の睡眠中にリセットが来る (起きたらもう次の期間) なら、
    # 表示側の締切は「寝るまで」(Pace.bedtime_at)。リセットが起きている時間に
    # 来るとき (週間枠の普段の夜など、寝ても起きた後にまだ使える) と、いま寝て
    # いる最中 (起きている時間 0) は None = 従来どおりリセット基準の表示。
    bedtime_at = None
    if bedtime and sleep is not None:
        for band_start, band_end in _sleep_bands(now, resets, sleep, tz):
            # 帯は [now, resets) で切られるので、band_end == resets が「睡眠中にリセット」
            if now < band_start < resets and band_end >= resets:
                bedtime_at = band_start
            break  # 最初 (最も近い) の帯だけでよい

    length = window.length
    # Window lengths are elapsed time, even when callers supply a regional zone.
    start = resets.astimezone(timezone.utc) - length if sleep is not None else resets - length
    active_len = span(start, resets)  # sleep が無ければ length と同じ
    elapsed = min(max(span(start, now), timedelta(0)), active_len)
    # lookback (1h / 24h) の上限は壁時計の経過で clamp する (表示や recent の
    # 「直近」の考えかたは壁時計どおり。割る分母だけ起きている時間)。
    wall_elapsed = min(max(now - start, timedelta(0)), length)
    length_days = _as_days(active_len)
    fraction = _as_days(elapsed) / length_days if length_days > 0 else 0.0
    ideal = fraction * 100.0

    same_period = sorted(
        (s for s in samples if _same_period(s, resets) and s.at <= now),
        key=lambda s: s.at,
    )

    rate_win = used / _as_days(elapsed) if elapsed.total_seconds() > 0 else None

    # F2: 直近 2 時間の加速。status 判定の前に計算し、FRESH の Pace にも
    # 載せる。FRESH (混合 rate なし) では窓全体の平均速度 rate_win を比較
    # 基準にする (経過 0 で基準が無ければ 1% 以上動いていれば burst)。
    burst_raw, _moved = _burst_rate(used, same_period, now, span=span)

    if elapsed < 0.03 * active_len and used < 5:
        # F5: リセット直後で使用もまだ少ない (この時点で resets は必ずある)。
        # 「不明」ではなく枠が開いている状態として FRESH を返す。
        # FRESH 判定の 3% も起きている時間どうしで比べる。
        burst_rate, burst_exhausts_at = _burst_fields(
            burst_raw, rate_win, used, now, resets, advance)
        return Pace(
            window=window, status=PaceStatus.FRESH,
            elapsed_fraction=fraction, ideal_used_percent=ideal,
            rate_per_day=None, projected_end_percent=None,
            exhausts_at=None, sustainable_per_day=None, adjust_percent=None,
            burst_rate_per_day=burst_rate,
            burst_exhausts_at=burst_exhausts_at,
            bedtime_at=bedtime_at,
        )

    rate_recent = _recent_rate(used, same_period, now, wall_elapsed, length, span)
    if rate_win is not None and rate_recent is not None:
        rate = 0.5 * rate_win + 0.5 * rate_recent
    else:
        rate = rate_win if rate_win is not None else rate_recent

    t_days = _as_days(span(now, resets))  # リセットまでの起きている時間
    projected = used + rate * t_days if rate is not None else used

    exhausts_at = None
    if rate is not None and rate > 0 and used < 100:
        reached = advance((100.0 - used) / rate)
        if reached < resets:
            exhausts_at = reached

    # 混合 rate が基準: 明らかに速い (1.5 倍以上) ときだけ加速とみなす。
    # status の判定は変えず、加速のまま続けたときに 100% に届く時刻を
    # 併せて出す (リセット前のみ)。
    burst_rate, burst_exhausts_at = _burst_fields(
        burst_raw, rate, used, now, resets, advance)

    sustainable = (100.0 - used) / t_days if t_days > 0 else None
    adjust = None
    if rate is not None and rate > 0 and sustainable is not None:
        adjust = (sustainable / rate - 1.0) * 100.0

    # 表示用の「1日あたり」の許容量は暦の 1 日あたり (リセットまでの壁時計の
    # 日数で割る)。睡眠中に使わない分は自然と他の起きている時間に振られるので、
    # pacing.sleep 設定時は起きている時間基準の sustainable より小さくなる。
    # sleep 未設定なら壁時計 == 起きている時間なので sustainable と同じ値。
    wall_days = _as_days(max(resets - now, timedelta(0)))
    allowance = (100.0 - used) / wall_days if wall_days > 0 else None

    leftover = 100.0 - projected
    remaining_fraction = min(1.0, max(0.0, t_days / length_days)) if length_days > 0 else 0.0
    tolerance = end_tolerance + (early_tolerance - end_tolerance) * remaining_fraction

    if used >= 100:
        status = PaceStatus.EXHAUSTED
    elif exhausts_at is not None:
        status = PaceStatus.OVER
    elif leftover > tolerance:
        status = PaceStatus.SPARE
    elif leftover <= tolerance / 2:
        status = PaceStatus.TIGHT
    else:
        status = PaceStatus.ON_TRACK

    return Pace(
        window=window, status=status, elapsed_fraction=fraction,
        ideal_used_percent=ideal, rate_per_day=rate,
        projected_end_percent=projected, exhausts_at=exhausts_at,
        projected_leftover=leftover, leftover_tolerance=tolerance,
        sustainable_per_day=sustainable, adjust_percent=adjust,
        daily_used=_daily_used(window, same_period, start, now, used),
        burst_rate_per_day=burst_rate, burst_exhausts_at=burst_exhausts_at,
        bedtime_at=bedtime_at, allowance_per_day=allowance,
    )
