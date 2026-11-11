"""advice_text() の last_unit 分岐と 1 日未満の窓の文言 (render.py) のテスト。

- リセットまでの残り時間が 1 単位 (5 時間枠など 1 日未満の枠なら 1 時間、
  1 日以上の枠なら 1 日) を切るときは、1 単位あたりの許容量
  (「1時間あたり 244%」など、残り時間より大きく意味をなさない数字) を
  出さず、リセットまでの残り時間を伝える。
- 1 日未満の窓では 1 単位あたりの許容量をやめて、残量と締切までの時間を
  そのまま出す (「残り91% · あと4時間」)。1 日以上の窓 (週間
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
    assert text == "このままだと約73%余る · リセットまで18分"
    # 18分残しでは「1時間あたり 244%」のような許容量は意味をなさない
    assert "244" not in text
    assert "1時間あたり" not in text


def test_5h_last_unit_spare_normal():
    pace = _pace(25.0, timedelta(minutes=18), timedelta(hours=5))
    text = advice_text(pace, compact=False, now=NOW)
    assert text == "このままだと約73%余る · リセットまで18分、残り75%は自由に使える"
    # 18分残しでは「1時間あたり 244%」のような許容量は意味をなさない
    assert "244" not in text
    assert "1時間あたり" not in text


# --- 5時間枠・リセットまで1時間以上: 残量と時間をそのまま出す新仕様 ----------


def test_5h_spare_shows_rest_and_time_to_reset():
    # used 9%・リセットまで4時間: 許容量ではなく「残り91% · あと4時間」
    pace = _pace(9.0, timedelta(hours=4), timedelta(hours=5))
    assert pace.status is PaceStatus.SPARE
    assert advice_text(pace, compact=True, now=NOW) == \
        "このままだと約55%余る · 残り91% · あと4時間"
    assert advice_text(pace, compact=False, now=NOW) == \
        "このままだと約55%余る · 残り91% · あと4時間"


def test_5h_over_full_keeps_slow_down_head():
    # full (compact=False) の OVER は頭が「ペースを約◯%落とす」、compact はバッジと重なる「ペース超過」を付けない
    pace = _pace(95.0, timedelta(hours=4), timedelta(hours=5))
    assert pace.status is PaceStatus.OVER
    assert advice_text(pace, compact=True, now=NOW) == \
        "残り5% · あと4時間"
    assert advice_text(pace, compact=False, now=NOW) == \
        "ペースを約99%落とす · 残り5% · あと4時間"


def test_5h_tight_and_on_track_heads():
    tight = advice_text(_pace(19.5, timedelta(hours=4), timedelta(hours=5)),
                        compact=False, now=NOW)
    on_track = advice_text(_pace(19.0, timedelta(hours=4), timedelta(hours=5)),
                           compact=False, now=NOW)
    assert tight == "ほぼ使い切れる見込み · 残り81% · あと4時間"
    assert on_track == "残り81% · あと4時間"


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
        "残り91% · 寝るまで3時間"
    assert advice_text(pace, compact=False, now=NOW) == \
        "残り91% · 寝るまで3時間"


def test_5h_bedtime_over_keeps_heads():
    # bedtime でも順調系以外は状態語の頭が付く (short_left_bedtime キー)
    pace = _pace(95.0, timedelta(hours=4), timedelta(hours=5))
    assert pace.status is PaceStatus.OVER
    pace.bedtime_at = NOW + timedelta(hours=3)
    assert advice_text(pace, compact=True, now=NOW) == \
        "残り5% · 寝るまで3時間"
    assert advice_text(pace, compact=False, now=NOW) == \
        "ペースを約99%落とす · 残り5% · 寝るまで3時間"


def test_5h_bedtime_tight_keeps_heads():
    pace = _pace(19.5, timedelta(hours=4), timedelta(hours=5))
    assert pace.status is PaceStatus.TIGHT
    pace.bedtime_at = NOW + timedelta(hours=3)
    assert advice_text(pace, compact=True, now=NOW) == \
        "残り81% · 寝るまで3時間"
    assert advice_text(pace, compact=False, now=NOW) == \
        "ほぼ使い切れる見込み · 残り81% · 寝るまで3時間"


# --- 1日以上の窓 (週間など) は 1日単位の許容量のまま変わらない ----------------


def test_weekly_keeps_per_day_allowance_wording():
    pace = _pace(27.0, timedelta(days=5), timedelta(days=7))
    assert pace.status is PaceStatus.ON_TRACK
    assert advice_text(pace, compact=True, now=NOW) == "1日 約15%まで"
    assert advice_text(pace, compact=False, now=NOW) == \
        "1日15%ペースで最後まで使える"
    spare = _pace(19.0, timedelta(days=5), timedelta(days=7))
    assert spare.status is PaceStatus.SPARE
    assert advice_text(spare, compact=True, now=NOW) == "このままだと約34%余る · 1日 約16%まで"
    assert advice_text(spare, compact=False, now=NOW) == \
        "このままだと約34%余る · 1日 約16%まで使える"


# --- 週間枠・リセットまで23時間 (1日未満 = last_unit) -----------------------


def test_weekly_last_unit_compact():
    pace = _pace(19.0, timedelta(hours=23), timedelta(days=7))
    assert pace.status is PaceStatus.SPARE
    text = advice_text(pace, compact=True, now=NOW)
    assert text == "このままだと約78%余る · リセットまで23時間"
    # 残り 23時間 (< 1日) で「1日 約○○%まで」は意味をなさない
    assert "1日" not in text


def test_weekly_last_unit_normal():
    pace = _pace(19.0, timedelta(hours=23), timedelta(days=7))
    text = advice_text(pace, compact=False, now=NOW)
    assert text == "このままだと約78%余る · リセットまで23時間、残り81%は自由に使える"
    # 残り 23時間 (< 1日) で「1日 約○○%まで」は意味をなさない
    assert "1日" not in text


# --- last_unit での状態ごとの文言 (通常サイズ) ------------------------------


@pytest.mark.parametrize("used,compact_expected,normal_expected", [
    (97.0, "リセットまで18分",
     "ペースを落とす · リセットまでの18分は残り3%で"),
    (93.5, "リセットまで18分",
     "ほぼ使い切れる見込み · リセットまで18分"),
    (92.0, "リセットまで18分",
     "リセットまで18分、残り8%は使い切ってよい"),
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
        assert advice_text(pace, now=NOW) == "About 34% will remain at this pace · up to 16%/day"
    finally:
        i18n.set_language(old)


def test_weekly_over_compact_omits_badge_word():
    # ペース超過はカード右上のバッジに出るので、compact のアドバイス行には繰り返さない
    pace = _pace(90.0, timedelta(days=5), timedelta(days=7))
    assert pace.status is PaceStatus.OVER
    text = advice_text(pace, compact=True, now=NOW)
    assert "ペース超過" not in text
    assert text.startswith("1日 約")
    assert "ペース超過" not in advice_text(pace, compact=False, now=NOW)


@pytest.mark.parametrize("lang,expected_compact,expected_full", [
    ("ja", "ペースを落とす", "ペースを落とす · 残り期間は控えめに"),
    ("en", "Slow down", "Slow down · take it easy"),
])
def test_over_without_allowance_omits_badge_word(lang, expected_compact, expected_full):
    # 1日の許容量が出せない OVER (compact の tail なし・full の over_generic) でも
    # バッジと同じ「ペース超過」は出さず、空にもしない
    from quotacanvas import i18n
    win = Window("codex", "weekly", "週間", 90.0, NOW + timedelta(days=5),
                 timedelta(days=7))
    pace = Pace(window=win, status=PaceStatus.OVER, elapsed_fraction=0.3,
                ideal_used_percent=30.0, rate_per_day=None,
                projected_end_percent=None, exhausts_at=None,
                sustainable_per_day=None, adjust_percent=None, daily_used=[])
    old = i18n.current_language()
    i18n.set_language(lang)
    try:
        assert advice_text(pace, compact=True, now=NOW) == expected_compact
        assert advice_text(pace, compact=False, now=NOW) == expected_full
    finally:
        i18n.set_language(old)


@pytest.mark.parametrize("used,resets_in,length,expected", [
    (90.0, timedelta(days=5), timedelta(days=7), "Up to 2%/day"),
    (95.0, timedelta(hours=4), timedelta(hours=5), "5% left · in 4h"),
    (97.0, timedelta(minutes=18), timedelta(hours=5), "Resets in 18m"),
], ids=["per_day", "hourly", "last_unit"])
def test_over_compact_english_omits_badge_word(used, resets_in, length, expected):
    # 英語でも compact の OVER は「Over pace」を付けず、行頭を大文字にする
    from quotacanvas import i18n
    old = i18n.current_language()
    i18n.set_language("en")
    try:
        pace = _pace(used, resets_in, length)
        assert pace.status is PaceStatus.OVER
        assert advice_text(pace, compact=True, now=NOW) == expected
    finally:
        i18n.set_language(old)


@pytest.mark.parametrize("lang,used,resets_in,length,expected", [
    ("ja", 19.5, timedelta(hours=4), timedelta(hours=5), "残り81% · あと4時間"),
    ("ja", 93.5, timedelta(minutes=18), timedelta(hours=5), "リセットまで18分"),
    ("en", 19.5, timedelta(hours=4), timedelta(hours=5), "81% left · in 4h"),
    ("en", 93.5, timedelta(minutes=18), timedelta(hours=5), "Resets in 18m"),
    ("ja", 27.5, timedelta(days=5), timedelta(days=7), "1日 約15%まで"),
    ("en", 27.5, timedelta(days=5), timedelta(days=7), "Up to 15%/day"),
], ids=["ja_hourly", "ja_last_unit", "en_hourly", "en_last_unit",
        "ja_per_day", "en_per_day"])
def test_tight_compact_omits_badge_word(lang, used, resets_in, length, expected):
    # 適正ペースもカード右上のバッジに出るので、compact のアドバイス行には繰り返さない
    from quotacanvas import i18n
    old = i18n.current_language()
    i18n.set_language(lang)
    try:
        pace = _pace(used, resets_in, length)
        assert pace.status is PaceStatus.TIGHT
        assert advice_text(pace, compact=True, now=NOW) == expected
    finally:
        i18n.set_language(old)


@pytest.mark.parametrize("lang,expected", [
    ("ja", "ほぼ使い切れる見込み"), ("en", "On pace to use it all"),
])
def test_tight_compact_without_tail_uses_non_badge_wording(lang, expected):
    from quotacanvas import i18n
    win = Window("codex", "weekly", "週間", 30.0, NOW + timedelta(days=5),
                 timedelta(days=7))
    pace = Pace(window=win, status=PaceStatus.TIGHT, elapsed_fraction=0.3,
                ideal_used_percent=30.0, rate_per_day=None,
                projected_end_percent=None, exhausts_at=None,
                sustainable_per_day=None, adjust_percent=None, daily_used=[])
    old = i18n.current_language()
    i18n.set_language(lang)
    try:
        assert advice_text(pace, compact=True, now=NOW) == expected
    finally:
        i18n.set_language(old)
