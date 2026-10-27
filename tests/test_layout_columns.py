"""列の組み立て (render.group_columns) と auto_layout / 描画のテスト。

新しい仕様: small 2 段の列は右端に限らずどの位置にも置ける
(4 列すべて 2 段なら最大 8 枚)。main = "" の small は空き枠として
位置を保ち、解決できない large 列と両段空の small 列だけが消える。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quotacanvas.models import ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas import render as render_mod
from quotacanvas.render import (AREA_TOP, CANVAS_H, CANVAS_W, GAP, MARGIN,
                                PanelSpec, auto_layout, group_columns, render)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


def _spec(main, size="large"):
    return PanelSpec(main=main, size=size)


# ------------------------------------------------------------- group_columns

def test_group_columns_pairs_consecutive_smalls_anywhere():
    # [small,small,small,small,large,large] → 4 列で 6 枚 (左の列も 2 段にできる)
    specs = [_spec("a:1", "small"), _spec("a:2", "small"),
             _spec("b:1", "small"), _spec("b:2", "small"),
             _spec("c:1"), _spec("d:1")]
    columns = group_columns(specs)
    assert [[sp.main for sp in col] for col in columns] == [
        ["a:1", "a:2"], ["b:1", "b:2"], ["c:1"], ["d:1"]]


def test_group_columns_single_small_starts_new_column():
    # small のあとに large が挟まると、次の small は前の列に入らず新しい列 (上段)
    specs = [_spec("a:1"), _spec("a:2", "small"), _spec("b:1"),
             _spec("b:2", "small")]
    columns = group_columns(specs)
    assert [[sp.main for sp in col] for col in columns] == [
        ["a:1"], ["a:2"], ["b:1"], ["b:2"]]


def test_group_columns_odd_smalls_leave_last_bottom_empty():
    specs = [_spec("a:1", "small"), _spec("a:2", "small"), _spec("a:3", "small")]
    columns = group_columns(specs)
    assert [[sp.main for sp in col] for col in columns] == [["a:1", "a:2"], ["a:3"]]


# --------------------------------------------------------------- auto_layout

def _cards(n):
    """n サービスぶんの (ProviderUsage, Pace)。各サービスに窓 1 つ。"""
    cards = []
    for i in range(n):
        provider = "p%d" % i
        win = Window(provider, "weekly", "週間", 10.0,
                     NOW + timedelta(days=5), timedelta(days=7))
        pu = ProviderUsage(provider, provider.title(), [win], None, NOW)
        cards.append((pu, compute_pace(win, [], NOW)))
    return cards


def test_auto_layout_up_to_four_services_all_large():
    specs = auto_layout(_cards(4))
    assert [sp.size for sp in specs] == ["large"] * 4


def test_auto_layout_n5_to_n8_fill_four_columns():
    # 右側の列から 2 段にして 4 列に収める
    expected_large = {5: 3, 6: 2, 7: 1, 8: 0}
    for n, n_large in expected_large.items():
        specs = auto_layout(_cards(n))
        assert [sp.size for sp in specs] == \
            ["large"] * n_large + ["small"] * (n - n_large), n
        assert len(group_columns(specs)) == 4, n


def test_auto_layout_keeps_at_most_eight_services():
    specs = auto_layout(_cards(9))
    assert len(specs) == 8
    assert len(group_columns(specs)) == 4


def test_auto_layout_all_small_panels_have_no_subs():
    specs = auto_layout(_cards(6))
    assert all(sp.subs == [] for sp in specs if sp.size == "small")


def test_auto_layout_small_drops_subs_large_keeps_subs():
    # 窓を 2 つ持つサービス 5 つ: small になったパネルは subs を持たない
    # (small は subs を持たない契約)、large は残りの窓を subs に保つ
    cards = []
    for i in range(5):
        provider = "p%d" % i
        wins = [Window(provider, "weekly", "週間", 10.0,
                       NOW + timedelta(days=5), timedelta(days=7)),
                Window(provider, "5h", "5時間", 20.0,
                       NOW + timedelta(hours=4), timedelta(hours=5))]
        pu = ProviderUsage(provider, provider.title(), wins, None, NOW)
        cards.extend((pu, compute_pace(w, [], NOW)) for w in wins)
    specs = auto_layout(cards)
    assert [sp.size for sp in specs] == ["large"] * 3 + ["small"] * 2
    assert all(sp.subs == [] for sp in specs if sp.size == "small")
    assert all(sp.subs == ["%s:5h" % sp.main.split(":", 1)[0]]
               for sp in specs if sp.size == "large")


# -------------------------------------------------------------------- render

def test_render_left_stacked_columns_returns_canvas():
    # 左の列から 2 段: [small, small, small, small, large, large] → 4 列 6 枚
    cards = _cards(6)
    layout = [_spec("p0:weekly", "small"), _spec("p1:weekly", "small"),
              _spec("p2:weekly", "small"), _spec("p3:weekly", "small"),
              _spec("p4:weekly"), _spec("p5:weekly")]
    img = render(cards, NOW.astimezone(), layout=layout)
    assert img.size == (CANVAS_W, CANVAS_H) == (1920, 462)


def _spy_draw(monkeypatch, name):
    """render_mod の描画関数を記録に差し替える。→ [box, ...]"""
    boxes = []
    orig = getattr(render_mod, name)

    def spy(s, box, *args, **kwargs):
        boxes.append(tuple(box))
        orig(s, box, *args, **kwargs)

    monkeypatch.setattr(render_mod, name, spy)
    return boxes


def test_render_empty_top_slot_keeps_bottom_position(monkeypatch):
    # 上段が空き枠 (main = "") でも下段の small は上段に詰められない
    boxes = _spy_draw(monkeypatch, "_draw_small_panel")
    cards = _cards(1)
    layout = [_spec("", "small"), _spec("p0:weekly", "small")]
    img = render(cards, NOW.astimezone(), layout=layout)
    assert img.size == (1920, 462)
    assert len(boxes) == 1                 # 上段には何も描かれない
    x0, y0, x1, y1 = boxes[0]
    assert y0 > AREA_TOP + GAP             # 下段 (上段ぶん下がった位置)
    assert y1 <= CANVAS_H - 8              # 下端まで


def test_render_unresolvable_small_keeps_slot_position(monkeypatch):
    # 解決できない small は空き段として位置を保つ (前の段にも後の段にも入らない)
    top = _spy_draw(monkeypatch, "_draw_small_panel")
    cards = _cards(1)
    layout = [_spec("p0:weekly", "small"), _spec("nope:weekly", "small")]
    img = render(cards, NOW.astimezone(), layout=layout)
    assert img.size == (1920, 462)
    assert len(top) == 1
    assert top[0][1] == AREA_TOP           # 解決できる方は上段のまま


def test_render_unresolvable_large_column_disappears(monkeypatch):
    # 解決できない large 列は列ごと消え、残りの列が等分幅で詰まる (従来通り)
    boxes = _spy_draw(monkeypatch, "_draw_large_column")
    cards = _cards(1)
    layout = [_spec("nope:weekly"), _spec("p0:weekly")]
    img = render(cards, NOW.astimezone(), layout=layout)
    assert img.size == (1920, 462)
    assert len(boxes) == 1
    x0, _y0, x1, _y1 = boxes[0]            # 1 列ぶんだけが全幅で描かれる
    assert (x0, x1) == (MARGIN, CANVAS_W - MARGIN)


def test_render_fully_empty_small_column_disappears(monkeypatch):
    # small 列の 2 段とも空き (main="" と解決不能) ならその列も消える
    boxes = _spy_draw(monkeypatch, "_draw_large_column")
    cards = _cards(1)
    layout = [_spec("", "small"), _spec("nope:weekly", "small"),
              _spec("p0:weekly")]
    img = render(cards, NOW.astimezone(), layout=layout)
    assert img.size == (1920, 462)
    assert [(b[0], b[2]) for b in boxes] == [(MARGIN, CANVAS_W - MARGIN)]


def test_render_more_than_four_columns_is_capped(monkeypatch):
    # 直接 render を呼んで 5 列以上を渡しても列は最大 4 に抑えられる
    boxes = _spy_draw(monkeypatch, "_draw_large_column")
    cards = _cards(5)
    layout = [_spec("p%d:weekly" % i) for i in range(5)]
    img = render(cards, NOW.astimezone(), layout=layout)
    assert img.size == (1920, 462)
    assert len(boxes) == 4
