"""advice_text() の last_unit 分岐と 1 日未満の窓の文言 (render.py) のテスト。

- リセットまでの残り時間が 1 単位 (5 時間枠など 1 日未満の枠なら 1 時間、
  1 日以上の枠なら 1 日) を切るときは、1 単位あたりの許容量
  (「1時間あたり 244%」など、残り時間より大きく意味をなさない数字) を
  出さず、リセットまでの残り時間を伝える。
- 1 日未満の窓では 1 単位あたりの許容量をやめて、残量と締切までの時間を
  そのまま出す (「余裕あり · 残り91% · あと4時間」)。1 日以上の窓 (週間
  など) は 1 日単位の許容量のまま変わらない。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas.models import Pace, PaceStatus, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import advice_text

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)


def _pace(used, resets_in, length):
    win = Window("codex", "5h", "5時間", used, NOW + resets_in, length)
    return compute_pace(win, [], NOW)


# --- 5時間枠・リセットまで18分 (1時間未満 = last_unit) ----------------------


def test_5h_last_unit_spare_compact():
    pace = _pace(25.0, timedelta(minutes=18), timedelta(hours=5))
    assert pace.status is PaceStatus.SPARE
    text = advice_text(pace, compact=True, now=NOW)
    assert text == "余裕あり · このままだと約73%余る · リセットまで18分"


def test_5h_last_unit_spare_normal():
    pace = _pace(25.0, timedelta(minutes=18), timedelta(hours=5))
    text = advice_text(pace, compact=False, now=NOW)
    assert text == "余裕あり · このままだと約73%余る · リセットまで18分、残り75%は自由に使える"


def test_5h_last_unit_never_shows_per_hour_allowance():
    # used 25%・18分残しでは「1時間あたり 244%」のような許容量は意味をなさない
    pace = _pace(25.0, timedelta(minutes=18), timedelta(hours=5))
    for compact in (True, False):
        text = advice_text(pace, compact, NOW)
        assert "244" not in text
        assert "1時間あたり" not in text
        assert "リセットまで18分" in text


# --- 5時間枠・リセットまで1時間以上: 残量と時間をそのまま出す新仕様 ----------


def test_5h_spare_shows_rest_and_time_to_reset():
    # used 9%・リセットまで4時間: 許容量ではなく「残り91% · あと4時間」
    pace = _pace(9.0, timedelta(hours=4), timedelta(hours=5))
    assert pace.status is PaceStatus.SPARE
    assert advice_text(pace, compact=True, now=NOW) == \
        "余裕あり · このままだと約55%余る · 残り91% · あと4時間"
    assert advice_text(pace, compact=False, now=NOW) == \
        "余裕あり · このままだと約55%余る · 残り91% · あと4時間"


def test_5h_over_full_keeps_slow_down_head():
    # full (compact=False) の OVER は頭が「ペースを約◯%落とす」、compact はバッジ
    pace = _pace(95.0, timedelta(hours=4), timedelta(hours=5))
    assert pace.status is PaceStatus.OVER
    assert advice_text(pace, compact=True, now=NOW) == \
        "ペース超過 · 残り5% · あと4時間"
    assert advice_text(pace, compact=False, now=NOW) == \
        "ペースを約99%落とす · 残り5% · あと4時間"


def test_5h_tight_and_on_track_heads():
    tight = advice_text(_pace(19.5, timedelta(hours=4), timedelta(hours=5)),
                        compact=False, now=NOW)
    on_track = advice_text(_pace(19.0, timedelta(hours=4), timedelta(hours=5)),
                           compact=False, now=NOW)
    assert tight == "ほぼ使い切れる見込み · 残り81% · あと4時間"
    assert on_track == "順調 · 残り81% · あと4時間"


def test_5h_bedtime_deadline_shows_until_bed():
    # advice = "bedtime" で寝る時刻が締切のときは「あと」が「寝るまで」になる
    win = Window("codex", "5h", "5時間", 9.0, NOW + timedelta(hours=4),
                 timedelta(hours=5))
    pace = Pace(window=win, status=PaceStatus.SPARE, elapsed_fraction=0.2,
                ideal_used_percent=20.0, rate_per_day=None,
                projected_end_percent=None, exhausts_at=None,
                sustainable_per_day=None, adjust_percent=None, daily_used=[],
                bedtime_at=NOW + timedelta(hours=3))
    assert advice_text(pace, compact=True, now=NOW) == \
        "余裕あり · 残り91% · 寝るまで3時間"
    assert advice_text(pace, compact=False, now=NOW) == \
        "余裕あり · 残り91% · 寝るまで3時間"


# --- 1日以上の窓 (週間など) は 1日単位の許容量のまま変わらない ----------------


def test_weekly_keeps_per_day_allowance_wording():
    pace = _pace(27.0, timedelta(days=5), timedelta(days=7))
    assert pace.status is PaceStatus.ON_TRACK
    assert advice_text(pace, compact=True, now=NOW) == "順調 · 1日 約15%まで"
    assert advice_text(pace, compact=False, now=NOW) == \
        "順調 · 1日15%ペースで最後まで使える"
    spare = _pace(19.0, timedelta(days=5), timedelta(days=7))
    assert spare.status is PaceStatus.SPARE
    assert advice_text(spare, compact=True, now=NOW) == "余裕あり · このままだと約34%余る · 1日 約16%まで"
    assert advice_text(spare, compact=False, now=NOW) == \
        "余裕あり · このままだと約34%余る · 1日 約16%まで使える"


# --- 週間枠・リセットまで23時間 (1日未満 = last_unit) -----------------------


def test_weekly_last_unit_compact():
    pace = _pace(19.0, timedelta(hours=23), timedelta(days=7))
    assert pace.status is PaceStatus.SPARE
    text = advice_text(pace, compact=True, now=NOW)
    assert text == "余裕あり · このままだと約78%余る · リセットまで23時間"


def test_weekly_last_unit_normal():
    pace = _pace(19.0, timedelta(hours=23), timedelta(days=7))
    text = advice_text(pace, compact=False, now=NOW)
    assert text == "余裕あり · このままだと約78%余る · リセットまで23時間、残り81%は自由に使える"


def test_weekly_last_unit_never_shows_per_day_allowance():
    # 残り 23時間 (< 1日) で「1日 約○○%まで」は意味をなさない
    pace = _pace(19.0, timedelta(hours=23), timedelta(days=7))
    for compact in (True, False):
        text = advice_text(pace, compact, NOW)
        assert "1日" not in text
        assert "リセットまで23時間" in text


# --- last_unit での状態ごとの文言 (通常サイズ) ------------------------------


@pytest.mark.parametrize("used,compact_expected,normal_expected", [
    (97.0, "ペース超過 · リセットまで18分",
     "ペースを落とす · リセットまでの18分は残り3%で"),
    (93.5, "適正ペース · リセットまで18分",
     "ほぼ使い切れる見込み · リセットまで18分"),
    (92.0, "順調 · リセットまで18分",
     "順調 · リセットまで18分、残り8%は使い切ってよい"),
], ids=["over", "tight", "on_track"])
def test_5h_last_unit_wording_per_status(used, compact_expected, normal_expected):
    pace = _pace(used, timedelta(minutes=18), timedelta(hours=5))
    assert pace.status is not PaceStatus.SPARE
    assert advice_text(pace, compact=True, now=NOW) == compact_expected
    assert advice_text(pace, compact=False, now=NOW) == normal_expected


def test_spare_forecast_in_english():
    from quotacanvas import i18n
    old = i18n.current_language()
    try:
        i18n.set_language("en")
        pace = _pace(19.0, timedelta(days=5), timedelta(days=7))
        assert advice_text(pace, now=NOW) == "Spare · about 34% will remain at this pace · up to 16%/day"
    finally:
        i18n.set_language(old)
