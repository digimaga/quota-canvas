"""Tests for config.py + config-related touches in cli / pacing / providers."""

from __future__ import annotations

import logging
import tomllib
from datetime import datetime, time, timedelta, timezone

import pytest

from quotacanvas import cli, providers
from quotacanvas.config import (
    ConfigError,
    Night,
    brightness_for,
    config_from_dict,
    config_to_dict,
    format_toml,
    load_config,
    save_config,
)
from quotacanvas.history import History
from quotacanvas.models import PaceStatus, ProviderUsage, Window
from quotacanvas.pacing import compute_pace

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)


def write_cfg(tmp_path, text, name="config.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --- defaults ----------------------------------------------------------------


def test_missing_file_gives_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.display.windows == []
    assert cfg.display.brightness == 100
    assert cfg.display.night is None
    assert cfg.poll.interval == 900
    assert cfg.poll.redraw == 60
    assert cfg.pacing.tight == pytest.approx(92.0)
    assert cfg.pacing.on_track == pytest.approx(70.0)
    assert cfg.providers.enabled() == {"claude", "codex", "glm", "grok", "gemini"}
    assert not cfg.is_stale()


def test_default_path_is_under_dot_config():
    from quotacanvas.config import DEFAULT_PATH

    assert DEFAULT_PATH.name == "config.toml"
    assert ".config" in DEFAULT_PATH.parts
    assert "quota-canvas" in DEFAULT_PATH.parts


def test_load_full_file(tmp_path):
    p = write_cfg(
        tmp_path,
        """
        [display]
        windows = ["claude:weekly", "glm:5h"]
        brightness = 70

        [display.night]
        start = "22:30"
        end = "06:15"
        brightness = 15

        [poll]
        interval = 300
        redraw = 30

        [pacing]
        tight = 85.5
        on_track = 60

        [providers.claude]
        enabled = false
        [providers.glm]
        enabled = false
        """,
    )
    cfg = load_config(p)
    assert cfg.display.windows == ["claude:weekly", "glm:5h"]
    assert cfg.display.brightness == 70
    assert cfg.display.night == Night(time(22, 30), time(6, 15), 15)
    assert cfg.poll.interval == 300
    assert cfg.poll.redraw == 30
    assert cfg.pacing.tight == pytest.approx(85.5)
    assert cfg.pacing.on_track == pytest.approx(60.0)
    assert cfg.providers.enabled() == {"codex", "grok", "gemini"}  # grok / gemini are not mentioned in the file, so they keep their default


# --- windows ordering --------------------------------------------------------


def test_windows_order_preserved(tmp_path):
    p = write_cfg(tmp_path, 'windows = ["glm:5h", "claude:weekly", "codex:weekly"]\n', name="d.toml")
    # [display] テーブルに入れても同じ
    p.write_text('[display]\nwindows = ["glm:5h", "claude:weekly", "codex:weekly"]\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.display.windows == ["glm:5h", "claude:weekly", "codex:weekly"]


def _usage(provider, key, length):
    win = Window(provider, key, "ラベル", 10.0, NOW + length, length)
    return ProviderUsage(provider, provider.title(), [win], None, NOW)


def test_build_cards_follows_windows_order(tmp_path):
    usages = [
        _usage("claude", "weekly", timedelta(days=7)),
        _usage("codex", "weekly", timedelta(days=7)),
        _usage("glm", "5h", timedelta(hours=5)),
    ]
    history = History(tmp_path / "history.jsonl")
    order = ["glm:5h", "claude:weekly"]  # codex:weekly は無いので出さない
    cards = cli.build_cards(usages, history, NOW, order=order)
    ids = [pace.window.id for _, pace in cards if pace is not None]
    assert ids == order


def test_build_cards_default_order_unchanged(tmp_path):
    usages = [
        _usage("glm", "5h", timedelta(hours=5)),
        _usage("codex", "weekly", timedelta(days=7)),
    ]
    history = History(tmp_path / "history.jsonl")
    cards = cli.build_cards(usages, history, NOW)  # order なし: 長い窓が先
    ids = [pace.window.id for _, pace in cards if pace is not None]
    assert ids == ["codex:weekly", "glm:5h"]


# --- night -------------------------------------------------------------------


def _at(day_hour, minute=0):
    return datetime(2026, 10, 24, day_hour, minute)


def test_night_crosses_midnight():
    night = Night(time(23, 0), time(7, 0), 20)
    assert night.is_night(_at(23, 30))
    assert night.is_night(_at(0, 0))
    assert night.is_night(_at(6, 59))
    assert not night.is_night(_at(7, 0))
    assert not night.is_night(_at(12, 0))
    assert not night.is_night(_at(22, 59))


def test_night_within_same_day():
    night = Night(time(1, 0), time(5, 0), 10)
    assert night.is_night(_at(2, 0))
    assert night.is_night(_at(1, 0))
    assert not night.is_night(_at(0, 59))
    assert not night.is_night(_at(5, 0))


def test_night_start_equals_end_is_never_night():
    night = Night(time(3, 0), time(3, 0), 5)
    assert not night.is_night(_at(3, 0))
    assert not night.is_night(_at(15, 0))


def test_brightness_for_with_night(tmp_path):
    p = write_cfg(
        tmp_path,
        '[display]\nbrightness = 100\n[display.night]\nstart = "23:00"\nend = "07:00"\nbrightness = 20\n',
    )
    cfg = load_config(p)
    assert brightness_for(cfg, _at(23, 0)) == 20
    assert brightness_for(cfg, _at(3, 0)) == 20
    assert brightness_for(cfg, _at(7, 0)) == 100
    assert brightness_for(cfg, _at(12, 0)) == 100


def test_brightness_for_without_night(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert brightness_for(cfg, _at(23, 30)) == 100


# --- invalid values ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[display]\nbrightness = 150",
        "[display]\nbrightness = -1",
        '[display]\nbrightness = "80"',
        '[display]\nwindows = "claude:weekly"',
        '[display]\nwindows = [1, 2]',
        '[display]\nwindows = [""]',
        "[poll]\ninterval = 30",
        '[poll]\ninterval = "fast"',
        "[poll]\ninterval = 1.5",
        "[poll]\nredraw = 5",
        "[pacing]\ntight = 101",
        "[pacing]\non_track = -0.5",
        '[pacing]\ntight = "high"',
        '[display.night]\nstart = "25:00"',
        '[display.night]\nstart = "night"',
        '[display.night]\nbrightness = 200',
        '[providers.claude]\nenabled = "yes"',
        "providers = 3",  # テーブルでない
    ],
)
def test_invalid_values_raise_config_error(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, text))


