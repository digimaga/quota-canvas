"""Tests for pacing.sleep: 睡眠帯を除いた「起きている時間」でのペース判定。

時刻はすべて tz=UTC で compute_pace を呼ぶので「ローカル時刻 = UTC」となり、
睡眠帯 (start, end) も UTC の壁時計として確定して試せる。
"""

from __future__ import annotations

import logging
import os
import time as time_mod
import tomllib
from datetime import datetime, time, timedelta, timezone

import pytest

from quotacanvas.config import (
    ConfigError, Sleep, config_from_dict, config_to_dict, format_toml,
    load_config, sleep_for,
)
from quotacanvas.history import History
from quotacanvas.models import PaceStatus, ProviderUsage, Sample, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.recommend import recommend

UTC = timezone.utc
# 例のケース: Grok の週間。リセット 04:00、睡眠 00:00-08:00、now 21:00 (前夜)。
# 窓の開始は 10-19 04:00、now 10-25 21:00 までの壁時計経過は 6日17時間 (161h)、
# この間の睡眠は 10-19 の 04:00-08:00 (4h) と 10-20..25 の 8h×6 (48h) の 52h。
# 起きている経過は 109h。窓全体の起きている長さは 168h - 56h = 112h。
# リセット 10-26 04:00 までの残りは壁時計 7h、起きているのは 21:00-00:00 の 3h。
RESET = datetime(2026, 10, 26, 4, 0, 0, tzinfo=UTC)
START = RESET - timedelta(days=7)
NOW = datetime(2026, 10, 25, 21, 0, 0, tzinfo=UTC)
SLEEP = (time(0, 0), time(8, 0))


