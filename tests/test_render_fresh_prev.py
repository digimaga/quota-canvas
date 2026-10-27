"""W1 で足す表示要素のテスト。

- F5: PaceStatus.FRESH のバッジ・色・advice (リセット直後)
- F2: burst_exhausts_at の advice、主カードの「先に尽きるサブ窓」注記
- F3: pace.previous_used の「前回 ○% 余り / 使用」表示 (大・小パネル)
- F1: render(recommendation=...) のヘッダ 1 行 (タイトルと時計のあいだ)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image, ImageDraw

from quotacanvas.models import Pace, PaceStatus, ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import (GREEN, HEADER_H, PURPLE, RED, YELLOW,
                                PanelSpec, _STATUS_BADGE, _STATUS_COLOR, _S,
                                _draw_small_panel, _prev_line, _sub_note,
                                advice_color, advice_text, render)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def text_calls(monkeypatch):
    """ImageDraw.text を記録に差し替える。→ [(draw, xy, text, font, anchor, fill)]"""
    calls = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, **kw):
        calls.append((self, tuple(xy), str(text), kw.get("font"),
                      kw.get("anchor"), kw.get("fill")))
        return orig(self, xy, text, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return calls


def _pace(win=None, status=PaceStatus.ON_TRACK, **kw):
    win = win or Window("claude", "weekly", "週間", 40.0,
                        NOW + timedelta(days=5), timedelta(days=7))
    fields = dict(status=status, elapsed_fraction=2.0 / 7.0,
                  ideal_used_percent=200.0 / 7.0, rate_per_day=8.0,
                  projected_end_percent=80.0, exhausts_at=None,
                  sustainable_per_day=12.0, adjust_percent=50.0, daily_used=[])
    fields.update(kw)
    return Pace(window=win, **fields)


def _cards(pace, *extra):
    pu = ProviderUsage("claude", "Claude Max",
                       [pace.window] + [p.window for p in extra], None, NOW)
    return [(pu, pace)] + [(pu, p) for p in extra]


def _texts(calls):
    return [text for _d, _xy, text, _f, _a, _fill in calls]


def _local_short(dt, now=None):
    """render._fmt_time_short と同じ規則の期待値作成 (当日なら HH:MM)。"""
    l = dt.astimezone()
    if now is not None:
        ln = now.astimezone()
        if (l.year, l.month, l.day) == (ln.year, ln.month, ln.day):
            return "%02d:%02d" % (l.hour, l.minute)
    return "%d/%d %02d:%02d" % (l.month, l.day, l.hour, l.minute)


# ------------------------------------------------------- F5: FRESH ステータス

def test_fresh_color_and_badge_key():
    assert _STATUS_COLOR[PaceStatus.FRESH] == GREEN   # spare と同じ緑系
    assert _STATUS_BADGE[PaceStatus.FRESH] == "render.badge.fresh"


def test_advice_fresh_wording():
    start = NOW - timedelta(hours=2)
    win = Window("claude", "weekly", "週間", 2.0, start + timedelta(days=7),
                 timedelta(days=7))
    pace = compute_pace(win, [], NOW)
    assert pace.status is PaceStatus.FRESH
    assert advice_text(pace, False, NOW) == "リセット直後 · 枠は開いています"
    assert advice_text(pace, True, NOW) == "リセット直後 · 枠あり"


def test_render_fresh_card_draws_badge_and_advice(text_calls):
    start = NOW - timedelta(hours=2)
    win = Window("claude", "weekly", "週間", 2.0, start + timedelta(days=7),
                 timedelta(days=7))
    pace = compute_pace(win, [], NOW)
    render(_cards(pace), NOW, layout=[PanelSpec("claude:weekly")])
    texts = _texts(text_calls)
    assert "リセット直後" in texts                       # バッジ
    assert "リセット直後 · 枠は開いています" in texts      # advice 行


def test_render_fresh_small_panel_smoke(text_calls):
    start = NOW - timedelta(minutes=5)   # 5時間枠の 3% (= 9分) 未満
    win = Window("codex", "5h", "5時間", 1.0, start + timedelta(hours=5),
                 timedelta(hours=5))
    pace = compute_pace(win, [], NOW)
    assert pace.status is PaceStatus.FRESH
    render(_cards(pace), NOW, layout=[PanelSpec("codex:5h", size="small")])
    texts = _texts(text_calls)
    assert "リセット直後" in texts
    assert any("リセット直後 · 枠あり" in t for t in texts)


# ------------------------------------------------------------ F2: burst 関係

def test_advice_burst_replaces_advice_when_not_over():
    at = NOW + timedelta(hours=6, minutes=30)
    pace = _pace(status=PaceStatus.ON_TRACK, burst_rate_per_day=60.0,
                 burst_exhausts_at=at)
    expect = "このペースだと %s に尽きる見込み" % _local_short(at, NOW)
    assert advice_text(pace, False, NOW) == expect
    assert advice_text(pace, True, NOW) == \
        "このペースだと %s に尽きる" % _local_short(at, NOW)


def test_advice_burst_time_only_when_same_day():
    # 当日の時刻は日付を省く (_fmt_dt_short の他の使われ方は変更しない)
    at = NOW + timedelta(minutes=30)
    pace = _pace(status=PaceStatus.SPARE, burst_rate_per_day=60.0,
                 burst_exhausts_at=at)
    expect = "このペースだと %s に尽きる見込み" % _local_short(at, NOW)
    assert ":" in expect and "/" not in expect
    assert advice_text(pace, False, NOW) == expect


def test_advice_burst_ignored_when_over():
    at = NOW + timedelta(hours=6)
    kw = dict(rate_per_day=20.0, projected_end_percent=140.0,
              sustainable_per_day=12.0, adjust_percent=-40.0)
    with_burst = _pace(status=PaceStatus.OVER, exhausts_at=NOW + timedelta(days=1),
                       burst_rate_per_day=60.0, burst_exhausts_at=at, **kw)
    without = _pace(status=PaceStatus.OVER, exhausts_at=NOW + timedelta(days=1),
                    **kw)
    assert advice_text(with_burst, False, NOW) == advice_text(without, False, NOW)
    assert "尽きる見込み" not in advice_text(with_burst, False, NOW)


# ------------------------------------------ burst advice の色 (警告の黄)

@pytest.mark.parametrize("status", [PaceStatus.SPARE, PaceStatus.ON_TRACK,
                                    PaceStatus.FRESH])
def test_advice_color_is_yellow_with_burst(status):
    pace = _pace(status=status, burst_rate_per_day=60.0,
                 burst_exhausts_at=NOW + timedelta(hours=3))
    assert advice_color(pace) == YELLOW
    # バッジ・帯側は status 色のまま (spare / fresh は緑系、on_track は紫)
    assert _STATUS_COLOR[status] == (GREEN if status is not PaceStatus.ON_TRACK
                                     else PURPLE)


def test_advice_color_without_burst_or_when_over():
    assert advice_color(_pace()) == PURPLE      # on_track, burst なし
    assert advice_color(_pace(status=PaceStatus.SPARE)) == GREEN
    over = _pace(status=PaceStatus.OVER, exhausts_at=NOW + timedelta(days=1),
                 burst_rate_per_day=60.0,
                 burst_exhausts_at=NOW + timedelta(hours=3),
                 rate_per_day=20.0, projected_end_percent=140.0,
                 sustainable_per_day=12.0, adjust_percent=-40.0)
    assert advice_color(over) == RED            # OVER は burst があっても赤のまま


def test_render_burst_advice_is_yellow_but_badge_green(text_calls):
    """spare バッジのカードで burst の警告行だけが黄で描かれる。"""
    pace = _pace(status=PaceStatus.SPARE, burst_rate_per_day=60.0,
                 burst_exhausts_at=NOW + timedelta(hours=3))
    render(_cards(pace), NOW, layout=[PanelSpec("claude:weekly")])
    adv = [(text, fill) for _d, _xy, text, _f, _a, fill in text_calls
           if "尽きる見込み" in text]
    assert adv and adv[0][1] == YELLOW
    assert "余裕あり" in _texts(text_calls)     # バッジは spare のまま


def test_render_small_panel_burst_advice_is_yellow(text_calls):
    pace = _pace(status=PaceStatus.SPARE, burst_rate_per_day=60.0,
                 burst_exhausts_at=NOW + timedelta(hours=3))
    render(_cards(pace), NOW, layout=[PanelSpec("claude:weekly", size="small")])
    adv = [(text, fill) for _d, _xy, text, _f, _a, fill in text_calls
           if "尽きる" in text]
    assert adv and adv[0][1] == YELLOW


def test_sub_note_rules():
    main = _pace()  # ON_TRACK, exhausts_at なし
    win5h = Window("claude", "5h", "5時間", 85.0, NOW + timedelta(hours=2),
                   timedelta(hours=5))
    over5h = compute_pace(win5h, [], NOW)
    assert over5h.status is PaceStatus.OVER and over5h.exhausts_at is not None
    note = _sub_note(main, [{"pace": over5h, "label": "5時間"}], NOW)
    assert note == "5時間枠が %s に尽きる見込み" % _local_short(over5h.exhausts_at, NOW)

    # 内訳窓 (part_of) と別サービスのサブは対象外
    part = compute_pace(Window("claude", "weekly_build", "週間 Build", 85.0,
                               NOW + timedelta(hours=2), timedelta(hours=5),
                               part_of="weekly"), [], NOW)
    other = compute_pace(Window("codex", "5h", "5時間", 85.0,
                                NOW + timedelta(hours=2), timedelta(hours=5)),
                         [], NOW)
    assert _sub_note(main, [{"pace": part, "label": "Build"}], NOW) is None
    assert _sub_note(main, [{"pace": other, "label": "Codex 5時間"}], NOW) is None

    # 尽きる時刻が主窓より後でも、サブ窓が OVER なら触れる
    main_over = _pace(status=PaceStatus.OVER,
                      exhausts_at=NOW + timedelta(hours=1))
    later = _pace(win5h, status=PaceStatus.OVER,
                  exhausts_at=NOW + timedelta(hours=10))
    assert _sub_note(main_over, [{"pace": later, "label": "5時間"}], NOW) \
        is not None

    # EXHAUSTED (尽きる時刻なし) は「すでに上限」の文言
    dead = _pace(win5h, status=PaceStatus.EXHAUSTED)
    assert _sub_note(main, [{"pace": dead, "label": "5時間"}], NOW) == \
        "5時間枠はすでに上限"

    # 複数あるときは早く尽きるほう
    sooner = _pace(win5h, status=PaceStatus.OVER,
                   exhausts_at=NOW + timedelta(minutes=30))
    both = _sub_note(main, [{"pace": later, "label": "5時間"},
                            {"pace": sooner, "label": "5時間"}], NOW)
    assert both == "5時間枠が %s に尽きる見込み" \
        % _local_short(sooner.exhausts_at, NOW)


def test_render_main_card_mentions_early_exhausting_sub(text_calls):
    main = _pace()
    over5h = compute_pace(Window("claude", "5h", "5時間", 85.0,
                                 NOW + timedelta(hours=2), timedelta(hours=5)),
                          [], NOW)
    render(_cards(main, over5h), NOW,
           layout=[PanelSpec("claude:weekly", subs=["claude:5h"])])
    texts = _texts(text_calls)
    assert any("5時間枠が" in t and "尽きる見込み" in t for t in texts)


# ------------------------------------------------------- F3: 前回の期間の結果

def test_prev_line_wording_follows_gauge():
    pace = _pace(previous_used=64.0)
    assert _prev_line(pace, "remaining") == "前回 36% 余り"
    assert _prev_line(pace, "used") == "前回 64% 使用"
    assert _prev_line(_pace(), "remaining") is None


def test_render_large_card_shows_prev_line(text_calls):
    pace = _pace(previous_used=64.0)
    render(_cards(pace), NOW, layout=[PanelSpec("claude:weekly")])
    assert any("前回 36% 余り" in t for t in _texts(text_calls))


def test_render_large_card_used_gauge_shows_prev_used(text_calls):
    pace = _pace(previous_used=64.0)
    render(_cards(pace), NOW, gauge="used",
           layout=[PanelSpec("claude:weekly")])
    assert any("前回 64% 使用" in t for t in _texts(text_calls))


def test_large_card_prev_sits_by_big_number_not_elapsed(text_calls):
    """「前回」は大きい % の行 ("残り · 週間" の後ろ) に単独で置き、経過の
    行には混ぜない (狭いカード / 3 列の広いカードの両方)。"""
    pace = _pace(previous_used=64.0)
    render(_cards(pace), NOW, layout=[PanelSpec("claude:weekly")])
    texts = _texts(text_calls)
    assert any(t.endswith("前回 36% 余り") for t in texts)
    assert not [t for t in texts if "経過" in t and "前回" in t]
    others = [_pace(Window("claude", key, key, 40.0, NOW + timedelta(days=5),
                           timedelta(days=7))) for key in ("w2", "w3")]
    text_calls.clear()   # 3 列の広いカード (右に指標列がある)
    render(_cards(pace, *others), NOW,
           layout=[PanelSpec("claude:weekly"), PanelSpec("claude:w2"),
                   PanelSpec("claude:w3")])
    texts = _texts(text_calls)
    assert "前回 36% 余り" in texts
    assert not [t for t in texts if "経過" in t and "前回" in t]


def test_exhaust_line_shows_datetime_and_span_not_day_index(text_calls):
    pace = _pace(status=PaceStatus.OVER,
                 exhausts_at=NOW + timedelta(days=1, hours=2))
    render(_cards(pace), NOW, layout=[PanelSpec("claude:weekly")])
    texts = _texts(text_calls)
    want = "枯渇予測 %s (あと 1日2時間)" % _local_short(pace.exhausts_at, NOW)
    assert want in texts
    assert not [t for t in texts if "日目" in t]


def test_exhaust_time_not_repeated_when_advice_shows_burst(text_calls):
    """下段が「このペースだと … に尽きる」のときは、中段に枯渇日時を出さない。"""
    pace = _pace(exhausts_at=NOW + timedelta(days=3),
                 burst_exhausts_at=NOW + timedelta(hours=10))
    render(_cards(pace), NOW, layout=[PanelSpec("claude:weekly")])
    texts = _texts(text_calls)
    assert any("に尽きる" in t for t in texts)
    assert not [t for t in texts if "枯渇予測" in t]
    assert "リセットまでに 80% 消費見込み" in texts


def test_render_small_panel_shows_prev_line(text_calls):
    pace = _pace(previous_used=64.0)
    render(_cards(pace), NOW, layout=[PanelSpec("claude:weekly", size="small")])
    assert "前回 36% 余り" in _texts(text_calls)


def test_small_panel_omits_prev_line_when_crowded(text_calls):
    """横幅が足りない小パネルでは「前回」行を省略する (はみ出させない)。"""
    img = Image.new("RGB", (400, 240), (0, 0, 0))
    s = _S(img, (0, 0))
    pace = _pace(previous_used=64.0)
    _draw_small_panel(s, (0, 0, 170, 220), "Claude Max", (None, pace),
                      NOW, "remaining", (200, 60, 30))
    assert not [t for t in _texts(text_calls) if "前回" in t]


# ------------------------------------------------------- 区切り (separator)

def test_separator_replaces_middle_dots(text_calls):
    """render(separator=...) は文言の " · " と先頭の "· " を置き換える。
    None (既定) なら文言のまま。"""
    rec = "Codex を使う · 週の残り 62%"
    render(_cards(_pace()), NOW, recommendation=rec, separator="／")
    texts = _texts(text_calls)
    assert "Codex を使う／週の残り 62%" in texts
    assert "／週間" in texts                       # "· 週間" (窓ラベル)
    assert not [t for t in texts if "·" in t]
    text_calls.clear()
    render(_cards(_pace()), NOW, recommendation=rec)
    assert rec in _texts(text_calls)


def test_default_separator_follows_language():
    from quotacanvas import i18n
    from quotacanvas.render import default_separator
    assert default_separator() == "／"             # テストは日本語
    i18n.set_language("en")
    try:
        assert default_separator() == " · "
    finally:
        i18n.set_language("ja")


# ------------------------------------------------------- F1: 今使う先の 1 行

def test_recommendation_drawn_between_title_and_clock(text_calls):
    rec = "Codex を使う · 週の残り 62% · リセットまで 1日3時間"
    render(_cards(_pace()), NOW, recommendation=rec)
    mid = [(xy, text) for _d, xy, text, _f, a, _fill in text_calls
           if a == "mm" and text == rec]
    assert mid
    (x, y), _text = mid[0]
    assert y == pytest.approx(HEADER_H / 2.0 + 2)   # ヘッダの中央
    assert 100 < x < 1800                            # タイトルと時計のあいだ


def test_no_recommendation_keeps_header_unchanged(text_calls):
    render(_cards(_pace()), NOW)
    # ヘッダの高さに中央寄せ ("mm") の文字は描かれない (バッジはカード内)
    header_mm = [(xy, t) for _d, xy, t, _f, a, _fill in text_calls
                 if a == "mm" and xy[1] < HEADER_H]
    assert header_mm == []
    texts = _texts(text_calls)
    assert "QuotaCanvas" in texts


def test_fresh_with_burst_says_when_it_runs_out():
    # FRESH でも直近 2 時間の加速でリセット前に尽きるなら、その時刻を伝える
    at = NOW + timedelta(hours=20)
    pace = _pace(status=PaceStatus.FRESH, rate_per_day=None,
                 projected_end_percent=None, burst_rate_per_day=120.0,
                 burst_exhausts_at=at)
    text = advice_text(pace, False, NOW)
    assert "リセット直後" not in text
    assert _local_short(at, NOW) in text
    assert advice_color(pace) == YELLOW