def test_error_message_is_friendly(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, "[poll]\ninterval = 10\n"))
    msg = str(ei.value)
    assert "poll.interval" in msg
    assert "config.toml" in msg
    assert "60" in msg


def test_syntax_error_mentions_file(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, "[display\nwindows=["))
    assert "構文" in str(ei.value)
    assert "config.toml" in str(ei.value)


def test_provider_enabled_bad_bool_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, '[providers.claude]\nenabled = "yes"\n'))


# --- unknown keys --------------------------------------------------------------


def test_unknown_keys_are_ignored_with_warning(tmp_path, caplog):
    text = """
unknown_top = 1
[display]
colr = "red"
[display.night]
foo = true
[poll]
retry = 3
[pacing]
speed = 2
[providers.openai]
enabled = false
"""
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        cfg = load_config(write_cfg(tmp_path, text))
    # すべて無視され、既定値のまま
    assert cfg.display.brightness == 100
    assert cfg.display.windows == []
    assert cfg.poll.interval == 900
    assert cfg.pacing.tight == pytest.approx(92.0)
    assert cfg.providers.enabled() == {"claude", "codex", "glm", "grok", "gemini"}
    warned = [r.getMessage() for r in caplog.records]
    for key in ("unknown_top", "display.colr", "display.night.foo", "poll.retry",
                "pacing.speed", "providers.openai"):
        assert any(key in m for m in warned), key


# --- reload (staleness) ---------------------------------------------------------


def test_is_stale_detects_change(tmp_path):
    p = write_cfg(tmp_path, "[pacing]\ntight = 80.0\n")
    cfg = load_config(p)
    assert not cfg.is_stale()
    p.write_text("[pacing]\ntight = 80.0\n# changed\n", encoding="utf-8")
    assert cfg.is_stale()
    cfg2 = load_config(p)
    assert cfg2.pacing.tight == pytest.approx(80.0)
    assert not cfg2.is_stale()


def test_is_stale_when_file_appears_or_disappears(tmp_path):
    missing = tmp_path / "late.toml"
    cfg = load_config(missing)
    assert not cfg.is_stale()
    write_cfg(tmp_path, "[poll]\ninterval = 90\n", name="late.toml")
    assert cfg.is_stale()

    p = write_cfg(tmp_path, "[poll]\ninterval = 90\n", name="gone.toml")
    cfg2 = load_config(p)
    p.unlink()
    assert cfg2.is_stale()


def test_mark_checked_silences_staleness(tmp_path):
    p = write_cfg(tmp_path, "[pacing]\ntight = 80.0\n")
    cfg = load_config(p)
    p.write_text("[pacing]\ntight = 80.0\n# x\n", encoding="utf-8")
    assert cfg.is_stale()
    cfg.mark_checked()
    assert not cfg.is_stale()


# --- format_toml -----------------------------------------------------------------


def test_format_toml_roundtrip(tmp_path):
    p = write_cfg(
        tmp_path,
        """
        [display]
        windows = ["claude:weekly", "glm:5h"]
        brightness = 70
        paused = true
        gauge = "used"
        [display.night]
        start = "22:30"
        end = "06:15"
        brightness = 15
        [poll]
        interval = 300
        redraw = 30
        [pacing]
        tight = 85.5
        on_track = 60
        [providers.claude]
        enabled = false
        [providers.glm]
        windows = ["5h"]
        """,
    )
    cfg = load_config(p)
    parsed = tomllib.loads(format_toml(cfg))
    assert parsed["display"]["windows"] == ["claude:weekly", "glm:5h"]
    assert parsed["display"]["brightness"] == 70
    assert parsed["display"]["paused"] is True
    assert parsed["display"]["gauge"] == "used"
    assert parsed["display"]["night"]["start"] == "22:30"
    assert parsed["display"]["night"]["brightness"] == 15
    assert parsed["poll"] == {"interval": 300, "redraw": 30}
    assert parsed["pacing"]["tight"] == pytest.approx(85.5)
    assert parsed["pacing"]["on_track"] == pytest.approx(60.0)
    assert parsed["providers"]["claude"]["enabled"] is False
    assert parsed["providers"]["codex"]["enabled"] is True
    assert parsed["providers"]["glm"]["windows"] == ["5h"]


def test_format_toml_defaults_are_valid_toml(tmp_path):
    parsed = tomllib.loads(format_toml(load_config(tmp_path / "none.toml")))
    assert parsed["display"]["windows"] == []
    assert parsed["display"]["brightness"] == 100
    assert parsed["display"]["paused"] is False
    assert parsed["display"]["gauge"] == "remaining"
    assert "night" not in parsed["display"]  # 未設定なら夜間減光は無効のまま
    assert "layout" not in parsed  # 未設定なら配置セクションはコメントだけ
    assert parsed["poll"] == {"interval": 900, "redraw": 60}
    assert parsed["pacing"]["tight"] == pytest.approx(92.0)
    assert parsed["providers"]["glm"]["windows"] == []


