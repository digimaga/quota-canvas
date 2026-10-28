"""Append-only observation history of window usage, stored as JSONL.

One line per Sample: {"w": window_id, "t": ISO, "u": used_percent,
"r": resets_at ISO or null}. Lines older than 8 days are dropped during
record(); the file is rewritten at most once per hour.

record() also detects the end of a window period (a new sample whose
resets_at differs from the previous one) and appends the previous period's
final used % to periods.jsonl (see periods.py), which feeds the
「前回 36% 余り」 display (Pace.previous_used).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional

from .atomic import atomic_write
from .platform_support.paths import data_dir
from .models import Sample, Window
from .periods import Periods

DEFAULT_PATH = data_dir() / "history.jsonl"

PRUNE_AFTER = timedelta(days=8)       # drop samples older than this
MIN_GAP = timedelta(minutes=10)       # always sample at least this often
PRUNE_INTERVAL = timedelta(hours=1)   # rewrite the file at most this often


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
    """APIs jitter resets_at by a few ms (or a second) between requests."""
    if a is None or b is None:
        return a is b
    return abs((a - b).total_seconds()) <= 60


def encode_sample(sample: Sample) -> str:
    """One JSONL line for a Sample."""
    return json.dumps(
        {
            "w": sample.window_id,
            "t": _to_iso(sample.at),
            "u": sample.used_percent,
            "r": _to_iso(sample.resets_at),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_line(line: str) -> Optional[Sample]:
    """Parse one JSONL line; broken lines return None and are ignored."""
    try:
        obj = json.loads(line)
        if not isinstance(obj, dict):
            return None
        at = _parse_iso(obj["t"])
        if at is None:
            return None
        resets_raw = obj.get("r")
        return Sample(
            str(obj["w"]),
            at,
            float(obj["u"]),
            _parse_iso(resets_raw) if resets_raw else None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class History:
    """Usage samples per window, appended to a JSONL file."""

    def __init__(self, path: Optional[os.PathLike] = None,
                 periods_path: Optional[os.PathLike] = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        # 閉じた期間 (前回の期間) は history.jsonl の隣の periods.jsonl に。
        # テストでは history を tmp に置くだけで本物の ~/Library を触らない。
        self.periods = Periods(periods_path) if periods_path is not None \
            else Periods(self.path.with_name("periods.jsonl"))
        self._last_prune: Optional[datetime] = None

    # ------------------------------------------------------------ public

    def record(self, windows: Iterable[Window], now: Optional[datetime] = None) -> List[Sample]:
        """Append a sample per window when something changed (or >=10min passed)."""
        moment = _as_utc(now) if now is not None else datetime.now(timezone.utc)
        latest = self.latest_by_window()
        appended: List[Sample] = []
        for window in windows:
            previous = latest.get(window.id)
            changed = (
                previous is None
                or previous.used_percent != window.used_percent
                or not _same_reset(previous.resets_at, window.resets_at)
                or (moment - previous.at) >= MIN_GAP
            )
            if changed:
                if previous is not None and not _same_reset(previous.resets_at, window.resets_at):
                    # 期間の切り替わり: 直前のサンプルを「閉じた期間」として記録する (F3)。
                    # 前後どちらかの resets_at が不明 (None) のときは、そのサンプルが
                    # 本当に前の期間の最後か分からないので閉じない。
                    if previous.resets_at is not None and window.resets_at is not None:
                        self.periods.record_closed(window.id, previous.resets_at,
                                                   previous.used_percent, moment)
                sample = Sample(window.id, moment, window.used_percent, window.resets_at)
                appended.append(sample)
                latest[window.id] = sample
        if appended:
            self._append(appended)
        self._prune_old(moment)
        return appended

    def samples(self, window_id: str) -> List[Sample]:
        """All stored samples for one window, oldest first."""
        found = [s for s in self._iter_file() if s.window_id == window_id]
        found.sort(key=lambda s: s.at)
        return found

    def latest_by_window(self) -> "dict[str, Sample]":
        """Newest sample per window id currently on disk."""
        latest: dict = {}
        for sample in self._iter_file():
            kept = latest.get(sample.window_id)
            if kept is None or sample.at >= kept.at:
                latest[sample.window_id] = sample
        return latest

    # ----------------------------------------------------------- private

    def _iter_file(self) -> Iterator[Sample]:
        try:
            lines = self.path.read_bytes().splitlines()
        except OSError:
            return
        for raw_line in lines:
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue
            sample = decode_line(line)
            if sample is not None:
                yield sample

    def _append(self, samples: List[Sample]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for sample in samples:
                handle.write(encode_sample(sample) + "\n")

    def _prune_old(self, now: datetime) -> None:
        """Drop samples older than PRUNE_AFTER, rewriting the file at most
        once per PRUNE_INTERVAL."""
        if self._last_prune is not None and (now - self._last_prune) < PRUNE_INTERVAL:
            return
        self._last_prune = now
        cutoff = now - PRUNE_AFTER
        all_samples = list(self._iter_file())
        keep = [s for s in all_samples if s.at >= cutoff]
        if len(keep) == len(all_samples):
            return
        def write(handle):
            for sample in keep:
                handle.write((encode_sample(sample) + "\n").encode("utf-8"))

        atomic_write(self.path, write)
