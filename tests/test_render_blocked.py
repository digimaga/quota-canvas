"""長い窓に止められた短いメイン窓 (blocked) の描画テスト。

_blocked_by の判定条件 (同じサービス・もっと長い窓・EXHAUSTED・リセットが
未来・part_of 無し・メイン自身は EXHAUSTED でない) と、小・大パネル ×
gauge remaining/used で大きな % が実際に使える量 (0% / 100%) になること、
止められていないときは従来の値のままであることを確認する。ダミーデータのみ。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from PIL import ImageDraw

from quotacanvas.models import Pace, PaceStatus, ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import (BG, PanelSpec, RED, TRACK, WHITE, _blocked_by,
                                _blocked_line, _brand_color, _mix,
                                _pool, render)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ _blocked_by --

def _w(provider, key, label, used, length, resets_at, part_of=None):
    return Window(provider, key, label, used, resets_at, length,
                  part_of=part_of)


def _pace(win, status):
    return Pace(window=win, status=status, elapsed_fraction=0.5,
                ideal_used_percent=50.0, rate_per_day=None,
                projected_end_percent=None, exhausts_at=None,
                sustainable_per_day=None, adjust_percent=None)


def _main_win(**kw):
    args = dict(provider="claude", key="5h", label="5時間", used=10.0,
                length=timedelta(hours=5),
                resets_at=NOW + timedelta(hours=2))
    args.update(kw)
    return _w(**args)


def _blocker_win(**kw):
    args = dict(provider="claude", key="weekly", label="週間", used=100.0,
                length=timedelta(days=7),
                resets_at=NOW + timedelta(days=3))
    args.update(kw)
    return _w(**args)


def test_blocked_by_finds_exhausted_longer_window():
    mp = _pace(_main_win(), PaceStatus.SPARE)
    bp = _pace(_blocker_win(), PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [bp], NOW) is bp


def test_blocked_by_requires_same_provider():
    mp = _pace(_main_win(), PaceStatus.SPARE)
    bp = _pace(_blocker_win(provider="codex"), PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [bp], NOW) is None


def test_blocked_by_requires_exhausted():
    mp = _pace(_main_win(), PaceStatus.SPARE)
    for st in (PaceStatus.ON_TRACK, PaceStatus.OVER, PaceStatus.SPARE,
               PaceStatus.TIGHT, PaceStatus.FRESH, PaceStatus.UNKNOWN):
        bp = _pace(_blocker_win(), st)
        assert _blocked_by(mp, [bp], NOW) is None, st


def test_blocked_by_requires_future_reset():
    mp = _pace(_main_win(), PaceStatus.SPARE)
    for resets_at in (NOW - timedelta(hours=1), NOW, None):
        bp = _pace(_blocker_win(resets_at=resets_at), PaceStatus.EXHAUSTED)
        assert _blocked_by(mp, [bp], NOW) is None, resets_at


def test_blocked_by_requires_part_of_none():
    mp = _pace(_main_win(), PaceStatus.SPARE)
    bp = _pace(_blocker_win(part_of="weekly"), PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [bp], NOW) is None


def test_blocked_by_requires_longer_window():
    mp = _pace(_main_win(), PaceStatus.SPARE)
    for length in (timedelta(hours=1), timedelta(hours=5),
                   timedelta(hours=4)):
        bp = _pace(_blocker_win(length=length), PaceStatus.EXHAUSTED)
        assert _blocked_by(mp, [bp], NOW) is None, length
    # 5 時間より少しでも長ければ対象 (5h + 1 秒)
    bp = _pace(_blocker_win(length=timedelta(hours=5) + timedelta(seconds=1)),
               PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [bp], NOW) is bp


def test_blocked_by_main_exhausted_returns_none():
    mp = _pace(_main_win(), PaceStatus.EXHAUSTED)
    bp = _pace(_blocker_win(), PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [bp], NOW) is None


def test_blocked_by_main_window_length_boundary():
    # メインが 1 日を超える窓なら止められない。ちょうど 1 日は対象。
    long_main = _pace(_main_win(key="7d", length=timedelta(days=7)),
                      PaceStatus.SPARE)
    bp30 = _pace(_blocker_win(key="30d", length=timedelta(days=30),
                              resets_at=NOW + timedelta(days=20)),
                 PaceStatus.EXHAUSTED)
    assert _blocked_by(long_main, [bp30], NOW) is None
    day_main = _pace(_main_win(key="1d", label="1日",
                               length=timedelta(days=1),
                               resets_at=NOW + timedelta(hours=20)),
                     PaceStatus.SPARE)
    assert _blocked_by(day_main, [bp30], NOW) is bp30


def test_blocked_by_picks_latest_reset():
    mp = _pace(_main_win(), PaceStatus.SPARE)
    bp_early = _pace(_blocker_win(key="weekly",
                                  resets_at=NOW + timedelta(days=3)),
                     PaceStatus.EXHAUSTED)
    bp_late = _pace(_blocker_win(key="30d", label="30日",
                                 length=timedelta(days=30),
                                 resets_at=NOW + timedelta(days=20)),
                    PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [bp_early, bp_late], NOW) is bp_late
    assert _blocked_by(mp, [bp_late, bp_early], NOW) is bp_late


def test_blocked_by_no_candidates():
    mp = _pace(_main_win(), PaceStatus.SPARE)
    assert _blocked_by(mp, [], NOW) is None


def test_blocked_line_uses_blocker_window_reset():
    """_blocked_line の span は止めている窓の resets_at - now (週間・3 日後)。
    メイン窓 (5時間・2時間後リセット) の情報ではない。"""
    weekly = Window("claude", "weekly", "週間", 100.0,
                    NOW + timedelta(days=3), timedelta(days=7))
    bp = compute_pace(weekly, [], NOW)
    assert bp.status is PaceStatus.EXHAUSTED
    line = _blocked_line(bp, NOW)
    assert "週間上限" in line and "解除まで" in line
    assert "3日" in line
    assert "2時間" not in line


def test_blocked_by_ignores_model_scoped_weekly():
    """Claude の weekly_<model> (weekly_scoped) は part_of=None でも
    モデル専用の別枠。尽きても共通の 5 時間枠は止まらない。"""
    mp = _pace(_main_win(), PaceStatus.SPARE)
    scoped = _pace(_blocker_win(key="weekly_fable", label="週間 Fable"),
                   PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [scoped], NOW) is None
    # 共通週間が本当に尽きていればそちらで止まる (共通枠の key だけを見る)
    shared = _pace(_blocker_win(), PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [scoped, shared], NOW) is shared


@pytest.mark.parametrize("key, pool", [
    ("5h", ""), ("1d", ""), ("4h", ""), ("1.5h", ""), ("30d", ""),
    ("daily", ""), ("weekly", ""), ("monthly", ""),
    ("claude", "claude"), ("claude_5h", "claude"),
    ("argon", "argon"), ("weekly_fable", "weekly_fable"),
])
def test_pool_from_key(key, pool):
    assert _pool(Window("x", key, "x", 0.0, NOW, timedelta(days=1))) == pool


def test_blocked_by_ignores_other_pools_gemini():
    """Gemini の Argon 専用枠・Antigravity 経由の Claude/GPT 枠 (3p) は
    gemini 共通の 5 時間枠とは別の利用枠。尽きても止めず、逆に共通の週間が
    尽きても 3p の 5 時間枠 (claude_5h) は止めない。"""
    five = _pace(_main_win(provider="gemini"), PaceStatus.SPARE)
    for key in ("argon", "claude"):
        other = _pace(_blocker_win(provider="gemini", key=key, label=key),
                      PaceStatus.EXHAUSTED)
        assert _blocked_by(five, [other], NOW) is None, key
    p3_five = _pace(_main_win(provider="gemini", key="claude_5h"),
                    PaceStatus.SPARE)
    shared = _pace(_blocker_win(provider="gemini"), PaceStatus.EXHAUSTED)
    assert _blocked_by(p3_five, [shared], NOW) is None
    assert _blocked_by(five, [shared], NOW) is shared


def test_blocked_by_same_3p_pool_gemini():
    """3p の週間 (claude) が尽きたら、同じ系統の 5 時間 (claude_5h) は止まる。"""
    p3_five = _pace(_main_win(provider="gemini", key="claude_5h"),
                    PaceStatus.SPARE)
    p3_week = _pace(_blocker_win(provider="gemini", key="claude", label="Claude"),
                    PaceStatus.EXHAUSTED)
    assert _blocked_by(p3_five, [p3_week], NOW) is p3_week


def test_blocked_by_generated_period_key():
    """_window_identity が作る 4h などの key も共通枠として判定する。"""
    mp = _pace(_main_win(provider="glm", key="4h", length=timedelta(hours=4)),
               PaceStatus.SPARE)
    bp = _pace(_blocker_win(provider="glm"), PaceStatus.EXHAUSTED)
    assert _blocked_by(mp, [bp], NOW) is bp


def test_blocked_by_main_used_full_without_reset_is_self_exhausted():
    """メイン窓が使用 100% 以上なら、リセット不明で状態が UNKNOWN でも
    メイン自身が尽きている扱い (長い窓の遮断表示にしない)。"""
    bp = _pace(_blocker_win(), PaceStatus.EXHAUSTED)
    for used in (100.0, 120.0):
        mp = _pace(_main_win(used=used, resets_at=None), PaceStatus.UNKNOWN)
        assert _blocked_by(mp, [bp], NOW) is None, used
    mp = _pace(_main_win(used=99.0, resets_at=None), PaceStatus.UNKNOWN)
    assert _blocked_by(mp, [bp], NOW) is bp


# ------------------------------------------------------------- 描画テスト --

@pytest.fixture()
def text_calls(monkeypatch):
    """ImageDraw.text を記録に差し替える。→ [(xy, text, font, anchor, fill)]"""
    calls = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, **kw):
        calls.append((tuple(xy), str(text), kw.get("font"),
                      kw.get("anchor"), kw.get("fill")))
        return orig(self, xy, text, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return calls


_PCT_RE = re.compile(r"\d+%|\?")


def _big_pcts(text_calls):
    """大きな % の数字 [(text, fill)]。% 表示の中で最大フォントのものだけ
    (サブ行・2 本目のバーの小さい数値は除く)。"""
    cands = []
    for _xy, text, font, _anchor, fill in text_calls:
        if not _PCT_RE.fullmatch(text):
            continue
        size = getattr(font, "size", 0) or 0
        cands.append((text, fill, size))
    if not cands:
        return []
    top = max(c[2] for c in cands)
    return [(t, f) for t, f, sz in cands if sz == top]


def _texts(text_calls):
    return [t for _xy, t, _f, _a, _fl in text_calls]


def _cards(provider="claude", weekly_used=100.0, five_used=0.0):
    """5 時間枠 (used=five_used) + 週間枠 (used=weekly_used) のサービス。"""
    five = Window(provider, "5h", "5時間", five_used,
                  NOW + timedelta(hours=2), timedelta(hours=5))
    weekly = Window(provider, "weekly", "週間", weekly_used,
                    NOW + timedelta(days=3), timedelta(days=7))
    wins = [five, weekly]
    pu = ProviderUsage(provider, provider.title(), wins, None, NOW)
    return [(pu, compute_pace(w, [], NOW)) for w in wins]


_BLOCKED_TXT = "週間上限 · 解除まで 3日"


# -- 大パネル ---------------------------------------------------------------

@pytest.mark.parametrize("gauge,expected", [("remaining", "0%"),
                                            ("used", "100%")])
def test_large_panel_blocked_shows_usable_zero(gauge, expected, text_calls):
    render(_cards(), NOW.astimezone(), gauge=gauge,
           layout=[PanelSpec("claude:5h", subs=["claude:weekly"],
                             size="large")])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == [expected]
    assert big[0][1] == RED                    # EXHAUSTED の数字色 (赤)
    texts = _texts(text_calls)
    assert any(_BLOCKED_TXT in t for t in texts)
    # メイン窓そのものの値は小さく残る ("5時間 100%" / used なら "5時間 0%")
    own = "5時間 100%" if gauge == "remaining" else "5時間 0%"
    assert any(own in t for t in texts)


@pytest.mark.parametrize("gauge,expected", [("remaining", "100%"),
                                            ("used", "0%")])
def test_large_panel_unblocked_keeps_own_value(gauge, expected, text_calls):
    render(_cards(weekly_used=30.0), NOW.astimezone(), gauge=gauge,
           layout=[PanelSpec("claude:5h", subs=["claude:weekly"],
                             size="large")])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == [expected]
    assert not any("週間上限" in t for t in _texts(text_calls))


def test_large_panel_blocked_monitor_only_keeps_monitor_color(text_calls):
    render(_cards(), NOW.astimezone(),
           layout=[PanelSpec("claude:5h", subs=["claude:weekly"],
                             size="large")],
           monitor_only=["claude"])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["0%"]
    assert big[0][1] == WHITE                  # % の数字は状態色にしない


# -- 小パネル ---------------------------------------------------------------

@pytest.mark.parametrize("gauge,expected", [("remaining", "0%"),
                                            ("used", "100%")])
def test_small_panel_blocked_shows_usable_zero(gauge, expected, text_calls):
    render(_cards(), NOW.astimezone(), gauge=gauge,
           layout=[PanelSpec("claude:5h", size="small", second_bar=True)])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == [expected]
    assert big[0][1] == RED
    texts = _texts(text_calls)
    assert any(_BLOCKED_TXT in t for t in texts)
    own = "5時間 100%" if gauge == "remaining" else "5時間 0%"
    assert any(own in t for t in texts)


@pytest.mark.parametrize("gauge,expected", [("remaining", "100%"),
                                            ("used", "0%")])
def test_small_panel_unblocked_keeps_own_value(gauge, expected, text_calls):
    render(_cards(weekly_used=30.0), NOW.astimezone(), gauge=gauge,
           layout=[PanelSpec("claude:5h", size="small", second_bar=True)])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == [expected]
    assert not any("週間上限" in t for t in _texts(text_calls))


def test_small_panel_blocked_monitor_only_keeps_monitor_color(text_calls):
    render(_cards(), NOW.astimezone(),
           layout=[PanelSpec("claude:5h", size="small", second_bar=True)],
           monitor_only=["claude"])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["0%"]
    assert big[0][1] == WHITE


def test_small_panel_without_second_bar_is_blocked(text_calls):
    """2 本目のバーが無くても、取得済みの全窓で遮断を判定する
    (表示されない窓でも止められる)。"""
    render(_cards(), NOW.astimezone(),
           layout=[PanelSpec("claude:5h", size="small", second_bar=False)])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["0%"]
    assert big[0][1] == RED
    texts = _texts(text_calls)
    assert any(_BLOCKED_TXT in t for t in texts)
    assert "上限到達" in texts
    assert any("5時間 100%" in t for t in texts)


def _daily_cards(daily_used=20.0, weekly_used=100.0):
    """日次 (1 日) + 週間のサービス。日次メインは第2バーに選べる短い窓が
    無い (週間はメインより長い) ので dual にならない。"""
    daily = Window("claude", "1d", "1日", daily_used,
                   NOW + timedelta(hours=20), timedelta(days=1))
    weekly = Window("claude", "weekly", "週間", weekly_used,
                    NOW + timedelta(days=3), timedelta(days=7))
    wins = [daily, weekly]
    pu = ProviderUsage("claude", "Claude", wins, None, NOW)
    return [(pu, compute_pace(w, [], NOW)) for w in wins]


def test_small_panel_daily_main_blocked_by_weekly(text_calls):
    """日次+週間だけの小パネル: 第2バーが無くても週間の使い切りで
    日次メインを遮断表示にする (second_bar=True でも second は選べない)。"""
    render(_daily_cards(), NOW.astimezone(),
           layout=[PanelSpec("claude:1d", size="small", second_bar=True)])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["0%"]
    assert big[0][1] == RED
    texts = _texts(text_calls)
    assert any(_BLOCKED_TXT in t for t in texts)
    assert "上限到達" in texts
    # 日次窓自身の値 (残り 80%) は小さく残る
    assert any("1日 80%" in t for t in texts)


def test_large_panel_blocked_without_subs(text_calls):
    """大パネルも subs に週間を出していなくても、取得済みの全窓で
    遮断を判定する。"""
    render(_cards(), NOW.astimezone(),
           layout=[PanelSpec("claude:5h", size="large")])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["0%"]
    assert big[0][1] == RED
    texts = _texts(text_calls)
    assert any(_BLOCKED_TXT in t for t in texts)
    assert "上限到達" in texts


def test_blocked_exhausted_main_window_keeps_normal_display(text_calls):
    """メイン窓自身が EXHAUSTED なら止められた扱いにしない (従来どおり)。"""
    render(_cards(five_used=100.0), NOW.astimezone(),
           layout=[PanelSpec("claude:5h", subs=["claude:weekly"],
                             size="large")])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["0%"]       # remaining: 自身の 0%
    assert not any("週間上限" in t for t in _texts(text_calls))
    assert not any(_BLOCKED_TXT in t for t in _texts(text_calls))
    assert big[0][1] == RED                     # 自身の EXHAUSTED 色


def test_blocked_long_main_window_is_not_blocked(text_calls):
    """メインが 1 日を超える窓なら長い窓が尽きていても従来どおり。"""
    monthly = Window("claude", "30d", "30日", 10.0,
                     NOW + timedelta(days=20), timedelta(days=30))
    weekly = Window("claude", "weekly", "週間", 100.0,
                    NOW + timedelta(days=3), timedelta(days=7))
    wins = [monthly, weekly]
    pu = ProviderUsage("claude", "Claude", wins, None, NOW)
    cards = [(pu, compute_pace(w, [], NOW)) for w in wins]
    render(cards, NOW.astimezone(),
           layout=[PanelSpec("claude:30d", subs=["claude:weekly"],
                             size="large")])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["90%"]      # 30日窓自身の残り
    assert not any("週間上限" in t for t in _texts(text_calls))


# ------------------------------------------- モデル専用枠・監視バッジ・描画 --

def _scoped_cards():
    """5時間(10%) + 共通週間(30%) + モデル専用 週間 Fable(100%)。
    weekly_fable は part_of=None だが Claude の weekly_scoped で、
    共通枠とは別のモデル別クォータ。"""
    five = Window("claude", "5h", "5時間", 10.0,
                  NOW + timedelta(hours=2), timedelta(hours=5))
    weekly = Window("claude", "weekly", "週間", 30.0,
                    NOW + timedelta(days=3), timedelta(days=7))
    fable = Window("claude", "weekly_fable", "週間 Fable", 100.0,
                   NOW + timedelta(days=3), timedelta(days=7))
    wins = [five, weekly, fable]
    pu = ProviderUsage("claude", "Claude", wins, None, NOW)
    return [(pu, compute_pace(w, [], NOW)) for w in wins]


@pytest.mark.parametrize("size,extra", [
    ("large", {"subs": ["claude:weekly"]}),
    ("small", {"second_bar": True}),
])
def test_model_scoped_weekly_does_not_block(size, extra, text_calls):
    """weekly_fable が上限でも共通の 5 時間枠は止まらない (大・小)。"""
    render(_scoped_cards(), NOW.astimezone(),
           layout=[PanelSpec("claude:5h", size=size, **extra)])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["90%"], size   # 5h の残り 90%
    assert not any("解除まで" in t for t in _texts(text_calls)), size


@pytest.mark.parametrize("size,extra", [
    ("large", {"subs": ["claude:weekly"]}),
    ("small", {"second_bar": True}),
])
def test_blocked_monitor_only_shows_exhausted_badge(size, extra, text_calls):
    """監視のみでも遮断時はバッジ文言を上限到達にする
    (配色は監視色・% 数字は白のまま)。"""
    render(_cards(), NOW.astimezone(),
           layout=[PanelSpec("claude:5h", size=size, **extra)],
           monitor_only=["claude"])
    texts = _texts(text_calls)
    assert "上限到達" in texts          # バッジ (助言の文言とは別)
    assert "監視のみ" not in texts
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == ["0%"]
    assert big[0][1] == WHITE           # % の数字は状態色にしない


@pytest.mark.parametrize("size,extra", [
    ("large", {"subs": ["claude:weekly"]}),
    ("small", {"second_bar": True}),
])
def test_blocked_panel_badge_and_advice(size, extra, text_calls):
    """大・小ともに: バッジは上限到達、助言は止めている窓のもの
    (「上限到達 · リセットまで あと 3日」= 週間のリセット基準)。"""
    render(_cards(), NOW.astimezone(),
           layout=[PanelSpec("claude:5h", size=size, **extra)])
    texts = _texts(text_calls)
    assert "上限到達" in texts                       # バッジ
    assert any("上限到達" in t and "リセットまで" in t and "3日" in t
               for t in texts), size                 # 遮断元の助言


# ------------------------------------------------------------ ゲージ形状 --

@pytest.fixture()
def shapes(monkeypatch):
    """ImageDraw.rounded_rectangle / rectangle を記録に差し替える。
    → (rrects, rects)。各要素は (box, fill)。"""
    rrects, rects = [], []
    orig_rr = ImageDraw.ImageDraw.rounded_rectangle
    orig_rc = ImageDraw.ImageDraw.rectangle

    def spy_rr(self, xy, **kw):
        rrects.append((tuple(xy), kw.get("fill")))
        return orig_rr(self, xy, **kw)

    def spy_rc(self, xy, **kw):
        rects.append((tuple(xy), kw.get("fill")))
        return orig_rc(self, xy, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "rounded_rectangle", spy_rr)
    monkeypatch.setattr(ImageDraw.ImageDraw, "rectangle", spy_rc)
    return rrects, rects


# 遮断ゲージの muted 塗りは _mix(テーマ色, TRACK, 0.85)、市松模様 は
# _mix(TRACK, BG, 0.5) (_draw_gauge と同じ式。テーマ色は claude の既定)。
_MUTED_FILL = _mix(_brand_color(None, "claude"), TRACK, 0.85)
_CHECKER_FILL = _mix(TRACK, BG, 0.5)


def _full_width_muted_fill(rrects):
    """muted 塗りの rrect のうち、同じ x 範囲の TRACK (ゲージの下地) と
    ちょうど同じ幅のもの = used ゲージの満杯 muted バー。"""
    muted = [b for b, f in rrects if f == _MUTED_FILL]
    tracks = [b for b, f in rrects if f == TRACK]
    return [m for m in muted
            if any(abs(m[0] - t[0]) < 1.0 and abs(m[2] - t[2]) < 1.0
                   for t in tracks)]


@pytest.mark.parametrize("size,extra", [
    ("large", {"subs": ["claude:weekly"]}),
    ("small", {"second_bar": True}),
])
def test_blocked_gauge_used_fills_full_muted(size, extra, shapes):
    """gauge="used" の遮断ゲージ: 実際に使える量 0 なので使用 100% =
    ゲージ全幅の muted バー + 市松模様。"""
    rrects, rects = shapes
    render(_cards(), NOW.astimezone(), gauge="used",
           layout=[PanelSpec("claude:5h", size=size, **extra)])
    assert _full_width_muted_fill(rrects), size
    assert any(f == _CHECKER_FILL for _b, f in rects), size


@pytest.mark.parametrize("size,extra", [
    ("large", {"subs": ["claude:weekly"]}),
    ("small", {"second_bar": True}),
])
def test_blocked_gauge_remaining_is_empty_muted(size, extra, shapes):
    """gauge="remaining" の遮断ゲージ: 残り 0% なので muted 塗りは無く、
    バー全体が市松模様。"""
    rrects, rects = shapes
    render(_cards(), NOW.astimezone(), gauge="remaining",
           layout=[PanelSpec("claude:5h", size=size, **extra)])
    assert not any(f == _MUTED_FILL for _b, f in rrects), size
    assert any(f == _CHECKER_FILL for _b, f in rects), size


def _nan_cards():
    """5時間窓の使用率が NaN (取得値なし) + 週間 100% のサービス。"""
    five = Window("claude", "5h", "5時間", float("nan"),
                  NOW + timedelta(hours=2), timedelta(hours=5))
    weekly = Window("claude", "weekly", "週間", 100.0,
                    NOW + timedelta(days=3), timedelta(days=7))
    wins = [five, weekly]
    pu = ProviderUsage("claude", "Claude", wins, None, NOW)
    return [(pu, _pace(five, PaceStatus.SPARE)),
            (pu, _pace(weekly, PaceStatus.EXHAUSTED))]


@pytest.mark.parametrize("gauge,expected", [("remaining", "0%"),
                                            ("used", "100%")])
def test_blocked_gauge_with_unknown_used(gauge, expected, shapes,
                                         text_calls):
    """メイン窓の使用率が不明 (NaN) でも、遮断時の used ゲージは確定した
    表示値 (使用 100%) から塗り幅を計算する (used_ok=False で消えない)。"""
    rrects, rects = shapes
    render(_nan_cards(), NOW.astimezone(), gauge=gauge,
           layout=[PanelSpec("claude:5h", subs=["claude:weekly"],
                             size="large")])
    big = _big_pcts(text_calls)
    assert [t for t, _f in big] == [expected]
    if gauge == "used":
        assert _full_width_muted_fill(rrects)
    else:
        assert not any(f == _MUTED_FILL for _b, f in rrects)
    assert any(f == _CHECKER_FILL for _b, f in rects)
