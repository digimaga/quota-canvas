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
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional

from .atomic import atomic_write
from .platform_support.paths import data_dir
from .models import Sample, Window
from .periods import ClosedPeriod, Periods, decode_entry

log = logging.getLogger("quotacanvas")

DEFAULT_PATH = data_dir() / "history.jsonl"

PRUNE_AFTER = timedelta(days=8)       # drop samples older than this
MIN_GAP = timedelta(minutes=10)       # always sample at least this often
PRUNE_INTERVAL = timedelta(hours=1)   # rewrite the file at most this often
MAX_PENDING_PERIODS = 32              # periods へ保存できず保持する閉期間の上限
PENDING_FILENAME = "periods-pending.json"  # 保留中の閉期間の退避先 (history.jsonl と同じフォルダ)


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
        used = float(obj["u"])
        if not math.isfinite(used):
            return None  # NaN/inf は JSON としても表示値としても扱わない
        resets_raw = obj.get("r")
        return Sample(
            str(obj["w"]),
            at,
            used,
            _parse_iso(resets_raw) if resets_raw else None,
        )
    except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
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
        # periods.jsonl へ保存できなかった閉じた期間。履歴サンプルとは独立に
        # 保持して record のたびに保存を再試行する。再起動で失われないよう、
        # history.jsonl と同じフォルダの小さなファイルに退避し、生成時に復元する。
        self._pending_path = self.path.with_name(PENDING_FILENAME)
        self._pending_periods: List[ClosedPeriod] = self._load_pending()
        # 保留ファイルへの反映に失敗したら True。リストが変わらなくても、
        # 次の record で書き直しを再試行する (再起動で保留を失わないため)。
        self._pending_dirty = False

    # ------------------------------------------------------------ public

    def record(self, windows: Iterable[Window], now: Optional[datetime] = None) -> List[Sample]:
        """Append a sample per window when something changed (or >=10min passed)."""
        moment = _as_utc(now) if now is not None else datetime.now(timezone.utc)
        self._flush_pending_periods()
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
                        self._save_closed(ClosedPeriod(
                            window.id, previous.resets_at, previous.used_percent, moment))
                # 閉じた期間を保存できなくても、サンプルは通常どおり追記する。
                # 保存できなかった期間は _pending_periods が覚えているので、
                # 履歴を止めなくても復旧後に再試行できる。
                sample = Sample(window.id, moment, window.used_percent, window.resets_at)
                appended.append(sample)
                latest[window.id] = sample
        if appended and not self._append(appended):
            appended = []
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

    def _save_closed(self, period: ClosedPeriod) -> bool:
        """閉じた期間を保存する。失敗したら再試行用に保持して False。"""
        if self.periods.record_closed(period.window_id, period.resets_at,
                                      period.used, period.closed_at):
            return True
        self._remember_pending(period)
        log.warning("閉じた期間を保存できないため保留します: %s", period.window_id)
        return False

    def _remember_pending(self, period: ClosedPeriod) -> None:
        """同じ (window_id, resets_at) は重複させず、上限を超えたら古い順に捨てる。"""
        if any(kept.window_id == period.window_id
               and _same_reset(kept.resets_at, period.resets_at)
               for kept in self._pending_periods):
            return
        self._pending_periods.append(period)
        while len(self._pending_periods) > MAX_PENDING_PERIODS:
            dropped = self._pending_periods.pop(0)
            log.warning("保留中の閉じた期間が上限を超えたため古いものを捨てます: %s",
                        dropped.window_id)
        self._persist_pending()

    def _flush_pending_periods(self) -> None:
        """保持している閉じた期間の保存を再試行する (古い順)。"""
        if not self._pending_periods:
            if self._pending_dirty:
                self._persist_pending()  # 消せなかった保留ファイルの後始末
            return
        pending = self._pending_periods
        remaining: List[ClosedPeriod] = []
        for index, period in enumerate(pending):
            if not self.periods.record_closed(period.window_id, period.resets_at,
                                              period.used, period.closed_at):
                # 保存障害は環境側 (ディスク等) のことが多いので、1 回失敗したら
                # この record では打ち切り、残りは次回まとめて再試行する。
                remaining = pending[index:]
                break
        self._pending_periods = remaining
        if len(remaining) != len(pending) or self._pending_dirty:
            self._persist_pending()

    def _load_pending(self) -> List[ClosedPeriod]:
        """保留ファイルから閉じた期間を復元する (History 生成時)。

        壊れた・読めないファイルは警告して無視する。復元できなくても常駐
        エンジンは落とさない (履歴サンプルの記録は続く)。"""
        try:
            raw = self._pending_path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("保留ファイルを読めないため無視します: %s", exc)
            return []
        try:
            items = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            log.warning("保留ファイルが壊れているため無視します: %s", exc)
            return []
        if not isinstance(items, list):
            log.warning("保留ファイルの形式が違うため無視します: %s", self._pending_path)
            return []
        pending: List[ClosedPeriod] = []
        for item in items:
            try:
                period = decode_entry(item)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue  # 壊れた 1 件だけなら他の保留は生かす
            if period is not None:
                pending.append(period)
        return pending[-MAX_PENDING_PERIODS:]

    def _persist_pending(self) -> None:
        """保留リストをファイルに反映する。空になったらファイルを消す。

        書けなくてもメモリ内での再試行は今までどおり続く (次に書けたときに
        内容を上書きするので、書けなかった分が二重に復元されることもない)。"""
        if not self._pending_periods:
            try:
                self._pending_path.unlink(missing_ok=True)
            except OSError as exc:
                self._pending_dirty = True
                log.warning("保留ファイルを消せません: %s", exc)
            else:
                self._pending_dirty = False
            return
        entries = [{"window_id": period.window_id,
                    "resets_at": _to_iso(period.resets_at),
                    "used": period.used,
                    "closed_at": _to_iso(period.closed_at)}
                   for period in self._pending_periods]

        def write(handle):
            handle.write(payload.encode("utf-8"))

        try:
            payload = json.dumps(entries, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False)
            atomic_write(self._pending_path, write)
        except (OSError, ValueError) as exc:
            self._pending_dirty = True
            log.warning("保留ファイルに書けないためメモリ内で再試行します: %s", exc)
        else:
            self._pending_dirty = False

    def _iter_file(self) -> Iterator[Sample]:
        try:
            lines = self.path.read_bytes().splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("history.jsonl を読めません: %s", exc)
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

    def _append(self, samples: List[Sample]) -> bool:
        """追記できたら True。書けなくても常駐ループを止めない (次回また試す)。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for sample in samples:
                    handle.write(encode_sample(sample) + "\n")
        except OSError as exc:
            log.warning("history.jsonl に追記できません: %s", exc)
            return False
        return True

    def _prune_old(self, now: datetime) -> None:
        """Drop samples older than PRUNE_AFTER, rewriting the file at most
        once per PRUNE_INTERVAL."""
        if self._last_prune is not None and (now - self._last_prune) < PRUNE_INTERVAL:
            return
        cutoff = now - PRUNE_AFTER
        all_samples = list(self._iter_file())
        keep = [s for s in all_samples if s.at >= cutoff]
        if len(keep) != len(all_samples):
            def write(handle):
                for sample in keep:
                    handle.write((encode_sample(sample) + "\n").encode("utf-8"))

            try:
                atomic_write(self.path, write)
            except OSError as exc:
                # 読めなかった場合も含め、失敗したら次回また試せるよう
                # _last_prune を進めない (1 時間のスロットルに入れない)。
                log.warning("history.jsonl を整理できません: %s", exc)
                return
        self._last_prune = now