def test_format_toml_layout_and_colors_roundtrip(tmp_path):
    p = write_cfg(tmp_path, """
        [[layout.panels]]
        main = "claude:weekly"
        subs = ["claude:5h"]

        [[layout.panels]]
        main = "codex:weekly"
        size = "small"

        [providers.glm]
        color = "#3B82F6"
        """)
    cfg = load_config(p)
    parsed = tomllib.loads(format_toml(cfg))  # format_toml は load_config で読み戻せる
    assert parsed["layout"]["panels"] == [
        {"main": "claude:weekly", "subs": ["claude:5h"], "size": "large"},
        {"main": "codex:weekly", "size": "small"},  # subs 省略 = 自動のまま書き出す
    ]
    assert parsed["providers"]["glm"]["color"] == "#3B82F6"
    # 読み戻した設定も同じ内容 (dict → Config → dict で不変)
    assert config_to_dict(config_from_dict(parsed)) == config_to_dict(cfg)


# --- pacing thresholds -----------------------------------------------------------


def _weekly(used):
    return Window("claude", "weekly", "週間", used,
                  NOW + timedelta(days=5), timedelta(days=7))  # 2 days elapsed


def test_compute_pace_default_thresholds_unchanged():
    # projected: 25% / 2d * 7d = 87.5 -> ON_TRACK (>=70, <92)
    assert compute_pace(_weekly(25.0), [], NOW).status is PaceStatus.ON_TRACK


def test_compute_pace_threshold_args():
    # 87.5 becomes TIGHT when the bar is lowered to 87
    assert compute_pace(_weekly(25.0), [], NOW, tight=87.0).status is PaceStatus.TIGHT
    # SPARE when on_track is raised above the projection
    assert compute_pace(_weekly(25.0), [], NOW, tight=100.0, on_track=88.0).status is PaceStatus.SPARE
    # ON_TRACK boundary uses on_track
    assert compute_pace(_weekly(25.0), [], NOW, on_track=87.5).status is PaceStatus.ON_TRACK
    assert compute_pace(_weekly(25.0), [], NOW, on_track=87.6, tight=100.0).status is PaceStatus.SPARE


# --- app contract keys: display.paused / display.gauge / providers.<p>.windows ----


def test_defaults_for_app_keys(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.display.paused is False
    assert cfg.display.gauge == "remaining"
    assert cfg.providers.windows == {}


def test_load_app_keys(tmp_path):
    p = write_cfg(tmp_path, """
        [display]
        paused = true
        gauge = "used"
        [providers.glm]
        windows = ["5h", "weekly"]
        """)
    cfg = load_config(p)
    assert cfg.display.paused is True
    assert cfg.display.gauge == "used"
    assert cfg.providers.windows_for("glm") == {"5h", "weekly"}
    assert cfg.providers.windows_for("claude") == set()  # 未指定 = 全部


@pytest.mark.parametrize(
    "text",
    [
        '[display]\npaused = "yes"',
        '[display]\npaused = 1',
        '[display]\ngauge = "both"',
        '[display]\ngauge = 1',
        '[providers.claude]\nwindows = "5h"',
        '[providers.claude]\nwindows = [5]',
        '[providers.claude]\nwindows = [""]',
    ],
)
def test_invalid_app_keys_raise_config_error(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, text))


# --- layout.panels (配置) ----------------------------------------------------------


def test_layout_defaults(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.layout.panels == []  # 空 = 自動配置
    assert cfg.providers.colors == {}


def test_load_layout_panels(tmp_path):
    p = write_cfg(tmp_path, """
        [[layout.panels]]
        main = "claude:weekly"
        subs = ["claude:5h"]
        size = "large"

        [[layout.panels]]
        main = "codex:weekly"
        size = "small"
        """)
    cfg = load_config(p)
    assert [(x.main, x.subs, x.size) for x in cfg.layout.panels] == [
        ("claude:weekly", ["claude:5h"], "large"),
        ("codex:weekly", None, "small"),  # subs 省略 = 自動（size は明示 "small"）
    ]


def test_layout_limits_ok(tmp_path):
    ok = [
        ["large"] * 4,                   # large 4 列
        ["large"] * 3 + ["small"],       # large 3 列 + small 列 (下段は空き枠)
        ["large", "small", "small"],     # 2 列目が small 2 段
        ["small", "small", "large", "large"],  # 左の列を 2 段に: 4 列 6 枚
        ["small"] * 8,                   # 4 列 × 2 段 = 8 枚
        ["large", "small", "small", "large", "small", "small"],  # 交互でも 4 列
        ["small"],                       # 1 列 (下段は空き枠)
        ["large"],                       # 1 枚だけでもよい
    ]
    for sizes in ok:
        text = "".join('[[layout.panels]]\nmain = "p%d:w"\nsize = "%s"\n\n'
                       % (i, s) for i, s in enumerate(sizes))
        cfg = load_config(write_cfg(tmp_path, text, name="-".join(sizes) + ".toml"))
        assert [x.size for x in cfg.layout.panels] == sizes


@pytest.mark.parametrize(
    "sizes",
    [
        ["large"] * 5,                          # large 5 列
        ["small"] * 9,                          # small 9 枚 = 5 列
        ["large", "large", "small", "small", "small", "large"],   # 5 列
        ["small", "small", "small"] + ["large"] * 4,              # 6 列
    ],
)
def test_layout_over_limit_raises(tmp_path, sizes):
    # 列は最大 4 (large 1 枚 / 連続する small 2 枚で 1 列)
    text = "".join('[[layout.panels]]\nmain = "p%d:w"\nsize = "%s"\n\n'
                   % (i, s) for i, s in enumerate(sizes))
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, text))
    assert "layout.panels" in str(ei.value)
    assert "列は最大 4" in str(ei.value)


