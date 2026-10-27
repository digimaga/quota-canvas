"""多言語化 (i18n) のテスト: locales の ja/en 同期と、描画系の英語表示。

(a) 全領域ファイル (他ワーカーの分も含む) について、ja と en でキー集合と
    差し込み名 ({name}) が一致すること。
(b) en で render() / render_error() / advice_text() が例外なく動き、
    ユーザーに見える文字列に日本語文字 (ひらがな・カタカナ・漢字) が
    混ざらないこと (キー落ち = キー文字列がそのまま出る、も拾う)。
"""

from __future__ import annotations

import string
from datetime import datetime, timedelta, timezone

from PIL import ImageDraw

from quotacanvas import i18n
from quotacanvas.models import (Pace, PaceStatus, ProviderUsage, ResetCredit,
                                Window)
from quotacanvas.pacing import compute_pace
from quotacanvas.render import (PanelSpec, advice_text, render, render_error)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)
NOW_L = NOW.astimezone()

_JP_RANGES = ((0x3040, 0x309F),   # ひらがな
              (0x30A0, 0x30FF),   # カタカナ
              (0x4E00, 0x9FFF))   # 漢字


def _has_japanese(text):
    # type: (str) -> bool
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _JP_RANGES)


# ------------------------------------------------ (a) ja/en 領域ファイルの同期

def _format_fields(text):
    # type: (str) -> set
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_locales_ja_en_have_same_files_keys_and_fields():
    ja_files = sorted(p.name for p in (i18n.LOCALES_DIR / "ja").glob("*.toml"))
    en_files = sorted(p.name for p in (i18n.LOCALES_DIR / "en").glob("*.toml"))
    assert ja_files, "ja の領域ファイルがない"
    assert ja_files == en_files, "ja/en で領域ファイルの集合が違う: %s vs %s" \
        % (ja_files, en_files)
    cat_ja, cat_en = i18n.catalog("ja"), i18n.catalog("en")
    assert set(cat_ja) == set(cat_en), \
        "ja/en でキー集合が違う: %s" % (set(cat_ja) ^ set(cat_en))
    for key, ja_text in cat_ja.items():
        assert cat_en[key], "en の値が空: %s" % key
        assert _format_fields(ja_text) == _format_fields(cat_en[key]), \
            "差し込み名が違う: %s (%r vs %r)" % (key, ja_text, cat_en[key])


# ------------------------------------------------------- (b) en での描画表示

def _card_set():
    """大 (サブ行あり) + リセット権 + 取得失敗 を 1 枚ずつ含む cards。"""
    w1 = Window("claude", "weekly", "Weekly", 40.0,
                NOW + timedelta(days=5), timedelta(days=7))
    w2 = Window("claude", "5h", "5h", 30.0,
                NOW + timedelta(hours=3), timedelta(hours=5))
    claude = ProviderUsage("claude", "Claude Max", [w1, w2], None, NOW,
                           stale_error="HTTP 429")
    cw = Window("codex", "weekly", "Weekly", 8.0,
                NOW + timedelta(days=5), timedelta(days=7))
    codex = ProviderUsage("codex", "Codex", [cw], None, NOW, reset_credits=[
        ResetCredit("Full reset", NOW - timedelta(days=1),
                    NOW + timedelta(hours=20)),
        ResetCredit("Full reset", NOW - timedelta(days=2),
                    NOW + timedelta(days=5)),
    ])
    bad = ProviderUsage("gemini", "Gemini", [], "HTTP 503", NOW)
    return [
        (claude, compute_pace(w1, [], NOW)),
        (claude, compute_pace(w2, [], NOW)),
        (codex, compute_pace(cw, [], NOW)),
        (bad, None),
    ]


def test_en_render_draws_no_japanese(monkeypatch):
    i18n.set_language("en")
    drawn = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, **kw):
        drawn.append(str(text))
        return orig(self, xy, text, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    layout = [PanelSpec("claude:weekly", subs=["claude:5h"]),
              PanelSpec("codex:weekly", size="small"),
              PanelSpec("gemini", size="small")]
    img = render(_card_set(), NOW_L, layout=layout)
    assert img.size == (1920, 462)
    img2 = render_error("HTTP 503", NOW_L)
    assert img2.size == (1920, 462)
    leaked = sorted({s for s in drawn if _has_japanese(s)})
    assert leaked == [], "en 表示に日本語が混ざっている: %s" % leaked
    assert any("Fetch error" in s for s in drawn)      # render_error の見出し
    assert any("Fetch failed" in s for s in drawn)     # stale マーク / エラーカード


def _all_advice_texts():
    """全ステータス × 枠の長さ × リセットまでの残り × sust 有無の advice 全量。"""
    texts = []
    for st in PaceStatus:
        for length in (timedelta(days=7), timedelta(hours=5)):
            for resets_in, sust in ((timedelta(days=5), 12.0),
                                    (timedelta(minutes=18), 12.0),
                                    (timedelta(days=5), None),
                                    (timedelta(minutes=18), None),
                                    (None, None)):
                win = Window("codex", "5h", "5h", 40.0,
                             NOW + resets_in if resets_in is not None else None,
                             length)
                pace = Pace(window=win, status=st, elapsed_fraction=0.3,
                            ideal_used_percent=30.0, rate_per_day=None,
                            projected_end_percent=None, exhausts_at=None,
                            sustainable_per_day=sust, adjust_percent=None,
                            daily_used=[])
                for compact in (False, True):
                    texts.append(advice_text(pace, compact, NOW))
    return texts


def test_en_advice_texts_have_no_japanese():
    i18n.set_language("en")
    texts = _all_advice_texts()
    assert len(texts) == 140   # 全 7 ステータス × 2 枠 × 5 状態 × compact 2
    leaked = sorted({s for s in texts if _has_japanese(s)})
    assert leaked == [], "en の advice に日本語が混ざっている: %s" % leaked


def test_ja_advice_texts_are_japanese_not_keys():
    # ja では日本語が返る (文言キーの落ち込み = キー文字列のまま、を逆側から拾う)
    i18n.set_language("ja")
    for text in _all_advice_texts():
        assert _has_japanese(text), "ja の advice が日本語でない: %r" % text
