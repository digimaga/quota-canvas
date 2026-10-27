"""内訳 (Window.part_of 付きの窓) の描画テスト。

- Grok の大パネル / 小パネルが gauge="remaining" / "used" の両方で例外なく
  描けること、サブ行に内訳行 (凡例の丸 + ラベル + 細いバー + 「20% 使用」)
  が出ること。
- parts を持たないカード (Claude など) の描画が parts 導入前と同じピクセル
  であること (tests/reference_noparts_*.png と比較)。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from quotacanvas.models import Pace, PaceStatus, ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import (_PART_COLORS, TRACK, PanelSpec, _lookups,
                                _mix, _paint_parts, _resolve_panel,
                                auto_layout, render)

NOW = datetime(2026, 10, 24, 20, 35, tzinfo=ZoneInfo("Asia/Tokyo"))
REF_DIR = Path(__file__).parent


def _grok_cards():
    """Grok weekly (22%) + 内訳 Build (20%) / Tasks (2%) のカード。"""
    reset = NOW + timedelta(days=5)
    main = Window("grok", "weekly", "週間", 22.0, reset, timedelta(days=7))
    build = Window("grok", "weekly_build", "週間 Build", 20.0, reset,
                   timedelta(days=7), part_of="weekly")
    tasks = Window("grok", "weekly_tasks", "週間 Tasks", 2.0, reset,
                   timedelta(days=7), part_of="weekly")
    pu = ProviderUsage("grok", "Grok", [main, build, tasks], None, NOW)
    pace = Pace(window=main, status=PaceStatus.TIGHT, elapsed_fraction=2.0 / 7.0,
                ideal_used_percent=200.0 / 7.0, rate_per_day=13.5,
                projected_end_percent=94.0, exhausts_at=None,
                sustainable_per_day=9.0, adjust_percent=-33.0,
                daily_used=[30.0, 25.0])
    return [(pu, pace), (pu, compute_pace(build, [], NOW)),
            (pu, compute_pace(tasks, [], NOW))]


# --------------------------------------------------------------- 解決 / 配置

def test_auto_layout_puts_parts_in_subs():
    """subs 省略の自動配置でも内訳窓がサブに入る。"""
    specs = auto_layout(_grok_cards())
    assert specs[0].main == "grok:weekly"
    assert specs[0].subs == ["grok:weekly_build", "grok:weekly_tasks"]


def test_resolve_panel_collects_parts_in_order():
    """parts は part_of == メイン窓 key の窓に並び順どおりパレット色を割り
    当てる。part_of が None の窓 (Claude の weekly_fable 相当) は対象外。"""
    cards = _grok_cards()
    panel = _resolve_panel(_lookups(cards), PanelSpec("grok:weekly"))
    assert [w.id for w, _c in panel["parts"]] == \
        ["grok:weekly_build", "grok:weekly_tasks"]
    assert [c for _w, c in panel["parts"]] == _PART_COLORS[:2]

    fable = Window("claude", "weekly_fable", "週間 Fable", 30.0,
                   NOW + timedelta(days=5), timedelta(days=7))
    claude = ProviderUsage("claude", "Claude Max",
                           [Window("claude", "weekly", "週間", 40.0,
                                   NOW + timedelta(days=5),
                                   timedelta(days=7)), fable])
    cards = cards + [(claude, compute_pace(fable, [], NOW)),
                     (claude, compute_pace(claude.windows[0], [], NOW))]
    panel = _resolve_panel(_lookups(cards), PanelSpec("claude:weekly"))
    assert panel["parts"] == []          # part_of 無しは内訳にしない


# ------------------------------------------------------------------ 描画

@pytest.mark.parametrize("gauge", ["remaining", "used"])
def test_render_grok_with_parts_smoke(gauge):
    """大パネル / 小パネルの両方で例外なく 1920x462 の画像が出る。"""
    img = render(_grok_cards(), NOW, gauge=gauge, layout=[
        PanelSpec("grok:weekly",
                  subs=["grok:weekly_build", "grok:weekly_tasks"]),
        PanelSpec("grok:weekly", size="small"),
    ])
    assert img.size == (1920, 462)


@pytest.fixture()
def text_calls(monkeypatch):
    calls = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, **kw):
        calls.append((self, tuple(xy), text, kw.get("font"), kw.get("anchor")))
        return orig(self, xy, text, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return calls


@pytest.mark.parametrize("gauge", ["remaining", "used"])
def test_part_sub_rows_render_as_breakdown(gauge, text_calls):
    """内訳窓のサブ行は凡例 + ラベル + 「20% 使用」で、状態バッジは出さない。
    (subs 省略の自動配置でサブに入ることの確認も兼ねる。)"""
    render(_grok_cards(), NOW, gauge=gauge)
    texts = [text for _d, _xy, text, _f, _a in text_calls]
    assert "Build" in texts and "Tasks" in texts          # サブラベル
    assert "20%" in texts and "2%" in texts               # 使用量 (gauge 非依存)
    # 数値に添う語 (gauge="used" はメイン数字の語として +1)
    assert texts.count("使用") == (3 if gauge == "used" else 2)
    assert texts.count("使い切り") == 1                   # バッジはメインのみ


def test_part_colors_are_painted(monkeypatch):
    """gauge="used" ではパレット色そのもので、gauge="remaining" では
    TRACK 半分混ぜの落ち着いた色で内訳が塗られる。"""
    fills = []
    orig = ImageDraw.ImageDraw.rounded_rectangle

    def spy(self, xy, radius=0, fill=None, **kw):
        if fill is not None:
            fills.append(tuple(fill))
        return orig(self, xy, radius=radius, fill=fill, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "rounded_rectangle", spy)
    purple = _PART_COLORS[0]

    render(_grok_cards(), NOW, gauge="used")
    assert purple in fills

    fills.clear()
    render(_grok_cards(), NOW, gauge="remaining")
    assert _mix(purple, TRACK, 0.85) in fills
    assert _mix(_PART_COLORS[1], TRACK, 0.85) in fills


# ------------------------------------------------------- _paint_parts の区間

class _RectSpy:
    """_paint_parts が塗る角丸矩形 (box, corners) を記録する _S の代役。"""

    def __init__(self):
        self.rects = []

    def rrect(self, box, radius=0, fill=None, outline=None, width=1,
              corners=None):
        self.rects.append((tuple(box), corners))


BAR = (0.0, 0.0, 400.0, 20.0)  # 幅 400px = 1% あたり 4px


def test_paint_parts_used_mode_positions_and_corners():
    """gauge="used": [0, 使用%] を左から順に塗る。左端 (0%) から始まる
    区間だけ左を丸め、end に達した区間だけ右を丸める。"""
    spy = _RectSpy()
    _paint_parts(spy, *BAR, 10.0, 0.0, 60.0,
                 [(30.0, _PART_COLORS[0]), (40.0, _PART_COLORS[1])])
    # 2 つ目は 60% を超えるので 30% に切り詰められる (30+40 -> 30+30)
    assert [box for box, _c in spy.rects] == \
        [(0.0, 0.0, 120.0, 20.0), (120.0, 0.0, 240.0, 20.0)]
    assert spy.rects[0][1] == (True, False, False, True)    # バー左端: 左だけ丸
    assert spy.rects[1][1] == (False, True, True, False)    # end に達する: 右だけ丸


def test_paint_parts_remaining_mode_no_notch_at_fill_boundary():
    """gauge="remaining": 消費済み区間 [残量%, 100%] を塗る。先頭区間の左は
    残量塗りとの境目なので丸めない (丸めると境目に切れ目が出る)。右端
    (100%) に達した区間だけ右を丸める。"""
    spy = _RectSpy()
    _paint_parts(spy, *BAR, 10.0, 78.0, 100.0,
                 [(20.0, _PART_COLORS[0]), (10.0, _PART_COLORS[1])])
    assert [box for box, _c in spy.rects] == \
        [(312.0, 0.0, 392.0, 20.0), (392.0, 0.0, 400.0, 20.0)]
    assert spy.rects[0][1] == (False, False, False, False)  # 境目は四角のまま
    assert spy.rects[1][1] == (False, True, True, False)    # バー右端: 右だけ丸


def test_paint_parts_rounds_left_only_at_bar_start():
    """start=0 (残量 0% など) のときは先頭区間の左もバーの左端として丸める。"""
    spy = _RectSpy()
    _paint_parts(spy, *BAR, 10.0, 0.0, 100.0,
                 [(20.0, _PART_COLORS[0]), (80.0, _PART_COLORS[1])])
    assert spy.rects[0][1] == (True, False, False, True)
    assert spy.rects[1][1] == (False, True, True, False)


# ------------------------------------------ parts 無しカードはピクセル同一

def _claude_cards():
    """tests/reference_noparts_*.png を生成したときと同じ固定カード。"""
    day = timedelta(days=1)
    claude_w = Window("claude", "weekly", "週間", 40.0, NOW + timedelta(days=5),
                      7 * day)
    claude_pace = Pace(window=claude_w, status=PaceStatus.OVER,
                       elapsed_fraction=2.0 / 7.0,
                       ideal_used_percent=2.0 / 7.0 * 100.0,
                       rate_per_day=20.0, projected_end_percent=140.0,
                       exhausts_at=NOW + timedelta(days=3),
                       sustainable_per_day=12.0, adjust_percent=-40.0,
                       daily_used=[6.0, 22.0, 12.0])
    fable_w = Window("claude", "weekly_fable", "週間 Fable", 30.0,
                     NOW + timedelta(days=5), 7 * day)
    fable_pace = Pace(window=fable_w, status=PaceStatus.ON_TRACK,
                      elapsed_fraction=2.0 / 7.0, ideal_used_percent=200.0 / 7.0,
                      rate_per_day=10.0, projected_end_percent=70.0,
                      exhausts_at=None, sustainable_per_day=10.0,
                      adjust_percent=0.0, daily_used=[10.0, 10.0, 10.0])
    claude_5h = Window("claude", "5h", "5時間", 30.0, NOW + timedelta(hours=3),
                       timedelta(hours=5))
    claude_5h_pace = Pace(window=claude_5h, status=PaceStatus.ON_TRACK,
                          elapsed_fraction=0.4, ideal_used_percent=40.0,
                          rate_per_day=360.0, projected_end_percent=75.0,
                          exhausts_at=None, sustainable_per_day=560.0,
                          adjust_percent=55.6, daily_used=[])
    claude = ProviderUsage("claude", "Claude Max",
                           [claude_w, fable_w, claude_5h])
    return [(claude, claude_pace), (claude, fable_pace),
            (claude, claude_5h_pace)]


@pytest.mark.parametrize("gauge", ["remaining", "used"])
def test_cards_without_parts_render_unchanged_pixels(gauge):
    """parts を持たないカードの描画は導入前と同じピクセルになる。"""
    ref = REF_DIR / ("reference_noparts_%s.png" % gauge)
    assert ref.exists(), "基準画像が無い: make_ref を再実行してください"
    img = render(_claude_cards(), NOW, gauge=gauge, layout=[
        PanelSpec("claude:weekly", subs=["claude:weekly_fable", "claude:5h"]),
        PanelSpec("claude:5h", size="small"),
    ])
    assert img.tobytes() == Image.open(ref).tobytes()


# --------------------------------------- review: メイン候補 / parts の絞り込み

def test_main_rank_never_prefers_part_windows():
    """part_of 付きの窓はどの窓より長くてもメイン候補にならない。"""
    short = Window("grok", "24h", "24時間", 10.0, NOW + timedelta(hours=16),
                   timedelta(days=1))
    part = Window("grok", "weekly_build", "週間 Build", 20.0,
                  NOW + timedelta(days=5), timedelta(days=7), part_of="weekly")
    pu = ProviderUsage("grok", "Grok", [part, short], None, NOW)
    specs = auto_layout([(pu, compute_pace(short, [], NOW)),
                         (pu, compute_pace(part, [], NOW))])
    assert specs[0].main == "grok:24h"          # 実窓がメイン


def test_parts_limited_to_explicit_subs():
    """spec.subs が明示のときは parts もその中の内訳だけ。空 (自動) なら全部。"""
    cards = _grok_cards()
    lookups = _lookups(cards)
    only_build = _resolve_panel(lookups, PanelSpec(
        "grok:weekly", subs=["grok:weekly_build"]))
    assert [w.id for w, _c in only_build["parts"]] == ["grok:weekly_build"]
    # 色も内訳 1 件ぶん (パレットの先頭) になる
    assert only_build["parts"][0][1] == _PART_COLORS[0]
    # subs が明示でも内訳が含まれていれば色付きサブ行になる
    without_parts = _resolve_panel(lookups, PanelSpec(
        "grok:weekly", subs=["claude:weekly_fable"]))
    assert without_parts["parts"] == []
    auto = _resolve_panel(lookups, PanelSpec("grok:weekly"))  # 空 = 自動
    assert [w.id for w, _c in auto["parts"]] == \
        ["grok:weekly_build", "grok:weekly_tasks"]


# ------------------------------------------- review: _paint_parts の角丸

@pytest.fixture()
def rrect_calls(monkeypatch):
    """ImageDraw.rounded_rectangle を記録に差し替える。
    → [(xy, radius, fill, corners)]"""
    calls = []
    orig = ImageDraw.ImageDraw.rounded_rectangle

    def spy(self, xy, radius=0, fill=None, outline=None, width=1, corners=None):
        calls.append((tuple(xy), radius,
                      tuple(fill) if fill is not None else None, corners))
        return orig(self, xy, radius=radius, fill=fill, outline=outline,
                    width=width, corners=corners)

    monkeypatch.setattr(ImageDraw.ImageDraw, "rounded_rectangle", spy)
    return calls


def test_first_part_segment_corner_rounding(rrect_calls):
    """先頭区間の左角はバー左端 (used) でのみ丸め、remaining の残り%側の
    境目では丸めない。"""
    render(_grok_cards(), NOW, gauge="used")
    purple_calls = [c for c in rrect_calls if c[2] == _PART_COLORS[0]]
    assert purple_calls and purple_calls[0][3][0] is True     # バー左端

    rrect_calls.clear()
    render(_grok_cards(), NOW, gauge="remaining")
    muted = _mix(_PART_COLORS[0], TRACK, 0.85)
    muted_calls = [c for c in rrect_calls if c[2] == muted]
    assert muted_calls and muted_calls[0][3][0] is False      # 境目は直線


def test_narrow_part_segment_radius_clamped_to_width(monkeypatch, rrect_calls):
    """4 列配置 (幅の狭いカード) でも 2% の区間 (約 8px) が例外なく描け、
    角丸半径が区間幅に収まる。"""
    cards = _grok_cards()
    day = timedelta(days=1)
    others = []
    for i, (prov, key) in enumerate((("claude", "weekly"), ("codex", "weekly"),
                                     ("glm", "5h"))):
        length = 7 * day if prov != "glm" else timedelta(hours=5)
        w = Window(prov, key, key, 10.0, NOW + timedelta(days=5), length)
        pu = ProviderUsage(prov, prov.title(), [w])
        others.append((pu, compute_pace(w, [], NOW)))
    img = render(cards + others, NOW, gauge="remaining")   # 4 列 = narrow
    assert img.size == (1920, 462)                         # 例外が出ない

    muted_amber = _mix(_PART_COLORS[1], TRACK, 0.85)   # Tasks 2% の区間
    narrow = [(xy, radius) for xy, radius, fill, _c in rrect_calls
              if fill == muted_amber and 0 < xy[2] - xy[0] <= 20]
    assert narrow, "幅 ~8px の内訳区間が描かれていない"
    for xy, radius in narrow:
        assert radius <= (xy[2] - xy[0]) / 2.0 - 0.5


def test_paint_parts_zero_width_segment_draws_plain_rect(monkeypatch):
    """幅が半径以下の極小区間は角丸なしの矩形になる (例外を出さない)。"""
    from quotacanvas.render import _paint_parts
    from quotacanvas.render import _S as Painter

    calls = []
    orig = ImageDraw.ImageDraw.rectangle

    def spy(self, xy, **kw):
        calls.append((tuple(xy), tuple(kw.get("fill") or ())))
        return orig(self, xy, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "rectangle", spy)
    img = Image.new("RGB", (200, 40))
    s = Painter(img, (0, 0))
    # 幅 1.98px (半径クランプで 0 になる) の区間 → 角丸なしの矩形
    _paint_parts(s, 10, 10, 190, 30, 10.0, 0.0, 1.1,
                 [(1.1, _PART_COLORS[0])])
    assert calls == [((10, 10, 11.98, 30), _PART_COLORS[0])]
    # 幅 1px 以下の区間は描かない (例外も出さない)
    calls.clear()
    _paint_parts(s, 10, 10, 190, 30, 10.0, 5.0, 5.3,
                 [(0.3, _PART_COLORS[0])])
    assert calls == []


# ------------------------------------------- % の数字の色 (percent_colors)

def test_percent_number_colors_follow_status_and_toggle():
    """オンなら 超過・枯渇=赤 / 余裕あり=緑 / 他 (tight を含む)=既定色。オフなら全部既定色。"""
    from types import SimpleNamespace
    from quotacanvas import render as R
    on, off = SimpleNamespace(percent_colors=True), SimpleNamespace(percent_colors=False)
    assert R._num_color(on, PaceStatus.OVER) == R.RED
    assert R._num_color(on, PaceStatus.EXHAUSTED) == R.RED
    assert R._num_color(on, PaceStatus.TIGHT) == R.WHITE
    assert R._num_color(on, PaceStatus.SPARE) == R.GREEN
    assert R._num_color(on, PaceStatus.ON_TRACK) == R.WHITE
    assert R._num_color(on, PaceStatus.ON_TRACK, R.TEXT) == R.TEXT
    for st in PaceStatus:
        assert R._num_color(off, st) == R.WHITE


def test_render_without_percent_colors_differs_only_when_colored():
    """percent_colors=False の描画は、色付きの数字がある (超過カード) ぶんだけ変わる。"""
    layout = [PanelSpec("claude:weekly", subs=["claude:weekly_fable", "claude:5h"]),
              PanelSpec("claude:5h", size="small")]
    colored = render(_claude_cards(), NOW, layout=layout)
    plain = render(_claude_cards(), NOW, layout=layout, percent_colors=False)
    assert colored.tobytes() != plain.tobytes()
