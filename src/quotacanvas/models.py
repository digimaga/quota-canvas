"""Data shared by providers, pacing and the renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional


@dataclass
class Window:
    """One rate-limit window of one service, as reported by its API."""

    provider: str          # "claude" | "codex" | "glm"
    key: str               # stable id within the provider, e.g. "weekly", "5h", "weekly_fable"
    label: str             # short display label, e.g. "週間", "5時間", "週間 Fable"
    used_percent: float    # 0-100
    resets_at: Optional[datetime]  # timezone-aware; None if unknown
    length: timedelta      # full window length (7d, 5h, ...)
    part_of: Optional[str] = None  # この窓が内訳として属する親窓の key。None なら独立した枠

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.key}"

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    @property
    def starts_at(self) -> Optional[datetime]:
        return self.resets_at - self.length if self.resets_at else None


@dataclass
class ResetCredit:
    """One "rate-limit reset credit": a right to instantly reset the weekly
    window (Codex). Display only; quota-canvas never redeems it."""

    title: str
    granted_at: Optional[datetime]
    expires_at: Optional[datetime]


@dataclass
class Balance:
    currency: str
    amount: float
    label: Optional[str] = None


@dataclass
class ProviderUsage:
    provider: str          # "claude" | "codex" | "glm"
    name: str              # display name, e.g. "Claude Max"
    windows: List[Window] = field(default_factory=list)
    error: Optional[str] = None   # short reason when the fetch failed
    fetched_at: Optional[datetime] = None
    reset_credits: List[ResetCredit] = field(default_factory=list)
    retry_after: Optional[float] = None  # seconds to wait before retrying (HTTP 429 Retry-After)
    stale_error: Optional[str] = None    # current fetch failure while showing last good data
    balances: List[Balance] = field(default_factory=list)


@dataclass
class Sample:
    """One stored observation of a window (for recent-rate and per-day analysis)."""

    window_id: str
    at: datetime           # timezone-aware
    used_percent: float
    resets_at: Optional[datetime]


class PaceStatus(str, Enum):
    OVER = "over"          # will run out before the reset at the projected rate
    TIGHT = "tight"        # projected to end close to 100%
    ON_TRACK = "on_track"  # roughly uses it up
    SPARE = "spare"        # projected to leave a lot unused
    EXHAUSTED = "exhausted"
    FRESH = "fresh"        # just reset and barely used: the window is open, too early to project
    UNKNOWN = "unknown"    # reset time missing or too little data


@dataclass
class Pace:
    window: Window
    status: PaceStatus
    elapsed_fraction: float             # 0-1 of the window elapsed
    ideal_used_percent: float           # elapsed_fraction * 100
    rate_per_day: Optional[float]       # projected consumption, % per day (smoothed)
    projected_end_percent: Optional[float]   # used at reset if the rate continues (may exceed 100)
    exhausts_at: Optional[datetime]     # when 100% is reached, if before the reset
    sustainable_per_day: Optional[float]     # % per day that uses exactly the rest by the reset
    adjust_percent: Optional[float]     # sustainable/rate - 1, in % (-40 = slow down 40%)
    daily_used: List[Optional[float]] = field(default_factory=list)
    # Per-day consumption (%) for each day of a multi-day window, oldest first, one entry
    # per day elapsed so far (today included). None where history doesn't cover that day.
    burst_rate_per_day: Optional[float] = None   # last-2h rate, only when clearly faster than rate_per_day
    burst_exhausts_at: Optional[datetime] = None  # when 100% is reached at the burst rate, if before the reset
    previous_used: Optional[float] = None         # final used % of the previous period (filled by cli)
    # advice = "bedtime" のときだけ載る: 次に寝る時刻 (リセットのほうが先なら None、
    # 睡眠中は None = 従来どおりリセット基準の表示)。表示側の締切「寝るまで」用。
    bedtime_at: Optional[datetime] = None
    # 表示用の「1日あたり」の許容量 (%/暦日): (100-used) / リセットまでの壁時計の
    # 日数。sustainable_per_day は起きている時間基準 (pacing.sleep 設定時は暦の
    # 1 日より短い) で rate との比較 (adjust) に使う。表示は暦の 1 日あたりが正しい。
    allowance_per_day: Optional[float] = None
    projected_leftover: Optional[float] = None  # predicted unused percentage points
    leftover_tolerance: Optional[float] = None  # tolerance at this point in the window
    # 1 日以上の窓で、使用率が整数 (端数切り捨て・丸めで最大 1% 低く見えている) のとき、
    # 1% 多く使っていたと仮定してもリセット前に 100% に届く時刻。status が over 以外のときだけ。
    # 振り分け判定 (gate.py) が境界での go を避けるために使う。表示の status は変えない。
    cautious_exhausts_at: Optional[datetime] = None
