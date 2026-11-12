"""Review 指摘 (persistence 部品) の回帰テスト。

- last-good.json / history.jsonl / periods.jsonl の非有限値・読み書き失敗で
  常駐エンジンが落ちないこと、既存データを消さないこと。
- status.json に NaN/Infinity を書かないこと。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quotacanvas import history as history_mod
from quotacanvas import periods as periods_mod
from quotacanvas.history import History, decode_line as decode_history_line
from quotacanvas.lastgood import LastGood
from quotacanvas.models import Pace, PaceStatus, ProviderUsage, Sample, Window
from quotacanvas.periods import Periods, decode_line as decode_period_line
from quotacanvas.status import build_status, write_status

UTC = timezone.utc
T0 = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)


def _win(used=10.0, resets=None, key="5h"):
    return Window("claude", key, "5時間", used, resets, timedelta(hours=5))


def _window_entry(key, used, length, resets=None):
    return {"key": key, "label": key, "used_percent": used,
            "resets_at": (resets or T0).isoformat(), "length": length}


def _payload(windows):
    return {"claude": {"name": "Claude", "error": None,
                       "fetched_at": T0.isoformat(), "retry_after": None,
                       "reset_credits": [], "windows": windows}}


# ------------------------------------------------------- last-good.json

def test_last_good_skips_non_finite_windows(tmp_path, caplog):
    path = tmp_path / "last-good.json"
    path.write_text(json.dumps(_payload([
        _window_entry("good", 22.0, 3600.0),
        _window_entry("nan", float("nan"), 3600.0),
        _window_entry("inf", float("inf"), 3600.0),
    ])), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        loaded = LastGood(path).data["claude"]
    assert [w.key for w in loaded.windows] == ["good"]
    assert any("非有限" in r.message for r in caplog.records)


@pytest.mark.parametrize("length", ["Infinity", float("inf"), 1e300, 8.64e13, -5.0, 0.0])
def test_last_good_skips_out_of_range_length_without_crashing(tmp_path, length):
    path = tmp_path / "last-good.json"
    path.write_text(json.dumps(_payload([
        _window_entry("good", 22.0, 3600.0),
        _window_entry("bad", 10.0, length),
    ])), encoding="utf-8")
    # 指摘 1: 以前は timedelta() の OverflowError が load から抜けて起動時に落ちた
    loaded = LastGood(path).data["claude"]
    assert [w.key for w in loaded.windows] == ["good"]


def test_last_good_skips_huge_int_window_without_dropping_provider(tmp_path, caplog):
    # 指摘 2: JSON の巨大整数は Python では任意精度の int になり、float() が
    # OverflowError を出す。窓単位で捕まえないと provider ごと消えていた。
    path = tmp_path / "last-good.json"
    path.write_text(json.dumps(_payload([
        _window_entry("good", 22.0, 3600.0),
        _window_entry("huge_used", 10 ** 400, 3600.0),
        _window_entry("huge_length", 10.0, 10 ** 400),
    ])), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        loaded = LastGood(path).data["claude"]
    assert [w.key for w in loaded.windows] == ["good"]
    warnings = [r.message for r in caplog.records if "解釈できない" in r.message]
    assert len(warnings) == 2


def test_last_good_huge_int_retry_after_becomes_none(tmp_path):
    path = tmp_path / "last-good.json"
    raw = _payload([_window_entry("good", 22.0, 3600.0)])
    raw["claude"]["retry_after"] = 10 ** 400
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = LastGood(path).data["claude"]
    assert loaded.retry_after is None
    assert [w.key for w in loaded.windows] == ["good"]


def test_last_good_lenient_to_integer_digit_limit(tmp_path, caplog):
    # Python の int 変換上限を超える桁数は json.loads 自体が ValueError を出す。
    # load が例外を出して常駐エンジンを止めないこと。
    path = tmp_path / "last-good.json"
    path.write_text('{"claude": {"windows": [{"length": %s}]}}' % ("9" * 5000),
                    encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        assert LastGood(path).data == {}
    assert any("壊れている" in r.message for r in caplog.records)


def test_poller_initializes_with_broken_last_good(tmp_path):
    from quotacanvas.cli import Poller

    path = tmp_path / "last-good.json"
    path.write_text(json.dumps(_payload([
        _window_entry("good", 22.0, 3600.0),
        _window_entry("bad", 10.0, float("inf")),
    ])), encoding="utf-8")
    poller = Poller(History(tmp_path / "history.jsonl"), enabled={"claude"},
                    last_good_path=path)
    assert [w.key for w in poller.last_good["claude"].windows] == ["good"]


def test_last_good_non_finite_retry_after_becomes_none(tmp_path):
    path = tmp_path / "last-good.json"
    raw = _payload([_window_entry("good", 22.0, 3600.0)])
    raw["claude"]["retry_after"] = float("inf")
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert LastGood(path).data["claude"].retry_after is None


# --------------------------------------------------------- history.jsonl

@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_history_decode_rejects_non_finite_used(token):
    line = '{"w": "claude:5h", "t": "%s", "u": %s, "r": null}' % (T0.isoformat(), token)
    # 指摘 2: float(obj["u"]) が NaN/inf を通していた
    assert decode_history_line(line) is None


def test_history_decode_rejects_huge_int_used():
    line = '{"w": "claude:5h", "t": "%s", "u": %s, "r": null}' % (
        T0.isoformat(), "9" * 400)
    # float() の OverflowError が decode_line から抜けて record ごと落ちていた
    assert decode_history_line(line) is None


def test_history_record_survives_huge_int_line(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"w": "claude:5h", "t": "%s", "u": %s, "r": null}\n' % (
        T0.isoformat(), "9" * 400), encoding="utf-8")
    history = History(path)
    # 壊れた行は無視され、新しいサンプルは追記できる
    assert len(history.record([_win()], now=T0 + timedelta(minutes=1))) == 1
    assert [s.used_percent for s in history.samples("claude:5h")] == [10.0]


def test_history_record_survives_unwritable_file(tmp_path):
    path = tmp_path / "history.jsonl"
    path.mkdir()  # open("a") も read_bytes も IsADirectoryError になる
    history = History(path)
    # 指摘 3: _append / _prune_old の OSError が cli まで抜けていた
    assert history.record([_win()], now=T0) == []


def test_history_prune_failure_keeps_samples_and_retries(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    path.write_text(_line(T0 - timedelta(days=9), 1.0), encoding="utf-8")
    history = History(path)
    monkeypatch.setattr("quotacanvas.history.atomic_write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert len(history.record([_win()], now=T0)) == 1
    # 整理は失敗したが、追記自体は成功している。スロットルも進めない。
    assert [s.used_percent for s in history.samples("claude:5h")] == [1.0, 10.0]
    assert history._last_prune is None


def _line(at, used, key="claude:5h"):
    from quotacanvas.history import encode_sample
    return encode_sample(Sample(key, at, used, None)) + "\n"


# -------------------------------------------------------- periods.jsonl

def test_periods_decode_rejects_non_finite_used():
    line = ('{"window_id": "claude:5h", "resets_at": null, "used": NaN, '
            '"closed_at": "%s"}' % T0.isoformat())
    assert decode_period_line(line) is None


def test_periods_decode_rejects_huge_int_used():
    line = ('{"window_id": "claude:5h", "resets_at": null, "used": %s, '
            '"closed_at": "%s"}' % ("9" * 400, T0.isoformat()))
    assert decode_period_line(line) is None


def _failing_write(*args, **kwargs):
    raise OSError("disk full")


def test_periods_write_failure_still_samples_and_retries_next_record(tmp_path, monkeypatch):
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    r2 = r1 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)

    real = periods_mod.atomic_write
    monkeypatch.setattr(periods_mod, "atomic_write", _failing_write)
    # P1: periods の保存に失敗しても、その窓のサンプルは通常どおり追記する
    appended = history.record([_win(used=2.0, resets=r2)], now=r1)
    assert len(appended) == 1
    assert [s.used_percent for s in history.samples("claude:5h")] == [64.0, 2.0]
    assert history.periods.previous_used("claude:5h") is None
    assert [(p.window_id, p.resets_at, p.used)
            for p in history._pending_periods] == [("claude:5h", r1, 64.0)]

    # 次の record のたびに保存を再試行する
    monkeypatch.setattr(periods_mod, "atomic_write", real)
    history.record([_win(used=3.0, resets=r2)], now=r1 + timedelta(minutes=1))
    assert history.periods.previous_used("claude:5h") == pytest.approx(64.0)
    assert history._pending_periods == []


def test_pending_period_survives_failure_for_many_rounds(tmp_path, monkeypatch):
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    r2 = r1 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)

    real = periods_mod.atomic_write
    monkeypatch.setattr(periods_mod, "atomic_write", _failing_write)
    for i in range(5):
        history.record([_win(used=2.0 + i, resets=r2)],
                       now=r1 + timedelta(minutes=i))
    # 何周失敗が続いても保留は 1 件のまま (重複させない)
    assert [(p.resets_at, p.used) for p in history._pending_periods] == [(r1, 64.0)]
    assert history.periods.previous_used("claude:5h") is None

    monkeypatch.setattr(periods_mod, "atomic_write", real)
    history.record([_win(used=9.0, resets=r2)], now=r1 + timedelta(minutes=6))
    assert history.periods.previous_used("claude:5h") == pytest.approx(64.0)
    assert history._pending_periods == []
    assert len(history.samples("claude:5h")) == 7


def test_pending_periods_cover_next_period_during_outage(tmp_path, monkeypatch):
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    r2 = r1 + timedelta(days=7)
    r3 = r2 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)

    real = periods_mod.atomic_write
    monkeypatch.setattr(periods_mod, "atomic_write", _failing_write)
    history.record([_win(used=30.0, resets=r2)], now=r1)              # r1 を閉じる
    history.record([_win(used=40.0, resets=r2)], now=r1 + timedelta(hours=1))
    history.record([_win(used=5.0, resets=r3)], now=r2)               # r2 も閉じる
    # 障害中に次の期間へ進んでも、閉じた 2 期間を両方とも覚えている
    assert [(p.resets_at, p.used) for p in history._pending_periods] == [
        (r1, 64.0), (r2, 40.0)]
    assert history.periods.previous_used("claude:5h") is None

    monkeypatch.setattr(periods_mod, "atomic_write", real)
    history.record([_win(used=6.0, resets=r3)], now=r2 + timedelta(hours=1))
    assert [(p.resets_at, p.used) for p in history.periods.periods("claude:5h")] == [
        (r1, 64.0), (r2, 40.0)]
    assert history.periods.previous_used("claude:5h") == pytest.approx(40.0)


def test_pending_period_dedupes_same_window_and_reset(tmp_path):
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    for used in (10.0, 99.0):
        history._remember_pending(periods_mod.ClosedPeriod("claude:5h", r1, used, T0))
    assert [(p.resets_at, p.used) for p in history._pending_periods] == [(r1, 10.0)]


def test_pending_periods_are_bounded(tmp_path):
    from quotacanvas.history import MAX_PENDING_PERIODS

    history = History(tmp_path / "history.jsonl")
    for i in range(MAX_PENDING_PERIODS + 5):
        history._remember_pending(periods_mod.ClosedPeriod(
            "claude:5h", T0 + timedelta(days=i), float(i), T0))
    assert len(history._pending_periods) == MAX_PENDING_PERIODS
    assert history._pending_periods[-1].used == pytest.approx(
        float(MAX_PENDING_PERIODS + 4))



def test_pending_period_survives_process_restart(tmp_path, monkeypatch):
    # P2: periods への保存に失敗したあとプロセスが再起動しても、保留ファイルから
    # 旧期間が復元され、復旧後に periods へ記録される
    history_path = tmp_path / "history.jsonl"
    history = History(history_path)
    r1 = T0 + timedelta(days=7)
    r2 = r1 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)

    real = periods_mod.atomic_write
    monkeypatch.setattr(periods_mod, "atomic_write", _failing_write)
    history.record([_win(used=2.0, resets=r2)], now=r1)  # r1 を閉じるが保存失敗
    assert history.periods.previous_used("claude:5h") is None
    assert (tmp_path / "periods-pending.json").exists()

    # 再起動を模して History を作り直す。保留はファイルから復元される。
    restarted = History(history_path)
    assert [(p.resets_at, p.used) for p in restarted._pending_periods] == [(r1, 64.0)]

    monkeypatch.setattr(periods_mod, "atomic_write", real)
    # 最新サンプルはもう新期間 (r2) なので再検知はできないが、復元した保留が記録される
    assert len(restarted.record([_win(used=3.0, resets=r2)],
                                now=r1 + timedelta(minutes=1))) == 1
    assert restarted.periods.previous_used("claude:5h") == pytest.approx(64.0)
    assert restarted._pending_periods == []
    assert not (tmp_path / "periods-pending.json").exists()  # 空になったら消す


def test_pending_file_unwritable_keeps_memory_retry(tmp_path, monkeypatch):
    # 保留ファイル自体が書けない環境では、今までどおりメモリ内で再試行を続ける
    history = History(tmp_path / "history.jsonl")
    r1 = T0 + timedelta(days=7)
    r2 = r1 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)

    real_periods = periods_mod.atomic_write
    real_history = history_mod.atomic_write
    monkeypatch.setattr(periods_mod, "atomic_write", _failing_write)
    monkeypatch.setattr(history_mod, "atomic_write", _failing_write)
    history.record([_win(used=2.0, resets=r2)], now=r1)
    assert [(p.resets_at, p.used) for p in history._pending_periods] == [(r1, 64.0)]
    assert not (tmp_path / "periods-pending.json").exists()

    monkeypatch.setattr(periods_mod, "atomic_write", real_periods)
    monkeypatch.setattr(history_mod, "atomic_write", real_history)
    history.record([_win(used=3.0, resets=r2)], now=r1 + timedelta(minutes=1))
    assert history.periods.previous_used("claude:5h") == pytest.approx(64.0)
    assert history._pending_periods == []


def test_pending_file_is_rewritten_once_it_becomes_writable(tmp_path, monkeypatch):
    # 保留ファイルだけが先に書けるようになり、periods の障害は続く場合も、
    # 次の record で保留ファイルを書き直す (その後の再起動で保留を失わない)
    history_path = tmp_path / "history.jsonl"
    history = History(history_path)
    r1 = T0 + timedelta(days=7)
    r2 = r1 + timedelta(days=7)
    history.record([_win(used=64.0, resets=r1)], now=T0)

    real_history = history_mod.atomic_write
    monkeypatch.setattr(periods_mod, "atomic_write", _failing_write)
    monkeypatch.setattr(history_mod, "atomic_write", _failing_write)
    history.record([_win(used=2.0, resets=r2)], now=r1)
    assert not (tmp_path / "periods-pending.json").exists()

    monkeypatch.setattr(history_mod, "atomic_write", real_history)  # 保留ファイルだけ復旧
    history.record([_win(used=3.0, resets=r2)], now=r1 + timedelta(minutes=1))
    assert (tmp_path / "periods-pending.json").exists()
    restarted = History(history_path)
    assert [(p.resets_at, p.used) for p in restarted._pending_periods] == [(r1, 64.0)]


def test_corrupted_pending_file_is_ignored_with_warning(tmp_path, caplog):
    (tmp_path / "periods-pending.json").write_text('{"pending": [trunc',
                                                    encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        history = History(tmp_path / "history.jsonl")
    assert history._pending_periods == []
    assert any("壊れている" in r.message for r in caplog.records)
    # 常駐は続く: サンプルの記録も閉じた期間の保存も普通に動く
    assert len(history.record([_win(used=10.0, resets=T0 + timedelta(days=7))],
                              now=T0)) == 1
    assert history.periods.previous_used("claude:5h") is None


def test_pending_file_with_wrong_shape_is_ignored(tmp_path, caplog):
    (tmp_path / "periods-pending.json").write_text('{"pending": []}',
                                                   encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        history = History(tmp_path / "history.jsonl")
    assert history._pending_periods == []
    assert any("形式が違う" in r.message for r in caplog.records)


def test_unreadable_pending_file_is_ignored(tmp_path, monkeypatch, caplog):
    pending = tmp_path / "periods-pending.json"
    pending.write_text("[]", encoding="utf-8")
    real = Path.read_bytes

    def fail_read(self):
        if self == pending:
            raise OSError("permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        history = History(tmp_path / "history.jsonl")
    assert history._pending_periods == []
    assert any("保留ファイルを読めない" in r.message for r in caplog.records)


def test_pending_file_skips_broken_entries_keeps_valid_ones(tmp_path):
    r1 = T0 + timedelta(days=7)
    good = {"window_id": "claude:5h", "resets_at": r1.isoformat(),
            "used": 64.0, "closed_at": T0.isoformat()}
    broken = [{"window_id": 3}, "junk", None,
              {"window_id": "claude:5h", "resets_at": None, "used": float("nan"),
               "closed_at": T0.isoformat()}]
    (tmp_path / "periods-pending.json").write_text(
        json.dumps([good] + broken), encoding="utf-8")
    history = History(tmp_path / "history.jsonl")
    assert [(p.resets_at, p.used) for p in history._pending_periods] == [(r1, 64.0)]
    # 次の record で保存され、保留ファイルも消える
    history.record([_win(used=1.0, resets=r1 + timedelta(days=7))], now=T0)
    assert history.periods.previous_used("claude:5h") == pytest.approx(64.0)
    assert history._pending_periods == []
    assert not (tmp_path / "periods-pending.json").exists()


def test_pending_file_load_respects_bound(tmp_path):
    from quotacanvas.history import MAX_PENDING_PERIODS

    entries = [{"window_id": "claude:5h",
                "resets_at": (T0 + timedelta(days=i)).isoformat(),
                "used": float(i), "closed_at": T0.isoformat()}
               for i in range(MAX_PENDING_PERIODS + 5)]
    (tmp_path / "periods-pending.json").write_text(json.dumps(entries),
                                                   encoding="utf-8")
    history = History(tmp_path / "history.jsonl")
    assert len(history._pending_periods) == MAX_PENDING_PERIODS
    assert history._pending_periods[-1].used == pytest.approx(
        float(MAX_PENDING_PERIODS + 4))


def test_periods_read_failure_does_not_replace_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "periods.jsonl"
    store = Periods(path)
    store.record_closed("claude:5h", T0, 64.0, T0)
    before = Path.read_bytes(path)

    real = Path.read_bytes

    def fail_read(self):
        if self == path:
            raise OSError("permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    # 指摘 4: 読めなかっただけの既存履歴を「空」として 1 件で置き換えない
    assert store.record_closed("claude:5h", T0 + timedelta(days=7), 10.0,
                               T0 + timedelta(days=7)) is False
    assert real(path) == before
    monkeypatch.undo()
    assert [p.used for p in store.periods("claude:5h")] == [64.0]


# ------------------------------------------------------------ status.json

def _cfg(tmp_path):
    from quotacanvas.config import load_config
    return load_config(tmp_path / "config.toml")


def _pace(win):
    return Pace(window=win, status=PaceStatus.UNKNOWN, elapsed_fraction=0.0,
                ideal_used_percent=0.0, rate_per_day=None,
                projected_end_percent=None, exhausts_at=None,
                sustainable_per_day=None, adjust_percent=None)


def test_build_status_drops_non_finite_windows(tmp_path):
    good = _win(used=30.0)
    bad = Window("claude", "bad", "bad", float("nan"), None, timedelta(hours=5))
    pu = ProviderUsage("claude", "Claude", [good, bad], None, T0)
    status = build_status(_cfg(tmp_path), [(pu, _pace(good)), (pu, _pace(bad))],
                          [pu], T0)
    assert [c["id"] for c in status["cards"]] == ["claude:5h"]
    assert [w["id"] for w in status["windows"]] == ["claude:5h"]
    # 指摘 5: allow_nan=False でも書ける (非有限値が残っていない)
    write_status(status, tmp_path / "status.json")


def test_build_status_omits_non_finite_previous_used(tmp_path):
    win = _win(used=30.0)
    pu = ProviderUsage("claude", "Claude", [win], None, T0)
    pace = _pace(win)
    pace.previous_used = float("inf")
    status = build_status(_cfg(tmp_path), [(pu, pace)], [pu], T0)
    assert "previous_used" not in status["cards"][0]


def test_write_status_rejects_nan_and_keeps_old_file(tmp_path):
    path = tmp_path / "status.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    # 指摘 5: 非標準 JSON (NaN) を書かず ValueError で呼び出し側に知らせる
    with pytest.raises(ValueError):
        write_status({"used": float("nan")}, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["status.json"]
