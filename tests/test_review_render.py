"""render の防御テスト (非有限値・長文・残高カードの行数)。

描画結果はメモリ上だけで作り、画像は保存しない。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from quotacanvas.models import (Balance, Pace, PaceStatus, ProviderUsage,
                                Window)
from quotacanvas.render import (PanelSpec, _S, _draw_balance_card,
                                _draw_error_card, _draw_local_card,
                                _draw_second_bar, _draw_service_card,
                                _elapsed_line, _fit_text, _fmt_num, _round,
                                render)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)
NONFINITE = [float("nan"), float("inf"), float("-inf")]


@pytest.fixture()
def text_calls(monkeypatch):
    """_S.text を記録に差し替える。→ [(xy, text, font, anchor)]"""
    calls = []
    orig = _S.text

    def spy(self, xy, text, font, fill, anchor=None):
        calls.append((tuple(xy), str(text), font, anchor))
        return orig(self, xy, text, font, fill, anchor)

    monkeypatch.setattr(_S, "text", spy)
    return calls


def _s():
    return _S(Image.new("RGB", (800, 500)), (0, 0), 1.0)


def _pace(win, **kw):
    fields = dict(status=PaceStatus.ON_TRACK, elapsed_fraction=2.0 / 7.0,
                  ideal_used_percent=200.0 / 7.0, rate_per_day=8.0,
                  projected_end_percent=80.0, exhausts_at=None,
                  sustainable_per_day=12.0, adjust_percent=50.0,
                  daily_used=[])
    fields.update(kw)
    return Pace(window=win, **fields)


def _window(used, label="週間"):
    return Window("claude", "weekly", label, used,
                  NOW + timedelta(days=5), timedelta(days=7))


# ------------------------------------------------------- 非有限値 (NaN / inf)

def test_round_and_fmt_num_Treat_nonfinite_as_unknown():
    for bad in NONFINITE:
        assert _round(bad) is None
        assert _fmt_num(bad) == "?"
    assert _round(1.4) == 1
    assert _round(1.5) == 2
    assert _fmt_num(12.0) == "12"


@pytest.mark.parametrize("bad", NONFINITE)
@pytest.mark.parametrize("gauge", ["remaining", "used"])
def test_render_nonfinite_percent_shows_unknown_not_zero(text_calls, bad,
                                                         gauge):
    win = _window(bad)
    pace = _pace(win, ideal_used_percent=bad, rate_per_day=bad,
                 projected_end_percent=bad, sustainable_per_day=bad,
                 adjust_percent=bad, previous_used=bad,
                 projected_leftover=bad)
    pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
    cards = [(pu, pace)]

    render(cards, NOW, layout=[PanelSpec("claude:weekly")], gauge=gauge)
    render(cards, NOW, layout=[PanelSpec("claude:weekly", size="small")],
           gauge=gauge)

    texts = [text for _xy, text, _font, _anchor in text_calls]
    assert "?" in texts
    assert "0%" not in texts
    assert "100%" not in texts


def test_second_bar_nonfinite_percent_shows_unknown(text_calls):
    win = _window(float("nan"), label="5時間")
    pace = _pace(win)
    _draw_second_bar(_s(), (16, 500), 100, (None, None, pace), (1, 2, 3),
                     "remaining")
    texts = [text for _xy, text, _font, _anchor in text_calls]
    assert "?" in texts
    assert "0%" not in texts


def test_gauge_data_is_not_drawn_as_zero_for_nonfinite():
    # 塗りと理想▼を描かないことを _S の rrect / polygon 呼び出しで確かめる。
    # (数値は "?" 表示になる。)
    win = _window(float("nan"))
    pace = _pace(win, ideal_used_percent=float("nan"))
    s = _s()
    drawn = []
    s.rrect = lambda *a, **k: drawn.append(("rrect", a, k.get("fill")))
    s.polygon = lambda *a, **k: drawn.append(("polygon", a, k.get("fill")))
    from quotacanvas.render import TRACK, _draw_gauge
    caption = _draw_gauge(s, (0, 0, 200, 20), win, pace, (1, 2, 3), (4, 5, 6),
                          "remaining")
    assert "?" in caption
    # 背景の TRACK だけ。テーマ色の塗り (幅 > 2px) と理想▼は無い。
    assert [kind for kind, _args, _fill in drawn] == ["rrect"]
    assert drawn[0][2] == TRACK


# ------------------------------------------------------- 長文の省略 (issue 3/4)

def test_fit_text_returns_empty_when_even_ellipsis_does_not_fit():
    s = _s()
    ell = s.textlength("…", font=s._font(_fit_test_font()))
    for width in (-10, 0, 1, ell):
        _font, shown = _fit_text(s, "とても長い名前です", "w7", 20, width,
                                 min_size=10)
        assert shown == ""


def _fit_test_font():
    from quotacanvas.render import F
    return F("w7", 10)


def test_error_card_ellipsizes_long_name_and_reason(text_calls):
    name = "VeryLongServiceName" * 8
    reason = "HTTP 503 " * 30
    _draw_error_card(_s(), (16, 52, 360, 454), {"name": name, "error": reason},
                     (1, 2, 3))
    texts = [text for _xy, text, _font, _anchor in text_calls]
    assert any(text.endswith("…") and len(text) < len(name) for text in texts)
    # 名前・理由のどちらも元の長さより短い (カード幅で省略されている)
    assert max(len(text) for text in texts) < max(len(name), len(reason))


def test_local_card_ellipsizes_long_name(text_calls):
    from quotacanvas.models import LocalSlots
    pu = ProviderUsage("local:test", "VeryLongLocalServerName" * 8,
                       slots=LocalSlots(1, 0, 4))
    _draw_local_card(_s(), (16, 52, 460, 245),
                     {"name": pu.name, "items": [(pu, None)]}, (1, 2, 3))
    texts = [text for _xy, text, _font, _anchor in text_calls]
    assert any(text.endswith("…") for text in texts)


def test_non_narrow_card_skips_indicators_when_width_is_tight(text_calls):
    # 長い窓ラベルで大きい % の行が右端まで埋まると、指標列は描かない。
    def texts_for(label):
        text_calls.clear()
        win = _window(40.0, label=label)
        pace = _pace(win)
        pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
        _draw_service_card(_s(), (0, 0, 540, 440), "Claude Max", (pu, pace),
                           [], NOW, "remaining", (1, 2, 3))
        return [text for _xy, text, _font, _anchor in text_calls]

    elapsed = _elapsed_line(_pace(_window(40.0)), NOW, total=True)
    assert elapsed in texts_for("週間")
    assert elapsed not in texts_for("非常に長い窓ラベル" * 20)


# --------------------------------------------------- 残高カードの行数 (issue 2)

def _balance_g(count, stale=False):
    balances = [Balance("USD", float(i + 1), "項目%d" % i)
                for i in range(count)]
    usage = ProviderUsage("deepseek", "DeepSeek", [], None, NOW,
                          balances=balances,
                          stale_error="HTTP 429" if stale else None)
    return {"name": "DeepSeek", "items": [(usage, None)],
            "stale": ("HTTP 429", NOW) if stale else None}


@pytest.mark.parametrize("stale", [False, True])
def test_balance_card_summarizes_rows_that_do_not_fit(text_calls, stale):
    y0, y1 = 52, 245                  # 小パネル 1 段ぶん
    _draw_balance_card(_s(), (16, y0, 460, y1), _balance_g(20, stale),
                       (1, 2, 3))
    rows = [(xy, text) for xy, text, _font, anchor in text_calls
            if anchor == "lm" and xy[1] >= y0 + 50]
    assert any(text.startswith("ほか ") and text.endswith(" 件")
               for _xy, text in rows)
    limit = y1 - (38 if stale else 20)
    assert all(xy[1] <= limit for xy, _text in rows)
