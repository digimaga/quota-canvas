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
import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .atomic import atomic_write
from .platform_support.paths import data_dir

log = logging.getLogger("quotacanvas")

MAX_PER_WINDOW = 20   # 窓ごとに残す閉じた期間の上限
RESET_TOLERANCE = 60.0  # resets_at の API の揺れを吸う秒数 (history.py と同じ)

DEFAULT_PATH = data_dir() / "periods.jsonl"


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
    except (ValueError, OverflowError):
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


def decode_entry(obj: Any) -> Optional[ClosedPeriod]:
    """Parse one decoded JSON object (a line's contents); broken ones return None."""
    if not isinstance(obj, dict):
        return None
    closed_at = _parse_iso(obj.get("closed_at"))
    if closed_at is None:
        return None
    used = float(obj["used"])
    if not math.isfinite(used):
        return None  # NaN/inf は「前回の使用率」として扱わない
    return ClosedPeriod(
        str(obj["window_id"]),
        _parse_iso(obj.get("resets_at")),
        used,
        closed_at,
    )


def decode_line(line: str) -> Optional[ClosedPeriod]:
    """Parse one JSONL line; broken lines return None and are ignored."""
    try:
        return decode_entry(json.loads(line))
    except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return None


class Periods:
    """Closed periods per window, kept as JSONL (latest MAX_PER_WINDOW per window)."""

    def __init__(self, path: Optional[os.PathLike] = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH

    # ------------------------------------------------------------ public

    def record_closed(self, window_id: str, resets_at: Optional[datetime],
                      used: float, closed_at: datetime) -> bool:
        """1 期間ぶんを追記する (同じ resets_at の期間は二重に記録しない)。

        戻り値は保存できたか。既存ファイルを読めなかったとき・書けなかった
        ときは False を返し、呼び出し側 (History.record) が履歴を進めずに
        再試行できるようにする。"""
        entries = self._read_entries()
        if entries is None:
            # 読めないだけの既存履歴を「空」とみなして 1 件だけで置き換えると
            # 消えてしまう。書き直さずに諦める (次回また読む)。
            log.warning("periods.jsonl を読めないため記録を見送ります: %s", self.path)
            return False
        if any(p.window_id == window_id and _same_reset(p.resets_at, resets_at)
               for p in entries):
            return True
        entries.append(ClosedPeriod(window_id, resets_at, float(used),
                                    _as_utc(closed_at)))
        return self._rewrite(entries)

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

    def _read_entries(self) -> Optional[List[ClosedPeriod]]:
        """全行を読む。ファイル無しは []、読み取り失敗は None を返す。

        None と [] を区別しないと、読めなかっただけのファイルを record_closed
        が 1 件だけで置き換えて既存履歴を消してしまう。"""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("periods.jsonl を読めません: %s", exc)
            return None
        entries: List[ClosedPeriod] = []
        for raw_line in raw.splitlines():
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue
            period = decode_line(line)
            if period is not None:
                entries.append(period)
        return entries

    def _iter_file(self) -> Iterator[ClosedPeriod]:
        yield from (self._read_entries() or [])

    def _rewrite(self, entries: List[ClosedPeriod]) -> bool:
        """窓ごとに最新 MAX_PER_WINDOW 件だけ残して原子的に書き直す。

        戻り値は保存できたか (失敗しても例外は出さない)。"""
        by_window = defaultdict(list)
        for entry in entries:
            by_window[entry.window_id].append(entry)
        keep: List[ClosedPeriod] = []
        for items in by_window.values():
            items.sort(key=lambda p: p.closed_at)
            keep.extend(items[-MAX_PER_WINDOW:])
        def write(handle):
            for entry in keep:
                handle.write((encode_period(entry) + "\n").encode("utf-8"))

        try:
            atomic_write(self.path, write)
        except OSError as exc:
            log.warning("periods.jsonl に保存できません: %s", exc)
            return False
        return True