@pytest.mark.parametrize(
    "text",
    [
        '[[layout.panels]]\nmain = "claude:weekly"\nsize = "big"',
        '[[layout.panels]]\nmain = "claude:weekly"\nsize = "LARGE"',
        '[[layout.panels]]\nmain = "claude:weekly"\nsize = 1',
        '[[layout.panels]]\nmain = "claude:weekly"\nsize = ""',
        '[[layout.panels]]\nsubs = ["claude:5h"]',          # main は必須
        '[[layout.panels]]\nmain = ""',                     # 空 main
        '[[layout.panels]]\nmain = 3',                      # main は文字列
        '[[layout.panels]]\nmain = "claude:weekly"\nsubs = "claude:5h"',
        '[[layout.panels]]\nmain = "claude:weekly"\nsubs = [5]',
        '[[layout.panels]]\nmain = "claude:weekly"\nsubs = [""]',
        'layout = ["x"]',                                   # テーブルでない
    ],
)
def test_invalid_layout_raises_config_error(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, text))


def test_layout_small_empty_main_is_empty_slot(tmp_path):
    # main = "" は size = "small" だけ許す: その段は空き枠 (位置を保つ)
    p = write_cfg(tmp_path, """
        [[layout.panels]]
        main = "claude:weekly"

        [[layout.panels]]
        main = ""
        size = "small"

        [[layout.panels]]
        main = "codex:weekly"
        size = "small"

        [[layout.panels]]
        main = ""
        size = "small"
        """)
    cfg = load_config(p)
    assert [(x.main, x.subs, x.size) for x in cfg.layout.panels] == [
        ("claude:weekly", None, "large"),
        ("", None, "small"),
        ("codex:weekly", None, "small"),
        ("", None, "small"),
    ]


def test_layout_large_empty_main_raises(tmp_path):
    # large で main が空なら従来どおりエラー (キー順に依存しない)
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(
            tmp_path, '[[layout.panels]]\nmain = ""\nsize = "large"\n'))
    assert "main" in str(ei.value)
    assert "small" in str(ei.value)  # 空き枠は small だけと案内する


def test_format_toml_roundtrips_empty_small_slot(tmp_path):
    p = write_cfg(tmp_path, """
        [[layout.panels]]
        main = ""
        size = "small"

        [[layout.panels]]
        main = "codex:weekly"
        size = "small"
        """)
    cfg = load_config(p)
    parsed = tomllib.loads(format_toml(cfg))  # format_toml は load_config で読み戻せる
    assert parsed["layout"]["panels"] == [
        {"main": "", "size": "small"},          # subs 省略 = 自動のまま書き出す
        {"main": "codex:weekly", "size": "small"},
    ]
    assert config_to_dict(config_from_dict(parsed)) == config_to_dict(cfg)


def test_layout_unknown_key_inside_panel_is_ignored(tmp_path, caplog):
    text = '[[layout.panels]]\nmain = "claude:weekly"\nwidth = 300\n'
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        cfg = load_config(write_cfg(tmp_path, text))
    assert [x.main for x in cfg.layout.panels] == ["claude:weekly"]
    assert any("layout.panels[0].width" in r.getMessage() for r in caplog.records)


# --- providers.<p>.color (テーマ色) -----------------------------------------------


@pytest.mark.parametrize("value", ["#D97757", "#000000", "#a3e635", "#ABCDEF"])
def test_provider_color_loads(tmp_path, value):
    cfg = load_config(write_cfg(tmp_path, "[providers.glm]\ncolor = \"%s\"\n" % value))
    assert cfg.providers.colors == {"glm": value}


@pytest.mark.parametrize(
    "text",
    [
        '[providers.glm]\ncolor = "D97757"',    # # 起点でない
        '[providers.glm]\ncolor = "#D9775"',    # 桁不足
        '[providers.glm]\ncolor = "#D977578"',  # 桁超過
        '[providers.glm]\ncolor = "#GGHHHH"',   # 16 進でない
        '[providers.glm]\ncolor = "red"',       # 色名は不可
        '[providers.glm]\ncolor = ""',
        '[providers.glm]\ncolor = 3',
    ],
)
def test_invalid_provider_color_raises(tmp_path, text):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, text))
    assert "RRGGBB" in str(ei.value)


# --- config_to_dict / config_from_dict / save_config -----------------------------


def test_config_to_dict_defaults(tmp_path):
    d = config_to_dict(load_config(tmp_path / "none.toml"))
    assert d["display"] == {"paused": False, "sleep_with_mac": True,
                            "gauge": "remaining", "percent_colors": True,
                            "separator": "", "language": "auto",
                            "windows": [], "brightness": 100}
    assert "night" not in d["display"]
    assert d["layout"] == {"panels": []}  # 空 = 自動配置
    assert d["poll"] == {"interval": 900, "redraw": 60}
    assert d["pacing"] == {"tight": 92.0, "on_track": 70.0}
    assert d["providers"]["claude"] == {"enabled": True, "windows": [],
                                        "token_refresh": "cli"}  # color は未指定なら absent


def test_config_dict_roundtrip_layout_and_colors(tmp_path):
    p = write_cfg(tmp_path, """
        [[layout.panels]]
        main = "claude:weekly"
        subs = ["claude:5h"]

        [[layout.panels]]
        main = "codex:weekly"
        size = "small"

        [providers.glm]
        color = "#3B82F6"
        """)
    d = config_to_dict(load_config(p))
    assert d["layout"] == {"panels": [
        {"main": "claude:weekly", "subs": ["claude:5h"], "size": "large"},
        {"main": "codex:weekly", "subs": None, "size": "small"},
    ]}
    assert d["providers"]["glm"] == {"enabled": True, "windows": [], "color": "#3B82F6"}
    assert "color" not in d["providers"]["claude"]
    assert config_to_dict(config_from_dict(d)) == d  # dict → Config → dict で不変


def test_config_from_dict_rejects_bad_layout_and_color():
    with pytest.raises(ConfigError):
        config_from_dict({"layout": {"panels": [{"main": "x", "size": "huge"}]}})
    with pytest.raises(ConfigError):
        config_from_dict({"layout": {"panels": [{"subs": ["x"]}]}})
    with pytest.raises(ConfigError):
        config_from_dict({"providers": {"glm": {"color": "blue"}}})


