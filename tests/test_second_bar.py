"""小パネルの 2 本目のバー (layout.panels[].second_bar) のテスト。

設定の読み書き・自動/明示の判定・窓の選び方・描画 (リセット行が % の上、
アドバイス行と重ならない) を確認する。
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone

import pytest
from PIL import ImageDraw

from quotacanvas.config import (ConfigError, config_from_dict, config_to_dict,
                                load_config, save_config)
from quotacanvas.models import ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import (PanelSpec, SECOND_BAR_AUTO, _layout_columns,
                                render)
from quotacanvas.status import _spec_dict

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


def _service(provider, extra=()):
    weekly = Window(provider, "weekly", "週間", 30.0,
                    NOW + timedelta(days=5), timedelta(days=7))
    five = Window(provider, "5h", "5時間", 60.0,
                  NOW + timedelta(hours=2), timedelta(hours=5))
    wins = [weekly, five, *extra]
    pu = ProviderUsage(provider, provider.title(), wins, None, NOW)
    return [(pu, compute_pace(w, [], NOW)) for w in wins]


def _second(cards, spec):
    cols = _layout_columns(cards, [spec])
    return cols[0][1][0]["second"]


def test_auto_shows_for_listed_providers_only():
    assert set(SECOND_BAR_AUTO) == {"opencode", "opendesign", "gemini", "devin"}
    for prov in SECOND_BAR_AUTO:
        sec = _second(_service(prov), PanelSpec(main=prov, size="small"))
        assert sec is not None and sec[2].window.key == "5h"
    assert _second(_service("claude"), PanelSpec(main="claude", size="small")) is None


def test_explicit_true_false_and_large():
    cards = _service("claude")
    assert _second(cards, PanelSpec(main="claude", size="small", second_bar=True)) is not None
    cards = _service("opencode")
    assert _second(cards, PanelSpec(main="opencode", size="small", second_bar=False)) is None
    assert _second(cards, PanelSpec(main="opencode", size="large")) is None


def test_no_second_when_main_is_shortest():
    cards = _service("opencode")
    assert _second(cards, PanelSpec(main="opencode:5h", size="small")) is None


def test_prefers_5h_and_skips_breakdown_windows():
    part = Window("opencode", "weekly_x", "内訳", 5.0, NOW + timedelta(days=5),
                  timedelta(hours=1), part_of="weekly")
    daily = Window("opencode", "1d", "1日", 10.0, NOW + timedelta(hours=3),
                   timedelta(days=1))
    cards = _service("opencode", extra=[part, daily])
    sec = _second(cards, PanelSpec(main="opencode", size="small"))
    assert sec[2].window.key == "5h"
    # 5h が無ければ内訳でない最短の窓
    cards = [c for c in cards if c[1].window.key != "5h"]
    sec = _second(cards, PanelSpec(main="opencode", size="small"))
    assert sec[2].window.key == "1d"


def test_second_picks_exact_five_hours_not_15h():
    w15 = Window("opencode", "15h", "15時間", 10.0, NOW + timedelta(hours=3),
                 timedelta(hours=15))
    cards = _service("opencode", extra=[w15])
    assert _second(cards, PanelSpec(main="opencode", size="small"))[2].window.key == "5h"
    # 5 時間枠が無ければ最短 (1h) を 15h より優先
    h1 = Window("opencode", "1h", "1時間", 10.0, NOW + timedelta(minutes=30),
                timedelta(hours=1))
    cards = [c for c in _service("opencode", extra=[w15, h1])
             if c[1].window.key != "5h"]
    assert _second(cards, PanelSpec(main="opencode", size="small"))[2].window.key == "1h"


def test_render_dual_draws_reset_line_above_remaining_word(monkeypatch):
    calls = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, **kw):
        calls.append((tuple(xy), text))
        return orig(self, xy, text, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    render(_service("opencode"), NOW,
           layout=[PanelSpec(main="opencode", size="small")])
    reset = [xy for xy, txt in calls if "5日" in str(txt)]
    word = [xy for xy, txt in calls if txt == "残り"]
    pct = [xy for xy, txt in calls if txt == "70%"]
    # リセット表示は % の右、「残り」の真上 (同じ x、上)
    assert reset and word and pct
    assert reset[0][0] == word[0][0] and reset[0][1] < word[0][1]
    assert reset[0][0] > pct[0][0]
    assert any(txt == "5時間" for _xy, txt in calls)      # 2 本目のラベル


def test_render_single_when_off(monkeypatch):
    calls = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, **kw):
        calls.append(text)
        return orig(self, xy, text, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    render(_service("opencode"), NOW,
           layout=[PanelSpec(main="opencode", size="small", second_bar=False)])
    assert "5時間" not in calls


def _cfg(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_config_second_bar_parse_and_roundtrip(tmp_path):
    p = _cfg(tmp_path, """
        [[layout.panels]]
        main = "opencode"
        size = "small"
        second_bar = true

        [[layout.panels]]
        main = "gemini"
        size = "small"
        second_bar = false

        [[layout.panels]]
        main = "devin"
        size = "small"
        """)
    cfg = load_config(p)
    assert [pn.second_bar for pn in cfg.layout.panels] == [True, False, None]
    d = config_to_dict(cfg)
    panels = d["layout"]["panels"]
    assert panels[0]["second_bar"] is True and panels[1]["second_bar"] is False
    assert "second_bar" not in panels[2]
    assert config_to_dict(config_from_dict(d)) == d
    out = tmp_path / "saved.toml"
    save_config(config_from_dict(d), out)
    assert [pn.second_bar for pn in load_config(out).layout.panels] == [True, False, None]


def test_config_second_bar_rejects_non_bool(tmp_path):
    p = _cfg(tmp_path, """
        [[layout.panels]]
        main = "opencode"
        size = "small"
        second_bar = "yes"
        """)
    with pytest.raises(ConfigError):
        load_config(p)


def test_status_spec_dict_second_bar_only_when_explicit():
    assert "second_bar" not in _spec_dict(PanelSpec(main="a:b", size="small"))
    assert _spec_dict(PanelSpec(main="a:b", size="small", second_bar=False))["second_bar"] is False
