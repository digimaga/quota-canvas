#!/usr/bin/env python
"""Render preview PNGs of the quota canvas with dummy data (no device).

--lang ja|en で表示言語を切り替えられる (既定は en 指定なし=システム言語)。
出力ファイル名には言語が入る (preview_en.png, preview_custom_en.png, ...)。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from quotacanvas import i18n
from quotacanvas.models import Pace, PaceStatus, ProviderUsage, ResetCredit, Window
from quotacanvas.render import (PanelSpec, auto_layout, default_separator,
                                 render, render_error)

NOW = datetime(2026, 10, 24, 20, 35, tzinfo=ZoneInfo("Asia/Tokyo"))
OUT = Path(__file__).resolve().parent.parent / "preview"  # 既定の出力先 (git 管理外)

DAY = timedelta(days=1)

# ダミーデータの窓ラベル (実データは providers 側の文言): 言語ごとに切り替える
LABELS = {
    "ja": {"weekly": "週間", "fable": "週間 Fable", "5h": "5時間"},
    "en": {"weekly": "Weekly", "fable": "Weekly Fable", "5h": "5h"},
}


def win(provider, key, label, used, resets_in, length):
    return Window(provider=provider, key=key, label=label,
                  used_percent=used, resets_at=NOW + resets_in, length=length)


def pace(window, status, elapsed, daily=None, rate=None, proj=None,
         exhaust=None, sust=None, adj=None):
    return Pace(window=window, status=status, elapsed_fraction=elapsed,
                ideal_used_percent=elapsed * 100.0, rate_per_day=rate,
                projected_end_percent=proj, exhausts_at=exhaust,
                sustainable_per_day=sust, adjust_percent=adj,
                daily_used=list(daily or []))


def flatten(pu, window_paces):
    """(ProviderUsage, [(Window, Pace), ...]) -> one card tuple per window.
    A service with no usable window (e.g. fetch error) still counts as one
    card and is passed with pace=None."""
    if not window_paces:
        return [(pu, None)]
    return [(pu, p) for _w, p in window_paces]


def make_services(lang):
    """Returns dicts of service -> (ProviderUsage, [(Window, Pace)])."""
    lab = LABELS.get(lang, LABELS["en"])
    claude_w = win("claude", "weekly", lab["weekly"], 40.0,
                   timedelta(days=5), 7 * DAY)
    claude_w_pace = pace(claude_w, PaceStatus.OVER, 2.0 / 7.0,
                         daily=[6.0, 22.0, 12.0], rate=20.0, proj=140.0,
                         exhaust=NOW + timedelta(days=3), sust=12.0, adj=-40.0)
    fable_w = win("claude", "weekly_fable", lab["fable"], 30.0,
                  timedelta(days=5), 7 * DAY)
    fable_pace = pace(fable_w, PaceStatus.ON_TRACK, 2.0 / 7.0,
                      daily=[10.0, 10.0, 10.0], rate=10.0, proj=70.0,
                      sust=10.0, adj=0.0)
    claude_5h = win("claude", "5h", lab["5h"], 30.0, timedelta(hours=3),
                    timedelta(hours=5))
    claude_5h_pace = pace(claude_5h, PaceStatus.ON_TRACK, 0.4,
                          rate=360.0, proj=75.0, sust=560.0, adj=55.6)
    claude = ProviderUsage("claude", "Claude Max",
                           [claude_w, fable_w, claude_5h])

    codex_w = win("codex", "weekly", lab["weekly"], 8.0,
                  timedelta(days=5, hours=12), 7 * DAY)
    codex_pace = pace(codex_w, PaceStatus.SPARE, 1.5 / 7.0,
                      daily=[8.0, None], rate=4.0, proj=28.0,
                      sust=92.0 / 5.5, adj=318.0)
    # リセット権 2 件: 1 件は残り 20 時間 (赤)、もう 1 件は あと 5日5時間
    codex_credits = [
        ResetCredit("Full reset", NOW - timedelta(days=9, hours=4),
                    NOW + timedelta(hours=20)),
        ResetCredit("Full reset", NOW - timedelta(days=1, hours=3),
                    NOW + timedelta(days=5, hours=5)),
    ]
    codex = ProviderUsage("codex", "Codex", [codex_w],
                          reset_credits=codex_credits)

    glm_5h = win("glm", "5h", lab["5h"], 10.0, timedelta(hours=4, minutes=30),
                 timedelta(hours=5))
    glm_pace = pace(glm_5h, PaceStatus.UNKNOWN, 0.1)
    glm = ProviderUsage("glm", "GLM Coding", [glm_5h])

    grok_w = win("grok", "weekly", lab["weekly"], 55.0, timedelta(days=5),
                 7 * DAY)
    grok_pace = pace(grok_w, PaceStatus.TIGHT, 2.0 / 7.0,
                     daily=[30.0, 25.0], rate=13.5, proj=94.0,
                     sust=9.0, adj=-33.0)
    grok = ProviderUsage("grok", "Grok", [grok_w])

    gemini = ProviderUsage("gemini", "Gemini", [], error="HTTP 503")

    # 前回の期間の結果 (「前回 ○% 余り」表示の確認用)
    claude_w_pace.previous_used = 64.0
    codex_pace.previous_used = 88.0
    grok_pace.previous_used = 30.0

    return {
        "claude": (claude, [(claude_w, claude_w_pace),
                            (fable_w, fable_pace),
                            (claude_5h, claude_5h_pace)]),
        "codex": (codex, [(codex_w, codex_pace)]),
        "glm": (glm, [(glm_5h, glm_pace)]),
        "grok": (grok, [(grok_w, grok_pace)]),
        "gemini": (gemini, []),
    }


def cards_of(s, keys):
    cards = []
    for key in keys:
        cards += flatten(*s[key])
    return cards


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", choices=i18n.LANGUAGE_CHOICES, default="auto",
                    help="表示言語 (auto = システム言語)")
    ap.add_argument("--out", type=Path, default=OUT,
                    help="出力ディレクトリ")
    args = ap.parse_args()
    lang = i18n.set_language(args.lang)
    args.out.mkdir(parents=True, exist_ok=True)
    s = make_services(lang)

    jobs = [
        # 5 services via auto_layout: 3 large + 2 small (top: Grok weekly,
        # bottom: Gemini error)
        ("preview_%s.png" % lang,
         cards_of(s, ("claude", "codex", "glm", "grok", "gemini")),
         None),
        # custom layout example
        ("preview_custom_%s.png" % lang,
         cards_of(s, ("claude", "codex", "glm", "grok", "gemini")),
         [PanelSpec("claude:weekly"), PanelSpec("grok:weekly"),
          PanelSpec("glm:5h"), PanelSpec("codex:weekly", size="small"),
          PanelSpec("gemini", size="small")]),
        # small slots used for claude sub windows themselves
        ("preview_custom2_%s.png" % lang,
         cards_of(s, ("claude", "codex", "grok")),
         [PanelSpec("claude:weekly"), PanelSpec("codex:weekly"),
          PanelSpec("grok:weekly"), PanelSpec("claude:5h", size="small"),
          PanelSpec("claude:weekly_fable", size="small")]),
        # 3 services auto: 3 equal columns (not 4)
        ("preview_auto3_%s.png" % lang,
         cards_of(s, ("claude", "codex", "glm")), None),
        # Codex のリセット権 2 件 (1 件は残り 20 時間で赤): 大パネル (見出し+行)
        # と小パネル (1 行) の両方が出る配置
        ("preview_credits_%s.png" % lang,
         cards_of(s, ("claude", "codex", "grok", "gemini")),
         [PanelSpec("claude:weekly"), PanelSpec("codex:weekly"),
          PanelSpec("grok:weekly"), PanelSpec("codex:weekly", size="small"),
          PanelSpec("gemini", size="small")]),
    ]
    for name, cards, layout in jobs:
        path = args.out / name
        img = render(cards, NOW, gauge="remaining",
                     separator=default_separator(),
                     layout=auto_layout(cards) if layout is None else layout)
        img.save(path)
        print("saved", path, "(%s)" % lang)


if __name__ == "__main__":
    main()