def test_config_from_dict_subs_null_means_auto():
    # JSON 設定 (config --set-json) で subs: null はキー省略と同じ = 自動。[] は明示的にサブなし。
    cfg = config_from_dict({"layout": {"panels": [
        {"main": "claude:weekly", "subs": None},
        {"main": "codex:weekly", "subs": []},
        {"main": "glm:5h"},
    ]}})
    assert [p.subs for p in cfg.layout.panels] == [None, [], None]
    assert config_to_dict(cfg)["layout"]["panels"][0]["subs"] is None  # dict でも自動のまま


def test_config_dict_roundtrip(tmp_path):
    p = write_cfg(tmp_path, """
        [display]
        windows = ["codex:weekly"]
        brightness = 40
        paused = true
        gauge = "used"
        [display.night]
        start = "23:30"
        end = "06:00"
        brightness = 25
        [poll]
        interval = 90
        redraw = 20
        [pacing]
        tight = 80
        on_track = 50
        [providers.claude]
        enabled = false
        windows = ["weekly"]
        """)
    d = config_to_dict(load_config(p))
    assert config_to_dict(config_from_dict(d)) == d  # dict → Config → dict で不変
    assert d["display"]["night"] == {"start": "23:30", "end": "06:00", "brightness": 25}


def test_config_from_dict_rejects_non_object(tmp_path):
    with pytest.raises(ConfigError):
        config_from_dict([1, 2, 3])
    with pytest.raises(ConfigError):
        config_from_dict("display")


def test_config_from_dict_error_mentions_source(tmp_path):
    with pytest.raises(ConfigError) as ei:
        config_from_dict({"display": {"brightness": 200}})
    assert "set-json" in str(ei.value)


def test_save_config_writes_loadable_toml(tmp_path):
    target = tmp_path / "out" / "config.toml"
    cfg = load_config(tmp_path / "none.toml")
    assert save_config(cfg, target) == target
    assert not (tmp_path / "out" / "config.toml.tmp").exists()
    assert config_to_dict(load_config(target)) == config_to_dict(cfg)


