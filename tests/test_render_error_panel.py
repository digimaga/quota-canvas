"""窓 ID 指定パネルの取得失敗時フォールバックのテスト。

layout.panels で main に窓 ID ("claude:weekly") を指定していても、その
サービスの取得が失敗した (ProviderUsage.error あり・使える窓なし) ときは
プロバイダ ID 指定と同じ形のエラーパネル (main=None) として残ること。
サービス自体が存在しない場合や取得成功時に窓 key だけ不一致のときは
従来どおり None (消える) であることも確認する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quotacanvas.models import ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas import render as render_mod
from quotacanvas.render import (CANVAS_H, CANVAS_W, MARGIN, PanelSpec,
                                _layout_columns, render)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


def _error_cards(provider="claude"):
    """取得失敗サービス 1 つぶんの cards (pace=None)。"""
    return [(ProviderUsage(provider, provider.title(), [], "HTTP 429", NOW),
             None)]


def _ok_cards(provider="claude", key="5h"):
    """取得成功 (別の窓 key のみ) のサービス 1 つぶんの cards。"""
    win = Window(provider, key, "5時間", 10.0,
                 NOW + timedelta(hours=3), timedelta(hours=5))
    pu = ProviderUsage(provider, provider.title(), [win], None, NOW)
    return [(pu, compute_pace(win, [], NOW))]


def _spy_draw(monkeypatch, name):
    """render_mod の描画関数を記録に差し替える。→ [box, ...]"""
    boxes = []
    orig = getattr(render_mod, name)

    def spy(s, box, *args, **kwargs):
        boxes.append(tuple(box))
        orig(s, box, *args, **kwargs)

    monkeypatch.setattr(render_mod, name, spy)
    return boxes


# ------------------------------------------------------------ _layout_columns

def test_large_window_id_error_stays_as_error_panel():
    # 取得失敗サービスの窓 ID 指定 large はエラーパネル (main=None) として残る
    cols = _layout_columns(_error_cards(), [PanelSpec("claude:weekly")])
    assert len(cols) == 1 and cols[0][0] == "large"
    p = cols[0][1][0]
    assert p is not None and p["main"] is None
    assert p["g"]["error"] == "HTTP 429" and p["g"]["best"] is None


def test_small_window_id_error_stays_as_error_panel():
    # small でも同じ (_draw_small_error に流れる)
    cols = _layout_columns(_error_cards(), [PanelSpec("claude:weekly",
                                                     size="small")])
    assert len(cols) == 1 and cols[0][0] == "small"
    p = cols[0][1][0]
    assert p is not None and p["main"] is None


def test_unknown_service_window_id_disappears():
    # cards に無いサービスの窓 ID は従来どおり消える (large 列ごと / small 段)
    cols = _layout_columns(_error_cards("claude"),
                           [PanelSpec("codex:weekly"),
                            PanelSpec("codex:weekly", size="small")])
    assert cols == []


def test_window_id_missing_on_healthy_service_is_none():
    # 取得成功していて窓 key だけ存在しない場合はエラーパネルにしない
    # (large は列ごと消え、両段空の small 列も消える: 従来どおり)
    assert _layout_columns(_ok_cards(), [PanelSpec("claude:weekly")]) == []
    assert _layout_columns(_ok_cards(),
                           [PanelSpec("claude:weekly", size="small")]) == []


def test_provider_id_error_panel_shape_unchanged():
    # プロバイダ ID 指定のエラーパネルと同じ形になること
    cols = _layout_columns(_error_cards(), [PanelSpec("claude"),
                                            PanelSpec("claude:weekly")])
    a, b = cols[0][1][0], cols[1][1][0]
    assert (a["main"], a["subs"]) == (b["main"], b["subs"]) == (None, [])
    assert a["g"] is b["g"]


# -------------------------------------------------------------------- render

def test_render_large_error_panel_drawn(monkeypatch):
    # large: 灰色のエラーカードが全幅 1 列で実際に描かれる
    boxes = _spy_draw(monkeypatch, "_draw_error_card")
    img = render(_error_cards(), NOW.astimezone(),
                 layout=[PanelSpec("claude:weekly")])
    assert img.size == (CANVAS_W, CANVAS_H) == (1920, 462)
    assert len(boxes) == 1
    x0, _y0, x1, _y1 = boxes[0]
    assert (x0, x1) == (MARGIN, CANVAS_W - MARGIN)


def test_render_small_error_panel_drawn(monkeypatch):
    # small: 上段スロットに小エラーパネルが描かれる
    boxes = _spy_draw(monkeypatch, "_draw_small_error")
    img = render(_error_cards(), NOW.astimezone(),
                 layout=[PanelSpec("claude:weekly", size="small")])
    assert img.size == (1920, 462)
    assert len(boxes) == 1
    assert boxes[0][1] == render_mod.AREA_TOP      # 上段のまま