@pytest.fixture
def utc_local_tz():
    """システムのローカルタイムゾーンを UTC にする。

    build_cards / recommend は tz を指定せず compute_pace を呼ぶので、
    ローカル時刻がテストの時刻 (UTC) と一致するようにする。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time_mod.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time_mod.tzset()


def grok_weekly(used: float, resets: datetime = RESET) -> Window:
    return Window("grok", "weekly", "週間", used, resets, timedelta(days=7))


def samp(at: datetime, used: float, resets: datetime = RESET) -> Sample:
    return Sample("grok:weekly", at, used, resets)


# --- 例のケース: tight -------------------------------------------------------


def test_example_case_tight_when_rate_fits_waking_hours():
    # used 91%: 起きている経過 109h で rate = 91*24/109 = 20.04/日。
    # 残りの起きている 3h で +2.5% -> 予測 93.5% -> 使い切り (tight)。
    pace = compute_pace(grok_weekly(91.0), [], NOW, tz=UTC, sleep=SLEEP)
    assert pace.status is PaceStatus.TIGHT
    assert pace.elapsed_fraction == pytest.approx(109 / 112)
    assert pace.rate_per_day == pytest.approx(91 * 24 / 109)
    assert pace.projected_end_percent == pytest.approx(91 * 112 / 109)
    assert pace.exhausts_at is None
    assert pace.sustainable_per_day == pytest.approx(9.0 / (3 / 24))  # 72/日
    # 100% に届くのは起きている時間で 10.8h 先 = 睡眠を飛ばして 10-26 15:47 で、
    # リセット (04:00) を過ぎるので exhausts_at は付かない


def test_sleep_changes_projection_from_wall_clock():
    # 同じ used でも sleep が無いと壁時計の残り 7h で割る: 予測は 95.0%。
    # sleep があると「起きている残り 3h」だけなので予測が下がる (93.5%)。
    pace = compute_pace(grok_weekly(91.0), [], NOW, tz=UTC)
    assert pace.projected_end_percent == pytest.approx(91 * 168 / 161)
    slept = compute_pace(grok_weekly(91.0), [], NOW, tz=UTC, sleep=SLEEP)
    assert slept.projected_end_percent == pytest.approx(91 * 112 / 109)
    assert slept.projected_end_percent < pace.projected_end_percent
    # リセット 04:00 を含む最後の 7h は 4/7 が睡眠なので、起きている時間ベースの
    # 予測は壁時計より下になる。壁時計 on_track -> 起きている時間 tight の反転は
    # 残りに睡眠があまり含まれないケース (test_wall_on_track_turns_tight) で試す。


def test_wall_on_track_turns_tight():
    # リセットが 0 時 (= 実質の締め切りどおり) の週間、now 21:00 の前夜。
    # 残り 27h のうち睡眠は 8h (30%) で、経過 141h の睡眠 48h (34%) より少ない。
    # used 77%: 壁時計の予測 91.7% -> on_track。起きている時間の予測 92.7% -> tight。
    reset = datetime(2026, 10, 27, 0, 0, 0, tzinfo=UTC)
    win = Window("claude", "weekly", "週間", 77.0, reset, timedelta(days=7))
    wall = compute_pace(win, [], NOW, tz=UTC)
    assert wall.status is PaceStatus.ON_TRACK
    assert wall.projected_end_percent == pytest.approx(77 * 168 / 141)
    slept = compute_pace(win, [], NOW, tz=UTC, sleep=SLEEP)
    assert slept.status is PaceStatus.TIGHT
    assert slept.projected_end_percent == pytest.approx(77 * 112 / 93)


# --- over: 0 時より前に尽きる -------------------------------------------------


def test_over_exhausts_before_midnight():
    # used 98%: rate 21.6/日 -> 残り 2% は起きている 2.22h で尽きる (23:13)。
    pace = compute_pace(grok_weekly(98.0), [], NOW, tz=UTC, sleep=SLEEP)
    assert pace.status is PaceStatus.OVER
    assert pace.exhausts_at is not None
    expected = NOW + timedelta(days=2 * 109 / (98 * 24))
    assert abs((pace.exhausts_at - expected).total_seconds()) < 1
    midnight = datetime(2026, 10, 26, 0, 0, 0, tzinfo=UTC)
    assert pace.exhausts_at < midnight  # 0 時より前
    assert pace.exhausts_at < RESET


# --- exhausts_at は睡眠帯を飛ばす ---------------------------------------------


def test_exhausts_at_skips_sleep_band():
    # 週間のリセットが 10-26 13:00、now 21:00 (前夜)、used 1040/11%。
    # rate = 240/11 = 21.8/日 -> 残りは起きている 6h で尽きる。
    # 21:00 + 3h (0 時まで) + 睡眠 8h を飛ばし + 3h = 10-26 11:00 (リセット前)。
    # 壁時計なら 21:00 + 6h = 10-26 03:00 になる。
    reset = datetime(2026, 10, 26, 13, 0, 0, tzinfo=UTC)
    pace = compute_pace(grok_weekly(1040 / 11, reset), [], NOW, tz=UTC, sleep=SLEEP)
    assert pace.status is PaceStatus.OVER
    assert pace.exhausts_at is not None
    expected = datetime(2026, 10, 26, 11, 0, 0, tzinfo=UTC)
    assert abs((pace.exhausts_at - expected).total_seconds()) < 1


# --- 睡眠中にリセットを迎える --------------------------------------------------


def test_reset_during_sleep_projects_used():
    # now が 01:00 (睡眠中)、リセットは 04:00 (まだ睡眠中): 起きている残り 0。
    # projected = used、exhausts_at なし、sustainable も 0 除算を避けて None。
    now = datetime(2026, 10, 26, 1, 0, 0, tzinfo=UTC)
    pace = compute_pace(grok_weekly(40.0), [], now, tz=UTC, sleep=SLEEP)
    assert pace.projected_end_percent == pytest.approx(40.0)
    assert pace.exhausts_at is None
    assert pace.sustainable_per_day is None
    assert pace.status is PaceStatus.SPARE
    assert pace.rate_per_day == pytest.approx(40 * 24 / 112)  # 起きている経過 112h


# --- sleep=None は従来どおり ---------------------------------------------------


def test_sleep_none_keeps_wall_clock_values():
    pace = compute_pace(grok_weekly(40.0), [], NOW, tz=UTC, sleep=None)
    # 従来の計算: 経過 161h、残り 7h
    assert pace.elapsed_fraction == pytest.approx(161 / 168)
    assert pace.rate_per_day == pytest.approx(40 * 24 / 161)
    assert pace.projected_end_percent == pytest.approx(40 + (40 * 24 / 161) * (7 / 24))
    assert pace.exhausts_at is None
    assert pace.sustainable_per_day == pytest.approx(60 / (7 / 24))
    assert pace.status is PaceStatus.SPARE


def test_fresh_threshold_compares_active_times():
    # 窓が今朝 04:00 に始まり now 09:10: 壁時計の経過は 5h10m で FRESH 閾値
    # (0.03*168h = 5.04h) を超えるが、起きている経過は 1h10m (04:00-08:00 が睡眠)
    # なので FRESH のまま。sleep が無いと FRESH を抜ける。
    reset = datetime(2026, 11, 2, 4, 0, 0, tzinfo=UTC)
    start = reset - timedelta(days=7)
    now = start + timedelta(hours=5, minutes=10)
    slept = compute_pace(grok_weekly(2.0, reset), [], now, tz=UTC, sleep=SLEEP)
    assert slept.status is PaceStatus.FRESH
    assert slept.elapsed_fraction == pytest.approx((7 / 6) / 112)  # 起きている 1h10m / 112h
    wall = compute_pace(grok_weekly(2.0, reset), [], now, tz=UTC)
    assert wall.status is not PaceStatus.FRESH


def test_crossing_midnight_sleep_band():
    # start > end (22:00-06:00) の日付またぎ帯。now 21:00、リセットは翌 13:00。
    # used 1040/11%: rate 21.8/日 -> 残りは起きている 6h で尽きる。
    # 21:00-22:00 の 1h + 帯 (22:00-06:00) を飛ばして 06:00 から 5h -> 10-26 11:00。
    # 壁時計なら 21:00 + 6h = 10-26 03:00 (まだ睡眠中) になる。
    sleep = (time(22, 0), time(6, 0))
    reset = datetime(2026, 10, 26, 13, 0, 0, tzinfo=UTC)
    pace = compute_pace(grok_weekly(1040 / 11, reset), [], NOW, tz=UTC, sleep=sleep)
    assert pace.elapsed_fraction == pytest.approx(104 / 112)  # 壁時計 152h - 睡眠 48h
    assert pace.rate_per_day == pytest.approx((1040 / 11) * 24 / 104)
    assert pace.status is PaceStatus.OVER
    assert pace.exhausts_at is not None
    expected = datetime(2026, 10, 26, 11, 0, 0, tzinfo=UTC)
    assert abs((pace.exhausts_at - expected).total_seconds()) < 1


# --- config のパース -----------------------------------------------------------


def test_load_sleep_crossing_midnight_and_skip(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[pacing.sleep]\nstart = "23:30"\nend = "06:00"\nskip = ["glm", "grok"]\n',
        encoding="utf-8",
    )
    sleep = load_config(p).pacing.sleep
    assert sleep == Sleep(time(23, 30), time(6, 0), ["glm", "grok"])
    # sleep_for: skip に入れた provider は None、他は (start, end)
    assert sleep_for(sleep, "glm") is None
    assert sleep_for(sleep, "claude") == (time(23, 30), time(6, 0))


def test_sleep_defaults_to_off(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.pacing.sleep is None
    assert sleep_for(None, "claude") is None
    assert sleep_for(cfg.pacing.sleep, "grok") is None
    assert config_to_dict(cfg)["pacing"] == {"tight": 92.0, "on_track": 70.0}


def test_load_sleep_same_day_band(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pacing.sleep]\nstart = "01:00"\nend = "07:00"\n', encoding="utf-8")
    assert load_config(p).pacing.sleep == Sleep(time(1, 0), time(7, 0), [])


def test_sleep_start_equals_end_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pacing.sleep]\nstart = "01:00"\nend = "01:00"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "pacing.sleep" in str(ei.value)
    with pytest.raises(ConfigError):
        config_from_dict({"pacing": {"sleep": {"start": "01:00", "end": "01:00"}}})


def test_sleep_unknown_key_is_ignored_with_warning(tmp_path, caplog):
    p = tmp_path / "config.toml"
    p.write_text('[pacing.sleep]\nstart = "00:00"\nend = "08:00"\nnapp = true\n',
                 encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        sleep = load_config(p).pacing.sleep
    assert sleep == Sleep(time(0, 0), time(8, 0), [])
    assert any("pacing.sleep.napp" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "text",
    [
        '[pacing.sleep]\nstart = "25:00"\nend = "08:00"',   # 不正な時刻
        '[pacing.sleep]\nstart = "00:00"\nend = "08:00"\nskip = "glm"',  # 配列でない
        '[pacing.sleep]\nstart = "00:00"\nend = "08:00"\nskip = [1]',
        '[pacing.sleep]\nstart = "00:00"\nend = "08:00"\nskip = ["chatgpt"]',  # 未知の provider
        '[pacing.sleep]\nstart = "00:00"\nend = "08:00"\nskip = [""]',
        '[pacing]\nsleep = 3',  # テーブルでない
    ],
)
def test_sleep_invalid_values_raise(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_sleep_roundtrips_through_toml_and_dict(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[pacing]\ntight = 85\n[pacing.sleep]\nstart = "00:00"\nend = "08:00"\n'
        'skip = ["glm"]\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    parsed = tomllib.loads(format_toml(cfg))  # format_toml は load_config で読み戻せる
    assert parsed["pacing"]["sleep"] == {"start": "00:00", "end": "08:00",
                                         "skip": ["glm"], "advice": "reset"}
    d = config_to_dict(cfg)
    assert d["pacing"]["sleep"] == {"start": "00:00", "end": "08:00",
                                    "skip": ["glm"], "advice": "reset"}
    assert config_to_dict(config_from_dict(d)) == d  # dict → Config → dict で不変


def test_format_toml_without_sleep_keeps_commented_example(tmp_path):
    text = format_toml(load_config(tmp_path / "none.toml"))
    assert "\n[pacing.sleep]" not in text  # 実テーブルは無い
    assert "# [pacing.sleep]" in text
    parsed = tomllib.loads(text)
    assert "sleep" not in parsed["pacing"]


# --- rate の分母ガード ---------------------------------------------------------


def test_burst_rate_needs_at_least_one_active_hour():
    # 基準 23:50・now 02:00 (睡眠 00:00-08:00): 壁計算の span は 2h10m だが
    # 起きているのは 23:50-00:00 の 10 分だけ。分母が縮んで rate が爆発する
    # ので burst は None。sleep が無いと壁時計 2h10m で計算される。
    now = datetime(2026, 10, 26, 2, 0, 0, tzinfo=UTC)
    base = samp(now - timedelta(hours=2, minutes=10), 37.0)
    slept = compute_pace(grok_weekly(40.0), [base], now, tz=UTC, sleep=SLEEP)
    assert slept.burst_rate_per_day is None
    assert slept.burst_exhausts_at is None
    wall = compute_pace(grok_weekly(40.0), [base], now, tz=UTC)
    assert wall.burst_rate_per_day == pytest.approx(3 * 144 / 13)  # 3% / 2h10m


def test_recent_rate_skipped_when_active_span_is_short():
    # 基準 01:00・now 17:00: 壁計算の span は 16h (lookback 24h の半分以上)だが
    # 起きているのは 9h (01:00-08:00 が睡眠) で半分未満 -> recent rate なし。
    # rate は窓全体の平均 (起きている 105h で割ったもの) だけになる。
    now = datetime(2026, 10, 25, 17, 0, 0, tzinfo=UTC)
    base = samp(now - timedelta(hours=16), 10.0)
    slept = compute_pace(grok_weekly(40.0), [base], now, tz=UTC, sleep=SLEEP)
    assert slept.rate_per_day == pytest.approx(40 * 24 / 105)
    wall = compute_pace(grok_weekly(40.0), [base], now, tz=UTC)
    assert wall.rate_per_day == pytest.approx(0.5 * (40 * 24 / 157) + 0.5 * (30 * 24 / 16))


def test_recent_rate_lookback_clamped_by_wall_elapsed():
    # 窓が今朝 04:00 に始まり now 21:00 (壁計算の経過 17h、起きている 13h)。
    # lookback は起きている経過ではなく壁時計の 17h で clamp するので、
    # カットオフは 04:00: 05:00 のサンプルは「now より前だがカットオフ後」
    # になり基準に選ばれる (古い実装では 07:00 のサンプルが基準だった)。
    now = datetime(2026, 10, 25, 21, 0, 0, tzinfo=UTC)
    reset = datetime(2026, 11, 1, 4, 0, 0, tzinfo=UTC)
    win = Window("claude", "weekly", "週間", 40.0, reset, timedelta(days=7))
    samples = [
        Sample("claude:weekly", datetime(2026, 10, 25, 5, 0, tzinfo=UTC), 5.0, reset),
        Sample("claude:weekly", datetime(2026, 10, 25, 7, 0, tzinfo=UTC), 10.0, reset),
    ]
    pace = compute_pace(win, samples, now, tz=UTC, sleep=SLEEP)
    # rate = 0.5 * rate_win(40*24/13) + 0.5 * recent(35*24/13): 基準は 05:00
    assert pace.rate_per_day == pytest.approx(75 * 12 / 13)


# --- config: start / end は必須 -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[pacing.sleep]",                                   # 空テーブル
        '[pacing.sleep]\nskip = ["glm"]',                   # skip だけ
        '[pacing.sleep]\nend = "08:00"',                    # start がない
        '[pacing.sleep]\nstart = "00:00"',                  # end がない
    ],
)
def test_sleep_requires_both_start_and_end(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text + "\n", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "pacing.sleep.start" in str(ei.value) \
        or "pacing.sleep.end" in str(ei.value)


def test_sleep_requires_both_keys_via_set_json():
    with pytest.raises(ConfigError):
        config_from_dict({"pacing": {"sleep": {"start": "00:00"}}})
    with pytest.raises(ConfigError):
        config_from_dict({"pacing": {"sleep": {"end": "08:00", "skip": []}}})
    with pytest.raises(ConfigError):
        config_from_dict({"pacing": {"sleep": {}}})


# --- render: 経過表示は壁時計 ---------------------------------------------------


def test_render_elapsed_line_uses_wall_clock():
    from quotacanvas import render as render_mod
    from quotacanvas.i18n import t

    pace = compute_pace(grok_weekly(91.0), [], NOW, tz=UTC, sleep=SLEEP)
    assert pace.elapsed_fraction == pytest.approx(109 / 112)  # 起きている時間ベース
    # 表示は壁時計の経過 (161h)。「1時間経過」表示が縮まないことを確認する。
    assert render_mod._elapsed_line(pace, NOW) == t(
        "render.elapsed.of",
        e=render_mod._fmt_span(NOW - START),
        total=render_mod._fmt_span(timedelta(days=7)),
    )
    # リセット不明の従来どおり: 経過 0 の表示のまま
    unknown = compute_pace(
        Window("grok", "weekly", "週間", 40.0, None, timedelta(days=7)), [], NOW, tz=UTC)
    assert render_mod._elapsed_line(unknown, NOW) == t(
        "render.elapsed.of",
        e=render_mod._fmt_span(timedelta(0)),
        total=render_mod._fmt_span(timedelta(days=7)),
    )


# --- 配線: build_cards / recommend ---------------------------------------------


def test_build_cards_applies_sleep_except_skipped_provider(tmp_path, utc_local_tz):
    from quotacanvas import cli

    midnight = datetime(2026, 10, 27, 0, 0, 0, tzinfo=UTC)
    claude = Window("claude", "weekly", "週間", 77.0, midnight, timedelta(days=7))
    glm = Window("glm", "weekly", "週間", 77.0, midnight, timedelta(days=7))
    usages = [
        ProviderUsage("claude", "Claude Max", [claude], None, NOW),
        ProviderUsage("glm", "GLM", [glm], None, NOW),
    ]
    history = History(tmp_path / "history.jsonl")
    sleep = Sleep(time(0, 0), time(8, 0), ["glm"])
    cards = cli.build_cards(usages, history, NOW, sleep=sleep)
    by_provider = {u.provider: pace for u, pace in cards}
    assert by_provider["claude"].status is PaceStatus.TIGHT      # 睡眠を適用
    assert by_provider["glm"].status is PaceStatus.ON_TRACK      # skip で壁時計


def test_recommend_applies_sleep(tmp_path, utc_local_tz):
    # リセット 0 時 (= 実質の締め切り) の週間 used 83.5%: 壁時計では 0 時の
    # 43 分前に尽きるだけなので tight で使えるが、睡眠を適用すると over になる。
    midnight = datetime(2026, 10, 27, 0, 0, 0, tzinfo=UTC)
    win = Window("claude", "weekly", "週間", 83.5, midnight, timedelta(days=7))
    usages = [ProviderUsage("claude", "Claude Max", [win], None, NOW)]
    history = History(tmp_path / "history.jsonl")
    rec = recommend(usages, history, NOW)  # sleep なし: 従来どおり使える
    assert rec is not None and rec.ranking[0]["usable"] is True
    # 睡眠あり: 0 時 (睡眠中) より前に尽きる (over) ので使えない
    rec = recommend(usages, history, NOW, sleep=Sleep(time(0, 0), time(8, 0), []))
    assert rec is None
    # skip に入れていると壁時計の判定に戻るので使える
    rec = recommend(usages, history, NOW, sleep=Sleep(time(0, 0), time(8, 0), ["claude"]))
    assert rec is not None and rec.provider == "claude"


# --- advice = "bedtime": Pace.bedtime_at の計算 ---------------------------------


def test_bedtime_at_is_next_sleep_start_before_reset():
    # 例のケース: now 21:00、次に寝る 00:00 がリセット (04:00) より先 → 締切は 00:00
    win = grok_weekly(77.0)
    pace = compute_pace(win, [], NOW, tz=UTC, sleep=SLEEP, bedtime=True)
    assert pace.bedtime_at == datetime(2026, 10, 26, 0, 0, 0, tzinfo=UTC)
    # 判定そのものは変わらない (bedtime_at は表示側の締切差し替え用)
    plain = compute_pace(win, [], NOW, tz=UTC, sleep=SLEEP)
    assert pace.status is plain.status
    assert pace.projected_end_percent == plain.projected_end_percent


def test_bedtime_at_none_without_flag_or_sleep():
    win = grok_weekly(77.0)
    # advice = "reset" (旗なし) と sleep なしでは従来どおり None
    assert compute_pace(win, [], NOW, tz=UTC, sleep=SLEEP).bedtime_at is None
    assert compute_pace(win, [], NOW, tz=UTC, bedtime=True).bedtime_at is None


def test_bedtime_at_none_when_currently_sleeping():
    # 寝ている最中 (01:00): 締切 (01:00) までの起きている時間が 0 なので
    # 従来どおりリセット基準の表示にする (None)
    pace = compute_pace(grok_weekly(40.0), [], datetime(2026, 10, 26, 1, 0, tzinfo=UTC),
                        tz=UTC, sleep=SLEEP, bedtime=True)
    assert pace.bedtime_at is None


def test_bedtime_at_none_when_reset_is_after_waking():
    # 週間枠のリセットが数日先 (起きている時間): 今夜寝ても起きた後にまだ
    # 使えるので、締切はリセットのまま (「寝るまで」を出さない)
    resets = datetime(2026, 10, 29, 13, 0, 0, tzinfo=UTC)
    win = Window("claude", "weekly", "週間", 39.0, resets, timedelta(days=7))
    pace = compute_pace(win, [], NOW, tz=UTC, sleep=SLEEP, bedtime=True)
    assert pace.bedtime_at is None
    # 起きた直後 (09:00) のリセットも同じ
    resets = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)
    win = Window("grok", "weekly", "週間", 77.0, resets, timedelta(days=7))
    pace = compute_pace(win, [], NOW, tz=UTC, sleep=SLEEP, bedtime=True)
    assert pace.bedtime_at is None


def test_bedtime_at_none_when_reset_comes_first():
    # 次に寝る 00:00 よりリセット (22:00) が先 → 締切はリセットのまま
    resets = datetime(2026, 10, 25, 22, 0, 0, tzinfo=UTC)
    win = Window("grok", "5h", "5時間", 10.0, resets, timedelta(hours=5))
    pace = compute_pace(win, [], NOW, tz=UTC, sleep=SLEEP, bedtime=True)
    assert pace.bedtime_at is None


# --- advice = "bedtime": advice_text の表示 -------------------------------------

_BEDTIME = datetime(2026, 10, 26, 0, 0, 0, tzinfo=UTC)


def _bedtime_pace(bedtime: bool, used: float = 77.0):
    from quotacanvas.render import advice_text  # noqa: F401 (呼び出し側で使う)
    return compute_pace(grok_weekly(used), [], NOW, tz=UTC, sleep=SLEEP,
                        bedtime=bedtime)


def test_advice_text_bedtime_display():
    from quotacanvas.render import advice_text
    pace = _bedtime_pace(True)
    assert pace.status is PaceStatus.ON_TRACK
    # 大きいカード: 「リセットまで7時間」が「寝るまで3時間」に変わる
    assert advice_text(pace, now=NOW) == "順調 · 寝るまで3時間、残り23%は使い切ってよい"
    # 小さいカードも同じく
    assert advice_text(pace, compact=True, now=NOW) == "順調 · 寝るまで3時間"


def test_advice_text_reset_display_without_bedtime():
    from quotacanvas.render import advice_text
    pace = _bedtime_pace(False)  # advice = "reset" (既定)
    assert advice_text(pace, now=NOW) == "順調 · リセットまで7時間、残り23%は使い切ってよい"
    assert advice_text(pace, compact=True, now=NOW) == "順調 · リセットまで7時間"


def test_advice_text_exhausted_keeps_reset_wording():
    # EXHAUSTED の「リセットまで」は advice = "bedtime" でも変えない
    from quotacanvas.render import advice_text
    pace = _bedtime_pace(True, used=100.0)
    assert pace.status is PaceStatus.EXHAUSTED
    assert pace.bedtime_at == _BEDTIME
    text = advice_text(pace, now=NOW)
    assert text == "上限到達 · リセットまで あと 7時間"
    assert "寝るまで" not in text


# --- 1日あたりの許容量の表示 (暦の 1 日あたり) ---------------------------------

_ALLOW_NOW = datetime(2026, 10, 26, 14, 4, 0, tzinfo=UTC)
_ALLOW_RESET = datetime(2026, 10, 29, 22, 0, 0, tzinfo=UTC)   # 3日と7時間56分先


def _allowance_pace(sleep):
    """used 43% の Claude 週間。リセットまでの壁時計は 3.3306 日、睡眠
    00:00-08:00 が 3 夜ぶん (24h) あるので起きている時間は 2.3306 日。"""
    win = Window("claude", "weekly", "週間", 43.0, _ALLOW_RESET,
                timedelta(days=7))
    return compute_pace(win, [], _ALLOW_NOW, tz=UTC, sleep=sleep)


def test_daily_allowance_display_uses_calendar_days_with_sleep():
    """[pacing.sleep] 設定時の「1日あたり」の許容量は暦の 1 日あたり。

    起きている時間基準の sustainable_per_day は 57% / 2.33日 = 24.6%/日
    (従来の表示「1日24%」) だが、睡眠中に使わない分は起きている時間に振られる
    ので、1日の許容量としては 57% / 3.33日 = 17.1%/日 が正しい。"""
    from quotacanvas.render import advice_text
    pace = _allowance_pace(SLEEP)
    assert pace.status is PaceStatus.ON_TRACK
    # sustainable は起きている時間基準のまま (adjust の比較に使う。変わらない)
    assert pace.sustainable_per_day == pytest.approx(57.0 / ((55 + 56 / 60) / 24))
    assert pace.allowance_per_day == pytest.approx(57.0 / (3 + (7 + 56 / 60) / 24))
    assert advice_text(pace, compact=True, now=_ALLOW_NOW) == "順調 · 1日 約17%まで"
    assert advice_text(pace, now=_ALLOW_NOW) == "順調 · 1日17%ペースで最後まで使える"


def test_daily_allowance_display_without_sleep_keeps_sustainable():
    """sleep 未設定なら壁時計 == 起きている時間なので、表示は従来どおり
    sustainable_per_day と同じ値 (上と同じ窓で 17%/日)。"""
    from quotacanvas.render import advice_text
    pace = _allowance_pace(None)
    assert pace.status is PaceStatus.ON_TRACK
    assert pace.allowance_per_day == pytest.approx(pace.sustainable_per_day)
    assert pace.allowance_per_day == pytest.approx(57.0 / (3 + (7 + 56 / 60) / 24))
    assert advice_text(pace, compact=True, now=_ALLOW_NOW) == "順調 · 1日 約17%まで"
    assert advice_text(pace, now=_ALLOW_NOW) == "順調 · 1日17%ペースで最後まで使える"


# --- advice の設定読み込み -------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        'advice = "night"',    # ありがちな間違い
        'advice = "auto"',
        'advice = ""',
        'advice = "BEDTIME"',  # 大文字は別の値
        'advice = 3',          # 文字列でない
    ],
)
def test_sleep_advice_invalid_raises(tmp_path, line):
    p = tmp_path / "config.toml"
    p.write_text('[pacing.sleep]\nstart = "00:00"\nend = "08:00"\n' + line + "\n",
                 encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "pacing.sleep.advice" in str(ei.value)


def test_sleep_advice_invalid_via_set_json():
    with pytest.raises(ConfigError):
        config_from_dict({"pacing": {"sleep": {"start": "00:00", "end": "08:00",
                                               "advice": "night"}}})


def test_sleep_advice_bedtime_loads_and_roundtrips(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pacing.sleep]\nstart = "00:00"\nend = "08:00"\nadvice = "bedtime"\n',
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.pacing.sleep.advice == "bedtime"
    parsed = tomllib.loads(format_toml(cfg))  # format_toml は load_config で読み戻せる
    assert parsed["pacing"]["sleep"]["advice"] == "bedtime"
    d = config_to_dict(cfg)
    assert d["pacing"]["sleep"] == {"start": "00:00", "end": "08:00", "skip": [],
                                    "advice": "bedtime"}
    assert config_to_dict(config_from_dict(d)) == d


def test_sleep_advice_default_is_reset(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[pacing.sleep]\nstart = "00:00"\nend = "08:00"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.pacing.sleep.advice == "reset"
    assert tomllib.loads(format_toml(cfg))["pacing"]["sleep"]["advice"] == "reset"
    assert config_to_dict(cfg)["pacing"]["sleep"]["advice"] == "reset"


# --- 配線: build_cards が bedtime_at を載せる ------------------------------------


def test_build_cards_carries_bedtime_at(tmp_path, utc_local_tz):
    from quotacanvas import cli

    four_am = datetime(2026, 10, 26, 4, 0, 0, tzinfo=UTC)  # 睡眠中にリセット
    claude = Window("claude", "weekly", "週間", 77.0, four_am, timedelta(days=7))
    glm = Window("glm", "weekly", "週間", 77.0, four_am, timedelta(days=7))
    usages = [
        ProviderUsage("claude", "Claude Max", [claude], None, NOW),
        ProviderUsage("glm", "GLM", [glm], None, NOW),
    ]
    history = History(tmp_path / "history.jsonl")
    sleep = Sleep(time(0, 0), time(8, 0), ["glm"], advice="bedtime")
    cards = cli.build_cards(usages, history, NOW, sleep=sleep)
    by_provider = {u.provider: pace for u, pace in cards}
    assert by_provider["claude"].bedtime_at == _BEDTIME
    assert by_provider["glm"].bedtime_at is None  # skip で従来どおり
    # advice = "reset" (既定) では載らない
    cards = cli.build_cards(usages, history, NOW,
                            sleep=Sleep(time(0, 0), time(8, 0), [], advice="reset"))
    by_provider = {u.provider: pace for u, pace in cards}
    assert by_provider["claude"].bedtime_at is None
