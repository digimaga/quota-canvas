"""小パネル (size="small") のリセット行 (残り時間 + リセット権) の描画テスト。

リセット行がゲージ・日次目盛り・アドバイス行と重ならず、それぞれ 2px 以上の
余白を保つことを、実際に描画して各テキストの bbox (ImageDraw.textbbox) から
検査する。ImageDraw.text を差し替えて呼び出し座標を集める。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image, ImageDraw

from quotacanvas.models import ProviderUsage, ResetCredit, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import (AREA_BOTTOM, AREA_TOP, CANVAS_W, GAP, MARGIN,
                                PanelSpec, _S, _draw_small_panel, render)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)
MARGIN_PX = 2          # 描画側と同じ「最低余白」


@pytest.fixture()
def text_calls(monkeypatch):
    """ImageDraw.text を記録に差し替える。→ [(draw, xy, text, font, anchor)]"""
    calls = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, **kw):
        calls.append((self, tuple(xy), text, kw.get("font"), kw.get("anchor")))
        return orig(self, xy, text, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return calls


def _codex_card(with_credits=True):
    # tests/test_codex_credits.py の test_render_panels_with_credits_smoke と
    # 同じ形の Codex カード
    win = Window("codex", "weekly", "週間", 10.0,
                 NOW + timedelta(days=5), timedelta(days=7))
    credits = [ResetCredit("Full reset", NOW - timedelta(days=1),
                           NOW + timedelta(hours=20)),
               ResetCredit("Full reset", NOW - timedelta(days=2),
                           NOW + timedelta(days=5))] if with_credits else []
    pu = ProviderUsage("codex", "Codex Pro", [win], None, NOW,
                       reset_credits=credits)
    return [(pu, compute_pace(win, [], NOW))]


def _first_small_slot():
    """render() が small 1 枚のときに割り当てる上段スロットの box。"""
    h = (AREA_BOTTOM - AREA_TOP - GAP) / 2.0
    return (MARGIN, AREA_TOP, CANVAS_W - MARGIN, AREA_TOP + h)


def _panel_rows(text_calls, box):
    """パネル内に描かれた ls 行を上から順に (top, bot, text) で返す。"""
    x0, y0, x1, y1 = box
    rows = []
    for draw, xy, text, font, anchor in text_calls:
        if anchor != "ls" or not (x0 <= xy[0] <= x1):
            continue
        bb = draw.textbbox(xy, text, font=font, anchor=anchor)
        if bb[1] >= y0 - 1 and bb[3] <= y1 + 1:
            rows.append((bb[1], bb[3], text))
    rows.sort()
    return rows


def _assert_clear(rows, ticks_bot, adv_top, label):
    """リセット行が目盛りの下 2px・アドバイス行の上 2px を保つこと。"""
    for top, bot, text in rows:
        assert top >= ticks_bot + MARGIN_PX, \
            "%s が目盛りに重なる: %r top=%s" % (label, text, top)
        assert bot <= adv_top - MARGIN_PX, \
            "%s がアドバイス行に重なる: %r bot=%s" % (label, text, bot)
    for a, b in zip(rows, rows[1:]):      # 行同士も 2px 以上空ける
        assert b[0] - a[1] >= MARGIN_PX, "行間が狭い: %r / %r" % (a[2], b[2])


def test_small_panel_reset_rows_clear_of_gauge_ticks_and_advice(text_calls):
    """リセット権ありの Codex を small パネルに置く: 残り時間とリセット権の
    2 行とも描き、ゲージ/目盛り/アドバイス行に重ならない。"""
    render(_codex_card(), NOW.astimezone(),
           layout=[PanelSpec("codex:weekly", size="small")])
    _x0, y0, _x1, y1 = box = _first_small_slot()
    ticks_bot = y0 + 140 + 4              # ゲージ下端 + 日次目盛りの飛び出し

    rows = _panel_rows(text_calls, box)
    reset_rows = [(t, b, x) for t, b, x in rows if x.startswith("リセット")]
    assert any(x.startswith("リセット あと") for *_, x in reset_rows)
    assert any(x.startswith("リセット権") for *_, x in reset_rows)
    adv_top = rows[-1][0]                 # 一番下の ls 行 = アドバイス
    assert rows[-1][2].endswith("まで")   # アドバイス行であることの目印
    assert y1 - 8 - 17 <= rows[-1][1]     # baseline y1-8 前後から動いていない
    _assert_clear(reset_rows, ticks_bot, adv_top, "リセット行")


def test_small_panel_without_credits_keeps_countdown_clear(text_calls):
    """リセット権なしのときは残り時間 1 行のみ (従来どおり) で重ならない。"""
    render(_codex_card(with_credits=False), NOW.astimezone(),
           layout=[PanelSpec("codex:weekly", size="small")])
    _x0, y0, _x1, _y1 = box = _first_small_slot()

    rows = _panel_rows(text_calls, box)
    reset_rows = [(t, b, x) for t, b, x in rows if x.startswith("リセット")]
    assert len(reset_rows) == 1
    assert reset_rows[0][2].startswith("リセット あと")
    adv_top = rows[-1][0]
    _assert_clear(reset_rows, y0 + 144, adv_top, "リセット行")


def test_small_panel_short_box_drops_credit_row(text_calls):
    """縦の空きが 2 行分なければ 1 行 (残り時間のみ) に縮める。"""
    img = Image.new("RGB", (CANVAS_W, AREA_BOTTOM), (0, 0, 0))
    s = _S(img, (0, 0))
    pu, pace = _codex_card()[0]
    box = (MARGIN, 0, 420, 185)           # アドバイス上の空きが 1 行分のみ
    _draw_small_panel(s, box, "Codex Pro", (pu, pace), NOW.astimezone(),
                      "remaining", (200, 60, 30))

    rows = _panel_rows(text_calls, box)
    reset_rows = [(t, b, x) for t, b, x in rows if x.startswith("リセット")]
    assert [x for _t, _b, x in reset_rows] and \
        all(x.startswith("リセット あと") for *_, x in reset_rows)
    adv_top = rows[-1][0]
    _assert_clear(reset_rows, 0 + 144, adv_top, "リセット行")


def test_small_panel_too_short_box_draws_no_reset_row(text_calls):
    """1 行も入らない高さならリセット行は何も描かない。"""
    img = Image.new("RGB", (CANVAS_W, AREA_BOTTOM), (0, 0, 0))
    s = _S(img, (0, 0))
    pu, pace = _codex_card()[0]
    box = (MARGIN, 0, 420, 140)           # ゲージ (〜144px) がはみ出る高さ
    _draw_small_panel(s, box, "Codex Pro", (pu, pace), NOW.astimezone(),
                      "remaining", (200, 60, 30))

    rows = _panel_rows(text_calls, box)
    assert not [x for _t, _b, x in rows if x.startswith("リセット")]