def test_save_config_atomic_replace(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("# old\n", encoding="utf-8")
    cfg = load_config(tmp_path / "none.toml")
    cfg.display.paused = True
    save_config(cfg, target)
    assert "paused = true" in target.read_text(encoding="utf-8")


def test_overlapping_saves_use_separate_temporary_files_and_clean_up(tmp_path, monkeypatch):
    from quotacanvas import config

    target = tmp_path / "config.toml"
    first = config_from_dict({"display": {"brightness": 40}})
    second = config_from_dict({"display": {"brightness": 60}})
    replace = config.os.replace
    sources = []

    def interleave(source, destination):
        sources.append(source)
        if len(sources) == 1:
            original = source.read_bytes()
            save_config(second, target)
            assert source.read_bytes() == original
            raise OSError("simulated replace failure")
        replace(source, destination)

    monkeypatch.setattr(config.os, "replace", interleave)
    with pytest.raises(OSError, match="simulated replace failure"):
        save_config(first, target)
    assert len(sources) == 2 and sources[0] != sources[1]
    assert all(source.parent == target.parent for source in sources)
    assert load_config(target).display.brightness == 60
    assert all(not source.exists() for source in sources)


# --- build_cards: providers.<p>.windows フィルタ (表示時に適用) --------------------


def test_build_cards_provider_windows_filter(tmp_path):
    usages = [
        _usage("glm", "5h", timedelta(hours=5)),
        _usage("glm", "weekly", timedelta(days=7)),
        _usage("claude", "weekly", timedelta(days=7)),
    ]
    history = History(tmp_path / "history.jsonl")
    cards = cli.build_cards(usages, history, NOW, provider_windows={"glm": {"5h"}})
    ids = [pace.window.id for _, pace in cards if pace is not None]
    assert ids == ["claude:weekly", "glm:5h"]  # glm:weekly は出ない


def test_build_cards_windows_filters_combine_with_and(tmp_path):
    usages = [
        _usage("glm", "5h", timedelta(hours=5)),
        _usage("claude", "weekly", timedelta(days=7)),
        _usage("claude", "5h", timedelta(hours=5)),
    ]
    history = History(tmp_path / "history.jsonl")
    # 両方のフィルタを満たす窓だけ出る: claude:weekly は providers.claude.windows で除外
    cards = cli.build_cards(usages, history, NOW,
                            order=["claude:weekly", "claude:5h", "glm:5h"],
                            provider_windows={"claude": {"5h"}})
    ids = [pace.window.id for _, pace in cards if pace is not None]
    assert ids == ["claude:5h", "glm:5h"]


def test_build_cards_empty_provider_windows_means_all(tmp_path):
    usages = [_usage("glm", "5h", timedelta(hours=5)), _usage("glm", "weekly", timedelta(days=7))]
    history = History(tmp_path / "history.jsonl")
    cards = cli.build_cards(usages, history, NOW, provider_windows={"glm": set()})
    ids = [pace.window.id for _, pace in cards if pace is not None]
    assert ids == ["glm:weekly", "glm:5h"]  # 空集合 = 制限なし


# --- providers.fetch_all(enabled=...) ---------------------------------------------

CLAUDE_PAYLOAD = {"five_hour": {"utilization": 1.0, "resets_at": None}}


def _patch_providers(monkeypatch):
    called = []

    def fake_http(url, headers, timeout=providers.TIMEOUT):
        called.append(url)
        if url == providers.CLAUDE_USAGE_URL:
            return CLAUDE_PAYLOAD
        return {}

    monkeypatch.setattr(providers.claude, "read_claude_credentials", lambda: ("t", "max"))
    monkeypatch.setattr(providers.codex, "read_codex_credentials", lambda: ("t", "acct"))
    monkeypatch.setattr(providers.glm, "read_glm_api_key", lambda: "key")
    for mod in (providers.claude, providers.codex, providers.glm):
        monkeypatch.setattr(mod, "http_get_json", fake_http)
    return called


def test_fetch_all_enabled_subset(monkeypatch):
    called = _patch_providers(monkeypatch)
    results = providers.fetch_all(NOW, enabled={"claude"})
    assert [r.provider for r in results] == ["claude"]
    assert called == [providers.CLAUDE_USAGE_URL]


def test_fetch_all_enabled_none_fetches_all(monkeypatch):
    _patch_providers(monkeypatch)
    results = providers.fetch_all(NOW, enabled=None)
    assert [r.provider for r in results] == ["claude", "codex", "glm", "grok", "gemini"]
    assert results[-1].error  # gemini: no keychain entry under the test fixture


def test_fetch_all_enabled_empty_fetches_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(providers.claude, "read_claude_credentials", boom)
    monkeypatch.setattr(providers.codex, "read_codex_credentials", boom)
    monkeypatch.setattr(providers.glm, "read_glm_api_key", boom)
    assert providers.fetch_all(NOW, enabled=set()) == []


# --- CLI: config --init and config error path --------------------------------------


def test_cli_config_init_creates_file_without_overwriting(tmp_path):
    target = tmp_path / "sub" / "config.toml"
    rc = cli.main(["--config", str(target), "config", "--init"])
    assert rc == 0
    assert target.exists()
    assert "windows" in target.read_text(encoding="utf-8")

    target.write_text("# my own\n", encoding="utf-8")
    rc = cli.main(["--config", str(target), "config", "--init"])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "# my own\n"  # 上書きしない


def test_cli_config_init_at_default_path(tmp_path, monkeypatch):
    fake_default = tmp_path / "home" / ".config" / "quota-canvas" / "config.toml"
    monkeypatch.setattr(cli, "DEFAULT_PATH", fake_default)  # cli 側の既定を使う
    rc = cli.main(["config", "--init"])
    assert rc == 0
    assert fake_default.exists()
    # --init は設定ファイルの読み込みに失敗しても動く（先に処理される）
    fake_default.write_text("[display\nbroken", encoding="utf-8")
    rc = cli.main(["config", "--init"])
    assert rc == 0
    assert fake_default.read_text(encoding="utf-8") == "[display\nbroken"


def test_cli_bad_config_exits_2_with_message(tmp_path, capsys):
    p = write_cfg(tmp_path, "[poll]\ninterval = 5\n")
    rc = cli.main(["--config", str(p), "once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "poll.interval" in err


def test_explicit_empty_subs_stays_empty(tmp_path):
    p = write_cfg(tmp_path, """
        [[layout.panels]]
        main = "claude:weekly"
        subs = []
        """)
    assert load_config(p).layout.panels[0].subs == []


# --- display.percent_colors (% の数字を利用状況の色にする) ------------------------


def test_percent_colors_defaults_to_true(tmp_path):
    assert load_config(tmp_path / "none.toml").display.percent_colors is True


def test_percent_colors_false_roundtrips(tmp_path):
    cfg = load_config(write_cfg(tmp_path, "[display]\npercent_colors = false\n"))
    assert cfg.display.percent_colors is False
    parsed = tomllib.loads(format_toml(cfg))
    assert parsed["display"]["percent_colors"] is False
    assert config_to_dict(config_from_dict(parsed))["display"]["percent_colors"] is False


def test_percent_colors_invalid_raises(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, '[display]\npercent_colors = "yes"\n'))
    assert "display.percent_colors" in str(ei.value)


# --- display.separator (文の区切り) -------------------------------------------------


def test_separator_defaults_to_language_default(tmp_path):
    assert load_config(tmp_path / "none.toml").display.separator == ""


def test_separator_roundtrips(tmp_path):
    cfg = load_config(write_cfg(tmp_path, '[display]\nseparator = " | "\n'))
    assert cfg.display.separator == " | "
    parsed = tomllib.loads(format_toml(cfg))
    assert parsed["display"]["separator"] == " | "
    assert config_to_dict(config_from_dict(parsed))["display"]["separator"] == " | "


@pytest.mark.parametrize("value", ["1", '"too long sep"'])
def test_separator_invalid_raises(tmp_path, value):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, "[display]\nseparator = %s\n" % value))
    assert "display.separator" in str(ei.value)


# --- display.sleep_with_mac (Mac スリープで画面も消灯し、復帰で戻す) ---------------


def test_sleep_with_mac_defaults_to_true(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.display.sleep_with_mac is True


def test_sleep_with_mac_false_roundtrips(tmp_path):
    p = write_cfg(tmp_path, "[display]\nsleep_with_mac = false\n")
    cfg = load_config(p)
    assert cfg.display.sleep_with_mac is False
    parsed = tomllib.loads(format_toml(cfg))  # format_toml は load_config で読み戻せる
    assert parsed["display"]["sleep_with_mac"] is False
    assert config_to_dict(config_from_dict(parsed))["display"]["sleep_with_mac"] is False


def test_sleep_with_mac_invalid_raises(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, '[display]\nsleep_with_mac = "yes"\n'))
    assert "display.sleep_with_mac" in str(ei.value)


def test_sleep_with_mac_set_json_roundtrip(tmp_path):
    d = config_to_dict(load_config(tmp_path / "none.toml"))
    d["display"]["sleep_with_mac"] = False
    cfg = config_from_dict(d)
    assert cfg.display.sleep_with_mac is False
    assert config_to_dict(cfg)["display"]["sleep_with_mac"] is False  # dict → Config → dict で不変


# --- providers.<p>.token_refresh (claude / codex / grok) ---------------------------

