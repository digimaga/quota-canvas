"""閉じた窓期間 (「前回の期間」) の記録 (F3)。

History.record が、ある窓の新しいサンプルの resets_at が直前のサンプルと
違う (=_same_reset が偽) のを検知したとき、直前のサンプルの使用率を
「閉じた期間」として periods.jsonl に追記する。次の期間のカードに
「前回 36% 余り」と出すのに使う (cli.build_cards -> Pace.previous_used)。

1 行 = {"window_id": "...", "resets_at": ISO|null, "used": 64.0, "closed_at": ISO}。
窓ごとに最新 MAX_PER_WINDOW 件だけ残す。既定の保存先は history.jsonl の隣
(~/Library/Application Support/quota-canvas/periods.jsonl)。
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

MAX_PER_WINDOW = 20   # 窓ごとに残す閉じた期間の上限
RESET_TOLERANCE = 60.0  # resets_at の API の揺れを吸う秒数 (history.py と同じ)

DEFAULT_PATH = (Path.home() / "Library" / "Application Support" / "quota-canvas"
                / "periods.jsonl")


@dataclass
class ClosedPeriod:
    """One finished window period: its reset time and the final used %."""

    window_id: str
    resets_at: Optional[datetime]   # その期間のリセット時刻 (不明なら None)
    used: float                     # 期間終了時点の使用率 (%)
    closed_at: datetime             # 期間の終わり (次の期間の最初のサンプル時刻) を検知した時刻


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_iso(value: Optional[datetime]) -> Optional[str]:
    return _as_utc(value).isoformat() if value is not None else None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def _same_reset(a: Optional[datetime], b: Optional[datetime]) -> bool:
    if a is None or b is None:
        return a is b
    return abs((a - b).total_seconds()) <= RESET_TOLERANCE


def encode_period(period: ClosedPeriod) -> str:
    """One JSONL line for a ClosedPeriod."""
    return json.dumps(
        {
            "window_id": period.window_id,
            "resets_at": _to_iso(period.resets_at),
            "used": period.used,
            "closed_at": _to_iso(period.closed_at),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_line(line: str) -> Optional[ClosedPeriod]:
    """Parse one JSONL line; broken lines return None and are ignored."""
    try:
        obj = json.loads(line)
        if not isinstance(obj, dict):
            return None
        closed_at = _parse_iso(obj["closed_at"])
        if closed_at is None:
            return None
        return ClosedPeriod(
            str(obj["window_id"]),
            _parse_iso(obj.get("resets_at")),
            float(obj["used"]),
            closed_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class Periods:
    """Closed periods per window, kept as JSONL (latest MAX_PER_WINDOW per window)."""

    def __init__(self, path: Optional[os.PathLike] = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH

    # ------------------------------------------------------------ public

    def record_closed(self, window_id: str, resets_at: Optional[datetime],
                      used: float, closed_at: datetime) -> None:
        """1 期間ぶんを追記する (同じ resets_at の期間は二重に記録しない)。"""
        entries = list(self._iter_file())
        if any(p.window_id == window_id and _same_reset(p.resets_at, resets_at)
               for p in entries):
            return
        entries.append(ClosedPeriod(window_id, resets_at, float(used),
                                    _as_utc(closed_at)))
        self._rewrite(entries)

    def periods(self, window_id: str) -> List[ClosedPeriod]:
        """All closed periods of one window, oldest first."""
        found = [p for p in self._iter_file() if p.window_id == window_id]
        found.sort(key=lambda p: p.closed_at)
        return found

    def previous_used(self, window_id: str) -> Optional[float]:
        """最新の閉じた期間の最終使用率。閉じた期間が無ければ None。"""
        periods = self.periods(window_id)
        return periods[-1].used if periods else None

    # ----------------------------------------------------------- private

    def _iter_file(self) -> Iterator[ClosedPeriod]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, OSError):
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            period = decode_line(line)
            if period is not None:
                yield period

    def _rewrite(self, entries: List[ClosedPeriod]) -> None:
        """窓ごとに最新 MAX_PER_WINDOW 件だけ残して原子的に書き直す。"""
        by_window = defaultdict(list)
        for entry in entries:
            by_window[entry.window_id].append(entry)
        keep: List[ClosedPeriod] = []
        for items in by_window.values():
            items.sort(key=lambda p: p.closed_at)
            keep.extend(items[-MAX_PER_WINDOW:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for entry in keep:
                handle.write(encode_period(entry) + "\n")
        os.replace(tmp, self.path)
