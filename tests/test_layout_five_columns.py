"""5 列 (列幅 約 365px) の描画で文字がカード内に収まり重ならないことの回帰テスト。

- ローカル LLM カードの「残り」と「待ち n」が重ならない (文字サイズを縮小)。
- large パネルの窓ラベルが残り幅に合わせて縮小・省略され、右端をはみ出さない。
4 列以下の見た目は tests/reference_*.png の比較テストで別途確認している。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from quotacanvas import render as render_mod
from quotacanvas.i18n import set_language
from quotacanvas.models import LocalSlots, ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import (AREA_BOTTOM, AREA_TOP, CANVAS_W, GAP, MARGIN,
                                PanelSpec, render)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)
NCOLS = 5


def _card_box(i):
    """render の col_box() と同じ 5 列のカード矩形。"""
    aw = CANVAS_W - 2 * MARGIN
    col_w = (aw - (NCOLS - 1) * GAP) / NCOLS
    x = float(MARGIN + i * (col_w + GAP))
    return (int(round(x)), AREA_TOP, int(round(x + col_w)), AREA_BOTTOM)


def _service_card(provider, label="週間", used=0.0):
    win = Window(provider, "weekly", label, used,
                 NOW + timedelta(days=5), timedelta(days=7))
    pu = ProviderUsage(provider, provider.title(), [win], None, NOW)
    return (pu, compute_pace(win, [], NOW))


def _capture_texts(monkeypatch):
    """_S.text を包んで (表示文言, xy, font, anchor) を記録する。"""
    records = []
    orig = render_mod._S.text

    def spy(self, xy, txt, font, fill, anchor=None):
        records.append((self._t(txt), tuple(xy), font, anchor))
        orig(self, xy, txt, font, fill, anchor)

    monkeypatch.setattr(render_mod._S, "text", spy)
    return records


def _bboxes(records):
    d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    return [(txt, d.textbbox(xy, txt, font=font, anchor=anchor))
            for txt, xy, font, anchor in records]


def _in_card(records, col):
    """その列のカードに描かれた文字だけ (xy の x が列内)。"""
    x0, _y0, x1, _y1 = _card_box(col)
    return [r for r in records if x0 <= r[1][0] <= x1]


def test_local_card_texts_fit_and_do_not_overlap_in_five_columns(monkeypatch):
    monkeypatch.setattr("quotacanvas.i18n._current", "ja")
    records = _capture_texts(monkeypatch)
    local = ProviderUsage("local:test-server", "test-server", [], None, NOW,
                          slots=LocalSlots(0, 0, 16))
    cards = [(local, None)] + [_service_card("p%d" % i) for i in range(4)]
    layout = [PanelSpec(main="local:test-server")]
    layout += [PanelSpec(main="p%d:weekly" % i) for i in range(4)]
    img = render(cards, NOW, layout=layout)
    assert img.size == (1920, 462)

    x0, _y0, x1, _y1 = _card_box(0)
    texts = {txt: bbox for txt, bbox in _bboxes(_in_card(records, 0))}
    frac = texts["16/16"]
    free = texts["残り"]
    wait = texts["待ち 0"]
    for b in (frac, free, wait):
        assert b[0] >= x0 - 1 and b[2] <= x1 + 1, b
    # 「16/16」「残り」「待ち 0」が左から順に重ならない
    assert frac[2] <= free[0] + 0.5, (frac, free)
    assert free[2] <= wait[0] + 0.5, (free, wait)
    assert wait[2] <= x1 + 1


def test_large_panel_label_fits_in_five_columns(monkeypatch):
    monkeypatch.setattr("quotacanvas.i18n._current", "en")
    records = _capture_texts(monkeypatch)
    target = _service_card("claude", label="Weekly Fable 5.1")
    cards = [target] + [_service_card("p%d" % i) for i in range(4)]
    layout = [PanelSpec(main="claude:weekly")]
    layout += [PanelSpec(main="p%d:weekly" % i) for i in range(4)]
    img = render(cards, NOW, layout=layout)
    assert img.size == (1920, 462)

    x0, _y0, x1, _y1 = _card_box(0)
    bbs = _bboxes(_in_card(records, 0))
    word = next(b for txt, b in bbs if txt == "left")
    labels = [(txt, b) for txt, b in bbs if txt.startswith("·")]
    assert word[2] <= x1 + 1, word
    for txt, b in labels:
        # 省略された場合も、描かれた場合はカード右端の内側に収まる
        assert b[0] >= x0 - 1 and b[2] <= x1 + 1, (txt, b)
        assert word[2] <= b[0] + 0.5, (word, txt, b)
    # この列の文字はどれもカードの左右からはみ出さない
    for txt, b in bbs:
        assert b[0] >= x0 - 1 and b[2] <= x1 + 1, (txt, b)


def teardown_module():
    set_language("auto")