def test_token_refresh_default(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.providers.claude_token_refresh == "cli"
    assert cfg.providers.codex_token_refresh == "cli"
    assert cfg.providers.grok_token_refresh == "cli"


def test_token_refresh_parse_and_roundtrip(tmp_path):
    p = write_cfg(tmp_path, """
        [providers.claude]
        enabled = true
        token_refresh = "off"
        """)
    cfg = load_config(p)
    assert cfg.providers.claude_token_refresh == "off"

    parsed = tomllib.loads(format_toml(cfg))  # format_toml は load_config で読み戻せる
    assert parsed["providers"]["claude"]["token_refresh"] == "off"
    # codex / grok / gemini も token_refresh を持つ (設定していなければ既定の "cli")
    assert parsed["providers"]["codex"]["token_refresh"] == "cli"
    assert parsed["providers"]["grok"]["token_refresh"] == "cli"
    assert parsed["providers"]["gemini"]["token_refresh"] == "cli"
    # glm は token_refresh を持たない
    assert "token_refresh" not in parsed["providers"]["glm"]

    d = config_to_dict(cfg)  # config --json は claude / codex / grok / gemini に token_refresh を含める
    assert d["providers"]["claude"]["token_refresh"] == "off"
    assert d["providers"]["codex"]["token_refresh"] == "cli"
    assert d["providers"]["grok"]["token_refresh"] == "cli"
    assert d["providers"]["gemini"]["token_refresh"] == "cli"
    assert config_to_dict(config_from_dict(parsed)) == d  # --set-json で読み戻して不変


@pytest.mark.parametrize("provider", ["codex", "grok", "gemini"])
def test_token_refresh_off_roundtrip_per_provider(tmp_path, provider):
    p = write_cfg(tmp_path, """
        [providers.%s]
        token_refresh = "off"
        """ % provider)
    cfg = load_config(p)
    assert getattr(cfg.providers, provider + "_token_refresh") == "off"
    # ほかの provider には波及しない
    for other in ("claude", "codex", "grok", "gemini"):
        if other != provider:
            assert getattr(cfg.providers, other + "_token_refresh") == "cli"
    parsed = tomllib.loads(format_toml(cfg))
    assert parsed["providers"][provider]["token_refresh"] == "off"
    d = config_to_dict(cfg)
    assert d["providers"][provider]["token_refresh"] == "off"
    assert config_to_dict(config_from_dict(parsed)) == d


@pytest.mark.parametrize("provider", ["claude", "codex", "grok", "gemini"])
def test_token_refresh_rejects_unknown_value(tmp_path, provider):
    p = write_cfg(tmp_path, """
        [providers.%s]
        token_refresh = "auto"
        """ % provider)
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "providers.%s.token_refresh" % provider in str(ei.value)


def test_token_refresh_on_other_providers_is_unknown_key():
    # token_refresh を持てないのは glm (claude / codex / grok / gemini のみ)
    cfg = config_from_dict({"providers": {"glm": {"token_refresh": "off"}}})
    assert cfg.providers.claude_token_refresh == "cli"
    assert config_to_dict(cfg)["providers"]["glm"] == {"enabled": True, "windows": []}


# --- display.language (表示言語) ----------------------------------------------------


def test_language_defaults_to_auto(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.display.language == "auto"


def test_language_accepts_ja_and_en(tmp_path):
    for lang in ("ja", "en"):
        p = write_cfg(tmp_path, '[display]\nlanguage = "%s"\n' % lang)
        assert load_config(p).display.language == lang


def test_language_roundtrips_through_toml_and_dict(tmp_path):
    p = write_cfg(tmp_path, '[display]\nlanguage = "en"\n')
    cfg = load_config(p)
    parsed = tomllib.loads(format_toml(cfg))  # format_toml は load_config で読み戻せる
    assert parsed["display"]["language"] == "en"
    assert config_to_dict(cfg)["display"]["language"] == "en"
    assert config_to_dict(config_from_dict(parsed))["display"]["language"] == "en"


@pytest.mark.parametrize(
    "value",
    ["fr", "ja-JP", "AUTO", "", "ja ", 1, True, None],
)
def test_language_invalid_raises(value):
    with pytest.raises(ConfigError) as ei:
        config_from_dict({"display": {"language": value}})
    assert "display.language" in str(ei.value)


def test_language_missing_key_in_toml_means_auto(tmp_path):
    # language を書かなければ既定 auto (既存の設定ファイルに影響しない)
    p = write_cfg(tmp_path, "[display]\nbrightness = 80\n")
    assert load_config(p).display.language == "auto"


# --- routing.workers (名前の検証: gate.WORKER_NAME_RE と同じ条件) -------------


def test_routing_workers_names_ok(tmp_path):
    cfg = load_config(write_cfg(
        tmp_path, '[routing.workers]\nglm = ["glm:5h"]\n"my_ai.2" = ["codex:*"]\n'))
    assert cfg.routing.workers == {"glm": ["glm:5h"], "my_ai.2": ["codex:*"]}


@pytest.mark.parametrize("name", ["has space", 'quo"te', "new\nline", "tab\there",
                                  "x" * 41, "-bad$name"])
def test_routing_workers_bad_name_raises(name):
    with pytest.raises(ConfigError) as ei:
        config_from_dict({"routing": {"workers": {name: ["glm:5h"]}}})
    assert "ワーカー名" in str(ei.value) or "invalid worker name" in str(ei.value)


def test_routing_workers_empty_name_raises():
    # 空の名前は既存の workers_required (テーブルの形) で落とす
    with pytest.raises(ConfigError):
        config_from_dict({"routing": {"workers": {"": ["glm:5h"]}}})


# --- routing.workers の順番 (config --json の worker_order / --set-json の配列形式)

def test_config_json_carries_worker_order():
    # JSON のオブジェクトはキー順を保てない (アプリの JSONSerialization が並べ替える)
    # ため、config --json は表示順を worker_order でも返す
    cfg = config_from_dict({"routing": {"workers": {"zeta": ["glm:5h"],
                                                    "alpha": ["codex:*"]}}})
    data = config_to_dict(cfg)
    assert data["routing"]["worker_order"] == ["zeta", "alpha"]
    assert data["routing"]["workers"] == {"zeta": ["glm:5h"], "alpha": ["codex:*"]}


def test_set_json_accepts_worker_array():
    # アプリは [{name, windows}] の配列で送る (配列の順が line の表示順になる)
    cfg = config_from_dict({"routing": {"workers": [
        {"name": "zeta", "windows": ["glm:5h"]},
        {"name": "alpha", "windows": ["codex:*"]},
    ]}})
    assert list(cfg.routing.workers) == ["zeta", "alpha"]
    assert config_to_dict(cfg)["routing"]["worker_order"] == ["zeta", "alpha"]


def test_set_json_empty_worker_array_means_default():
    # 空配列 = ワーカーなし (取得できているサービスごとの既定ワーカー)
    cfg = config_from_dict({"routing": {"workers": []}})
    assert cfg.routing.workers == {}
    assert config_to_dict(cfg)["routing"]["worker_order"] == []


def test_worker_order_reorders_dict_form():
    # dict 形式 + worker_order ならその順に並べる (order 外は名前順で後ろ)
    cfg = config_from_dict({"routing": {
        "workers": {"a": ["glm:5h"], "b": ["codex:*"], "c": ["claude:weekly"]},
        "worker_order": ["c", "b"],
    }})
    assert list(cfg.routing.workers) == ["c", "b", "a"]


def test_worker_order_bad_type_raises():
    with pytest.raises(ConfigError):
        config_from_dict({"routing": {"workers": {"a": ["glm:5h"]},
                                      "worker_order": "a"}})


def test_worker_order_checked_with_array_form():
    # 配列形式の workers と一緒に来ても worker_order の型は検査する
    with pytest.raises(ConfigError):
        config_from_dict({"routing": {"workers": [{"name": "a", "windows": ["glm:5h"]}],
                                      "worker_order": "a"}})
    # 正しければ配列の順を優先し、worker_order は無視
    cfg = config_from_dict({"routing": {"workers": [
        {"name": "zeta", "windows": ["glm:5h"]},
        {"name": "alpha", "windows": ["codex:*"]},
    ], "worker_order": ["alpha"]}})
    assert list(cfg.routing.workers) == ["zeta", "alpha"]


def test_worker_dict_strip_duplicate_raises():
    # strip 後に重複する dict キー ("a" と " a ") も workers_duplicate で落とす
    with pytest.raises(ConfigError) as ei:
        config_from_dict({"routing": {"workers": {"a": ["glm:5h"], " a ": ["codex:*"]}}})
    assert "重複" in str(ei.value) or "duplicate" in str(ei.value)


def test_worker_array_bad_entry_raises():
    with pytest.raises(ConfigError):  # windows が無い
        config_from_dict({"routing": {"workers": [{"name": "a"}]}})
    with pytest.raises(ConfigError):  # オブジェクトですらない
        config_from_dict({"routing": {"workers": ["a"]}})
    with pytest.raises(ConfigError):  # 名前が規則外
        config_from_dict({"routing": {"workers": [
            {"name": "has space", "windows": ["glm:5h"]}]}})


def test_worker_array_duplicate_name_raises():
    with pytest.raises(ConfigError) as ei:
        config_from_dict({"routing": {"workers": [
            {"name": "a", "windows": ["glm:5h"]},
            {"name": "a", "windows": ["codex:*"]},
        ]}})
    assert "重複" in str(ei.value) or "duplicate" in str(ei.value)


def test_worker_array_saves_and_keeps_order(tmp_path):
    # TOML に保存して読み戻しても順番は保つ
    cfg = config_from_dict({"routing": {"workers": [
        {"name": "zeta", "windows": ["glm:5h"]},
        {"name": "alpha", "windows": ["codex:*"]},
    ]}})
    path = save_config(cfg, tmp_path / "config.toml")
    cfg2 = load_config(path)
    assert list(cfg2.routing.workers) == ["zeta", "alpha"]


def test_quoted_worker_and_panel_main_roundtrip(tmp_path):
    data = {"routing": {"workers": {"glm.fast": ["glm:5h"]}},
            "layout": {"panels": [{"main": 'glm:"quoted"', "size": "large"}]}}
    cfg = config_from_dict(data)
    path = save_config(cfg, tmp_path / "config.toml")
    restored = load_config(path)
    assert restored.routing.workers == {"glm.fast": ["glm:5h"]}
    assert restored.layout.panels[0].main == 'glm:"quoted"'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("key", ["floor_percent", "floor_hours", "short_over_margin_minutes"])
def test_routing_floats_reject_nonfinite(key, value):
    with pytest.raises(ConfigError):
        config_from_dict({"routing": {key: value}})


@pytest.mark.parametrize("section", ["night", "sleep"])
@pytest.mark.parametrize("value", ["23:00+09:00", "23:00Z", "2300", "23:00:00.1"])
def test_wall_times_reject_offsets_and_nonstandard_forms(section, value):
    times = {"start": value, "end": "08:00"}
    data = {"display": {"night": dict(times, brightness=10)}} if section == "night" else {"pacing": {"sleep": times}}
    with pytest.raises(ConfigError):
        config_from_dict(data)


def test_wall_time_seconds_survive_json_and_toml(tmp_path):
    data = {"display": {"night": {"start": "23:01:02", "end": "07:01:03", "brightness": 10}},
            "pacing": {"sleep": {"start": "00:01:04", "end": "08:01:05"}}}
    cfg = config_from_dict(data)
    restored = load_config(save_config(cfg, tmp_path / "config.toml"))
    assert config_to_dict(restored)["display"]["night"] == data["display"]["night"]
    assert config_to_dict(restored)["pacing"]["sleep"]["start"] == "00:01:04"
    assert config_to_dict(restored)["pacing"]["sleep"]["end"] == "08:01:05"
