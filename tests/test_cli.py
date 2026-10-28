"""CLI 単体テスト: detect --json / config --json / config --set-json / set-key。

ネットワーク・キーチェーンはモックし、実際の I/O は tmp_path 内だけ。
once の描画テストでは render もモックする。
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

from PIL import Image

from quotacanvas import cli, keychain, providers
from quotacanvas.config import load_config
from quotacanvas.models import ProviderUsage, Window
from quotacanvas.render import PanelSpec

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


def _run(monkeypatch, capsys, argv, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = cli.main(list(argv))
    out = capsys.readouterr().out
    return rc, out


# ------------------------------------------------------------- detect --json

def test_detect_json_output(monkeypatch, capsys):
    win = Window("codex", "weekly", "週間", 3.0, NOW + timedelta(days=7), timedelta(days=7))

    def detect_all(now=None):
        assert now is not None
        return [
            {"provider": "claude", "name": "Claude", "detected": True, "source": "s",
             "needs_key": False, "plan": "Max",
             "windows": [{"key": "5h", "label": "5時間"}, {"key": "weekly", "label": "週間"}],
             "error": None},
            {"provider": "glm", "name": "GLM (Z.ai)", "detected": False, "source": None,
             "needs_key": True, "plan": None, "windows": [], "error": "API キーが未設定"},
        ]

    monkeypatch.setattr(providers, "detect_all", detect_all)
    rc, out = _run(monkeypatch, capsys, ["detect", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert data[0]["provider"] == "claude" and data[0]["detected"] is True
    assert data[1] == {"provider": "glm", "name": "GLM (Z.ai)", "detected": False,
                       "source": None, "needs_key": True, "plan": None,
                       "windows": [], "error": "API キーが未設定"}


def test_detect_without_json_is_human_readable(monkeypatch, capsys):
    monkeypatch.setattr(providers, "detect_all", lambda now=None: [
        {"provider": "glm", "name": "GLM (Z.ai)", "detected": False, "source": None,
         "needs_key": True, "plan": None, "windows": [], "error": "API キーが未設定"},
    ])
    rc, out = _run(monkeypatch, capsys, ["detect"])
    assert rc == 0
    assert "GLM (Z.ai)" in out and "API キーが未設定" in out
    assert not out.lstrip().startswith("[")


# ------------------------------------------------------------ config --json

def test_config_json_matches_toml_shape(monkeypatch, capsys, tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[display]\npaused = true\ngauge = "used"\nwindows = ["claude:weekly"]\nbrightness = 70\n'
        '[providers.glm]\nenabled = false\nwindows = ["5h"]\n',
        encoding="utf-8",
    )
    rc, out = _run(monkeypatch, capsys, ["--config", str(cfg_path), "config", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert data["display"] == {"paused": True, "sleep_with_mac": True,
                               "gauge": "used", "percent_colors": True,
                               "separator": "", "language": "auto",
                               "windows": ["claude:weekly"], "brightness": 70}
    assert "night" not in data["display"]
    assert data["providers"]["glm"] == {"enabled": False, "windows": ["5h"]}
    assert data["providers"]["codex"] == {"enabled": True, "windows": [],
                                          "token_refresh": "cli"}  # W3: token_refresh 対応サービス


def test_config_json_error_is_json_with_nonzero_exit(monkeypatch, capsys, tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[poll]\ninterval = 5\n", encoding="utf-8")
    rc, out = _run(monkeypatch, capsys, ["--config", str(cfg_path), "config", "--json"])
    assert rc != 0
    assert "poll.interval" in json.loads(out)["error"]


# ------------------------------------------------------- config --set-json

FULL_CONFIG = {
    "display": {"paused": True, "gauge": "used", "windows": ["claude:weekly"],
                "brightness": 55,
                "night": {"start": "22:00", "end": "08:00", "brightness": 10}},
    "poll": {"interval": 300, "redraw": 30},
    "pacing": {"tight": 90, "on_track": 60},
    "providers": {"claude": {"enabled": False, "windows": ["weekly"]},
                  "codex": {"enabled": True, "windows": []},
                  "glm": {"enabled": True, "windows": ["5h", "weekly"]}},
}


def test_config_set_json_saves_and_echoes(monkeypatch, capsys, tmp_path):
    target = tmp_path / "out" / "config.toml"
    rc, out = _run(monkeypatch, capsys,
                   ["--config", str(target), "config", "--set-json"],
                   stdin=json.dumps(FULL_CONFIG, ensure_ascii=False))
    assert rc == 0
    echoed = json.loads(out)
    assert echoed["display"]["paused"] is True
    assert echoed["providers"]["glm"]["windows"] == ["5h", "weekly"]

    cfg = load_config(target)  # 保存された TOML はそのまま読み戻せる
    assert cfg.display.paused is True and cfg.display.gauge == "used"
    assert cfg.display.brightness == 55
    assert cfg.providers.windows == {"claude": ["weekly"], "glm": ["5h", "weekly"]}
    assert cfg.poll.interval == 300 and cfg.poll.redraw == 30
    assert not target.with_name("config.toml.tmp").exists()  # 原子的な書き込み


def test_config_set_json_invalid_value(monkeypatch, capsys, tmp_path):
    target = tmp_path / "config.toml"
    rc, out = _run(monkeypatch, capsys,
                   ["--config", str(target), "config", "--set-json"],
                   stdin='{"display": {"gauge": "weird"}}')
    assert rc != 0
    assert json.loads(out)["error"]
    assert not target.exists()  # 失敗時は保存しない


def test_config_set_json_broken_json(monkeypatch, capsys, tmp_path):
    rc, out = _run(monkeypatch, capsys,
                   ["--config", str(tmp_path / "c.toml"), "config", "--set-json"],
                   stdin='{"display": ')
    assert rc != 0
    assert json.loads(out)["error"]


def test_config_set_json_not_an_object(monkeypatch, capsys, tmp_path):
    rc, out = _run(monkeypatch, capsys,
                   ["--config", str(tmp_path / "c.toml"), "config", "--set-json"],
                   stdin="[1, 2]")
    assert rc != 0
    assert json.loads(out)["error"]


# ------------------------------------------- once: render への layout / colors

def _patch_fetch(monkeypatch):
    """providers.fetch_all をモックし、claude / codex / glm の利用枠を返す。
    claude と codex は複数の窓があり、subs 省略パネルの自動サブを確かめられる。"""
    wins = [
        Window("claude", "weekly", "週間", 30.0, NOW + timedelta(days=5), timedelta(days=7)),
        Window("claude", "5h", "5時間", 10.0, NOW + timedelta(hours=4), timedelta(hours=5)),
        Window("codex", "weekly", "週間", 20.0, NOW + timedelta(days=6), timedelta(days=7)),
        Window("codex", "5h", "5時間", 50.0, NOW + timedelta(hours=3), timedelta(hours=5)),
        Window("glm", "5h", "5時間", 40.0, NOW + timedelta(hours=4), timedelta(hours=5)),
    ]
    usages = [
        ProviderUsage("claude", "Claude Max", [wins[0], wins[1]], None, NOW),
        ProviderUsage("codex", "Codex", [wins[2], wins[3]], None, NOW),
        ProviderUsage("glm", "GLM", [wins[4]], None, NOW),
    ]
    monkeypatch.setattr(providers, "fetch_all", lambda now=None, enabled=None: list(usages))
    return usages


def _run_once(monkeypatch, tmp_path, config_text=""):
    """once を --no-screen で実行し、render に渡された引数を記録して返す。"""
    calls = {}

    def fake_render(cards, now, **kwargs):
        calls["cards"] = cards
        calls["kwargs"] = kwargs
        return Image.new("RGB", (1920, 462), (11, 13, 18))

    monkeypatch.setattr(cli, "render", fake_render)
    # 既定 config を読まない: 実環境の ~/.config/quota-canvas/config.toml に
    # layout.panels 等があっても影響されないよう、必ず tmp の設定ファイルを渡す。
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(config_text, encoding="utf-8")
    argv = ["--config", str(cfg_path), "--no-screen",
            "--history", str(tmp_path / "history.jsonl"), "once"]
    rc = cli.main(argv)
    return rc, calls


def test_once_passes_layout_and_colors_to_render(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch)
    config_text = (
        '[[layout.panels]]\n'
        'main = "claude:weekly"\n'
        'subs = ["glm:5h"]\n'
        'size = "large"\n'
        '[[layout.panels]]\n'
        'main = "claude:5h"\n'
        'size = "small"\n'
        '[[layout.panels]]\n'
        'main = "codex:weekly"\n'
        'size = "large"\n'
        '[providers.glm]\n'
        'color = "#123ABC"\n'
    )
    rc, calls = _run_once(monkeypatch, tmp_path, config_text)
    assert rc == 0
    kw = calls["kwargs"]
    # config の panels が render.PanelSpec に変換されて渡る。
    # subs を省略した codex パネルには同じサービスの別窓 (codex:5h) が
    # 自動サブとして付き、small パネルには付かない (自動配置と同じ規則)。
    assert kw["layout"] == [
        PanelSpec(main="claude:weekly", subs=["glm:5h"], size="large"),
        PanelSpec(main="claude:5h", subs=[], size="small"),
        PanelSpec(main="codex:weekly", subs=["codex:5h"], size="large"),
    ]
    # color 指定のある provider だけ渡る
    assert kw["colors"] == {"glm": "#123ABC"}
    assert kw["gauge"] == "remaining"


def test_once_without_layout_config_passes_none_and_empty_colors(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch)
    rc, calls = _run_once(monkeypatch, tmp_path)  # 配置も色も未指定
    assert rc == 0
    kw = calls["kwargs"]
    assert kw["layout"] is None  # None = render 側の自動配置
    assert kw["colors"] == {}


def test_once_ignores_layout_when_render_lacks_panelspec(monkeypatch, tmp_path):
    # render 側がまだ PanelSpec を持たない場合: layout=None で自動配置の互換描画
    import quotacanvas.render as render_mod
    monkeypatch.delattr(render_mod, "PanelSpec")
    _patch_fetch(monkeypatch)
    config_text = '[[layout.panels]]\nmain = "claude:weekly"\n'
    rc, calls = _run_once(monkeypatch, tmp_path, config_text)
    assert rc == 0
    assert calls["kwargs"]["layout"] is None


# ------------------------------------------- _output: 送信失敗時の開き直し

class _FakeScreen:
    """turzx.Screen の置き換え: show の戻り値 (False か例外) を差し替えられる。"""

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def show(self, img):
        self.calls.append(("show", img.size))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def brightness(self, value):
        self.calls.append(("brightness", value))


def test_output_reopens_and_resends_when_show_returns_false(monkeypatch):
    old, new = _FakeScreen(result=False), _FakeScreen()
    monkeypatch.setattr(cli, "_open_screen", lambda: new)
    img = Image.new("RGB", (8, 8))
    assert cli._output(img, old, None) is new     # 新しい screen を返す
    assert old.calls == [("show", (8, 8))]        # 旧画面には 1 回だけ送る
    assert new.calls == [("show", (8, 8))]        # 開き直した画面に送り直す


def test_output_reopens_when_show_raises(monkeypatch):
    old = _FakeScreen(result=RuntimeError("USB unplugged"))
    new = _FakeScreen()
    monkeypatch.setattr(cli, "_open_screen", lambda: new)
    assert cli._output(Image.new("RGB", (8, 8)), old, None) is new
    assert new.calls == [("show", (8, 8))]


def test_output_returns_none_when_reopen_fails(monkeypatch):
    old = _FakeScreen(result=False)
    monkeypatch.setattr(cli, "_open_screen", lambda: None)
    assert cli._output(Image.new("RGB", (8, 8)), old, None) is None
    assert old.calls == [("show", (8, 8))]        # 送り直しは開き直した先だけ


def test_output_keeps_screen_when_send_succeeds(monkeypatch):
    screen = _FakeScreen()

    def fail():
        raise AssertionError("送信に成功したのに画面を開き直した")

    monkeypatch.setattr(cli, "_open_screen", fail)
    img = Image.new("RGB", (8, 8))
    assert cli._output(img, screen, None) is screen  # 同じ screen を返す
    assert screen.calls == [("show", (8, 8))]        # 開き直さず 1 回だけ送る


# ---------------------------------------------------------------- set-key

def test_set_key_reads_stdin_and_stores(monkeypatch, capsys):
    stored = {}

    def fake_write(account, value):
        stored[account] = value

    monkeypatch.setattr(keychain, "write", fake_write)
    rc, out = _run(monkeypatch, capsys, ["set-key", "glm"], stdin="  zai-secret-1  \n")
    assert rc == 0
    assert stored == {"glm": "zai-secret-1"}  # 前後の空白は除かれる
    assert "zai-secret-1" not in out  # 値が出力に現れない


def test_set_key_json_output(monkeypatch, capsys):
    monkeypatch.setattr(keychain, "write", lambda account, value: None)
    rc, out = _run(monkeypatch, capsys, ["set-key", "glm", "--json"], stdin="k\n")
    assert rc == 0
    assert json.loads(out) == {"provider": "glm", "stored": True}


def test_set_key_delete(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(keychain, "delete", lambda account: calls.append(account) or True)
    rc, out = _run(monkeypatch, capsys, ["set-key", "glm", "--delete", "--json"])
    assert rc == 0
    assert calls == ["glm"]
    assert json.loads(out) == {"provider": "glm", "deleted": True}


def test_set_key_unknown_provider(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, ["set-key", "nope"], stdin="k\n")
    assert rc != 0
    assert json.loads(out)["error"]


def test_set_key_rejects_services_without_key(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("キー不要のサービスに書いてはいけない")

    monkeypatch.setattr(keychain, "write", boom)
    rc, out = _run(monkeypatch, capsys, ["set-key", "claude"], stdin="k\n")
    assert rc != 0
    assert json.loads(out)["error"]


def test_set_key_empty_input(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("空のキーを保存してはいけない")

    monkeypatch.setattr(keychain, "write", boom)
    rc, out = _run(monkeypatch, capsys, ["set-key", "glm"], stdin="\n")
    assert rc != 0
    assert json.loads(out)["error"]


def test_set_key_keychain_error_becomes_json_error(monkeypatch, capsys):
    from quotacanvas.keychain import KeychainError

    def failing(account, value):
        raise KeychainError("キーチェーンに保存できませんでした")

    monkeypatch.setattr(keychain, "write", failing)
    rc, out = _run(monkeypatch, capsys, ["set-key", "glm"], stdin="k\n")
    assert rc != 0
    assert json.loads(out)["error"] == "キーチェーンに保存できませんでした"

def test_render_layout_fills_omitted_subs_from_same_service():
    from datetime import timedelta
    from quotacanvas.cli import _render_layout
    from quotacanvas.config import Config, Panel
    from quotacanvas.models import Pace, PaceStatus, ProviderUsage, Window

    def card(provider, key):
        w = Window(provider, key, key, 10.0, None, timedelta(days=7))
        p = Pace(w, PaceStatus.UNKNOWN, 0.0, 0.0, None, None, None, None, None)
        return ProviderUsage(provider, provider, [w]), p

    cards = [card("claude", "weekly"), card("claude", "weekly_fable"), card("claude", "5h"),
             card("codex", "weekly")]
    cfg = Config()
    cfg.layout.panels = [
        Panel(main="claude:weekly"),                        # subs omitted -> automatic
        Panel(main="claude:5h", size="small"),              # used as a main elsewhere
        Panel(main="codex:weekly", subs=[], size="small"),  # explicitly none
    ]
    specs = _render_layout(cfg, cards)
    assert specs[0].subs == ["claude:weekly_fable"]  # 5h is its own panel, so not repeated
    assert specs[2].subs == []


def test_render_layout_passes_through_empty_small_slot():
    """main = "" の small パネル (空き枠) はそのまま PanelSpec になり、
    status.json でも列構成を復元できる。"""
    from quotacanvas.cli import _render_layout
    from quotacanvas.config import Config, Panel

    cfg = Config()
    cfg.layout.panels = [
        Panel(main="", size="small"),               # 上段空き
        Panel(main="codex:weekly", size="small"),   # 下段
        Panel(main="", size="small"),               # 末尾の空き列
    ]
    specs = _render_layout(cfg, [])
    assert [(sp.main, sp.subs, sp.size) for sp in specs] == [
        ("", [], "small"),
        ("codex:weekly", [], "small"),
        ("", [], "small"),
    ]


def test_render_layout_auto_subs_shared_capped_and_small_empty():
    """subs 省略の自動サブは自動配置と同じ規則: 大パネル間で重複せず最大 3 件、
    small パネルには付かず、明示 subs の窓は自動側には出ない。"""
    from quotacanvas.cli import _render_layout
    from quotacanvas.config import Config, Panel
    from quotacanvas.models import Pace, PaceStatus

    def card(provider, key):
        w = Window(provider, key, key, 10.0, None, timedelta(days=7))
        p = Pace(w, PaceStatus.UNKNOWN, 0.0, 0.0, None, None, None, None, None)
        return ProviderUsage(provider, provider, [w]), p

    cards = [card("claude", k) for k in
             ("weekly", "weekly_fable", "5h", "7h", "2h", "3h", "4h", "1h", "opus")]
    cards.append(card("codex", "weekly"))
    cfg = Config()
    cfg.layout.panels = [
        Panel(main="claude:weekly", subs=["claude:opus"]),  # 明示 subs (自動側からは外れる)
        Panel(main="claude:weekly_fable"),                  # 大パネル, subs 省略
        Panel(main="claude:1h", size="small"),              # small, subs 省略
        Panel(main="claude:4h"),                            # 大パネル, subs 省略
        Panel(main="codex:weekly"),                         # 同サービスの別窓なし
    ]
    specs = _render_layout(cfg, cards)
    assert specs[0].subs == ["claude:opus"]
    # main も明示 subs も除いた未使用 claude 窓は 4 件 (5h, 7h, 2h, 3h)
    # あるが 3 件で切る。opus は別パネルの明示 subs なので出ない。
    assert specs[1].subs == ["claude:5h", "claude:7h", "claude:2h"]
    assert specs[2].subs == []                       # small パネルは自動サブなし
    assert specs[3].subs == ["claude:3h"]            # 先行パネルと重複しない
    assert specs[4].subs == []                       # 同じサービスの別窓が無い


def test_render_layout_explicit_subs_are_capped_at_three():
    """明示 subs も status.json を描画側にそろえるため 3 件で切る。"""
    from quotacanvas.cli import _render_layout
    from quotacanvas.config import Config, Panel
    from quotacanvas.models import Pace, PaceStatus

    def card(provider, key):
        w = Window(provider, key, key, 10.0, None, timedelta(days=7))
        p = Pace(w, PaceStatus.UNKNOWN, 0.0, 0.0, None, None, None, None, None)
        return ProviderUsage(provider, provider, [w]), p

    cards = [card("claude", k) for k in ("weekly", "5h", "7h", "2h", "1h")]
    cfg = Config()
    cfg.layout.panels = [Panel(main="claude:weekly",
                               subs=["claude:5h", "claude:7h", "claude:2h", "claude:1h"])]
    specs = _render_layout(cfg, cards)
    assert specs[0].subs == ["claude:5h", "claude:7h", "claude:2h"]


def test_config_set_json_accepts_sleep_with_mac(monkeypatch, capsys, tmp_path):
    target = tmp_path / "config.toml"
    payload = {"display": {"paused": False, "sleep_with_mac": False}}
    rc, out = _run(monkeypatch, capsys,
                   ["--config", str(target), "config", "--set-json"],
                   stdin=json.dumps(payload))
    assert rc == 0
    assert json.loads(out)["display"]["sleep_with_mac"] is False
    cfg = load_config(target)  # 保存された TOML はそのまま読み戻せる
    assert cfg.display.sleep_with_mac is False and cfg.display.paused is False


# ------------------------------------------- display.language: en 設定で英語になる

def test_english_config_gives_english_cli_message(monkeypatch, capsys, tmp_path):
    """display.language = "en" の設定で、CLI がユーザーに見せる文言が英語になる。"""
    from quotacanvas import i18n

    captured = {}
    monkeypatch.setattr(cli, "render_error",
                        lambda msg, now, scale=1.0: captured.setdefault("msg", msg)
                        or Image.new("RGB", (1920, 462)))
    monkeypatch.setattr(providers, "fetch_all", lambda now=None, enabled=None: [])
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[display]\nlanguage = "en"\n', encoding="utf-8")
    rc = cli.main(["--config", str(cfg_path), "--no-screen",
                   "--history", str(tmp_path / "history.jsonl"), "once"])
    assert rc == 1  # 取得できずエラーパネル
    assert captured["msg"] == "Could not fetch usage"
    assert captured["msg"] != "利用枠を取得できませんでした"
    assert i18n.current_language() == "en"


# ------------------------------------------- build_cards: 前回の期間 (F3)

def test_build_cards_fills_previous_used(tmp_path):
    from quotacanvas.history import History

    history = History(tmp_path / "history.jsonl")
    r1 = NOW + timedelta(days=7)
    r2 = r1 + timedelta(days=7)
    history.record([Window("claude", "weekly", "週間", 64.0, r1, timedelta(days=7))],
                   now=NOW - timedelta(days=7))
    history.record([Window("claude", "weekly", "週間", 2.0, r2, timedelta(days=7))], now=NOW)
    usage = ProviderUsage("claude", "Claude Max",
                          [Window("claude", "weekly", "週間", 2.0, r2, timedelta(days=7))],
                          None, NOW)
    cards = cli.build_cards([usage], history, NOW)
    assert cards[0][1].previous_used == 64.0


def test_build_cards_leaves_previous_used_none_without_periods(tmp_path):
    from quotacanvas.history import History

    history = History(tmp_path / "history.jsonl")
    usage = ProviderUsage("claude", "Claude Max",
                          [Window("claude", "weekly", "週間", 2.0,
                                  NOW + timedelta(days=7), timedelta(days=7))],
                          None, NOW)
    cards = cli.build_cards([usage], history, NOW)
    assert cards[0][1].previous_used is None


# ------------------------------------------- once: 推奨行 (F1) を render へ

def _run_once_capture(monkeypatch, tmp_path, config_text="", fetch=None):
    """once を実行し、render に渡された引数 (recommendation 込み) を記録して返す。"""
    calls = {}

    def fake_render(cards, now, recommendation=None, **kwargs):
        calls["cards"] = cards
        calls["kwargs"] = kwargs
        calls["recommendation"] = recommendation
        return Image.new("RGB", (1920, 462), (11, 13, 18))

    monkeypatch.setattr(cli, "render", fake_render)
    if fetch is not None:
        monkeypatch.setattr(providers, "fetch_all", fetch)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(config_text, encoding="utf-8")
    argv = ["--config", str(cfg_path), "--no-screen",
            "--history", str(tmp_path / "history.jsonl"), "once"]
    return cli.main(argv), calls


def test_once_passes_recommendation_text_to_render(monkeypatch, tmp_path):
    # claude だけが使える (codex 週間は OVER): 推奨は claude、代表窓 = 週間。
    # 窓の resets_at は実時刻から作る (リセット待ち判定は実時刻との比較のため)
    base = datetime.now(timezone.utc)

    def usable(now=None, enabled=None):
        return [ProviderUsage("claude", "Claude Max", [
                    Window("claude", "weekly", "週間", 10.0,
                           base + timedelta(days=6), timedelta(days=7)),
                    Window("claude", "5h", "5時間", 10.0,
                           base + timedelta(hours=4), timedelta(hours=5))], None, now),
                ProviderUsage("codex", "Codex", [
                    Window("codex", "weekly", "週間", 80.0,
                           base + timedelta(days=4), timedelta(days=7))], None, now)]

    rc, calls = _run_once_capture(monkeypatch, tmp_path,
                                  '[display]\nlanguage = "ja"\n', fetch=usable)
    assert rc == 0
    assert calls["kwargs"]["gauge"] == "remaining"  # 従来の引数はそのまま
    rec = calls["recommendation"]
    assert isinstance(rec, str)
    assert rec.startswith("Claude Max を使う · 週間の残り 90% · リセットまで ")
    assert [c[1].window.id for c in calls["cards"]][:1] == ["claude:weekly"]


def test_once_no_render_call_when_no_usage(monkeypatch, tmp_path):
    rc, calls = _run_once_capture(
        monkeypatch, tmp_path, '[display]\nlanguage = "ja"\n',
        fetch=lambda now=None, enabled=None: [])
    assert rc == 1  # カード無し -> エラーパネル (render は呼ばれない)
    assert calls == {}


def test_once_recommendation_none_when_all_unusable(monkeypatch, tmp_path):
    base = datetime.now(timezone.utc)

    def low_only(now=None, enabled=None):
        win = Window("glm", "5h", "5時間", 96.0,
                     base + timedelta(hours=5), timedelta(hours=5))
        return [ProviderUsage("glm", "GLM", [win], None, now)]

    rc, calls = _run_once_capture(monkeypatch, tmp_path,
                                  '[display]\nlanguage = "ja"\n', fetch=low_only)
    assert rc == 0
    assert calls["recommendation"] is None  # 残り 4% (< 5%) で使えないので推奨なし


def test_set_json_invalid_serialization_preserves_existing(monkeypatch, capsys, tmp_path):
    from quotacanvas import config
    target = tmp_path / "config.toml"
    target.write_text('[display]\nbrightness = 44\n', encoding="utf-8")
    before = target.read_bytes()
    monkeypatch.setattr(config, "format_toml", lambda cfg: '[routing.workers]\nglm.fast = ["glm:5h"]\n')
    rc, out = _run(monkeypatch, capsys, ["--config", str(target), "config", "--set-json"], stdin="{}")
    assert rc != 0 and json.loads(out)["error"]
    assert target.read_bytes() == before
    assert not target.with_name("config.toml.tmp").exists()


def test_pick_fresh_status_without_recommendation_does_not_fetch(monkeypatch, capsys, tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setattr(cli, "STATUS_PATH", status_path)
    def fail_fetch(*args, **kwargs):
        raise AssertionError("fresh status should not fetch")
    monkeypatch.setattr(cli.Poller, "fetch", fail_fetch)
    rc, out = _run(monkeypatch, capsys, ["--config", str(tmp_path / "config.toml"), "pick", "--json"])
    assert rc == 0 and json.loads(out) == {"recommendation": None}


def test_set_json_roundtrips_quoted_values(monkeypatch, capsys, tmp_path):
    target = tmp_path / "config.toml"
    data = {"routing": {"workers": {"glm.fast": ["glm:5h"]}},
            "layout": {"panels": [{"main": 'glm:"quoted"', "size": "large"}]}}
    rc, out = _run(monkeypatch, capsys, ["--config", str(target), "config", "--set-json"], stdin=json.dumps(data))
    assert rc == 0 and json.loads(out)["routing"]["workers"] == data["routing"]["workers"]
    cfg = load_config(target)
    assert cfg.routing.workers == data["routing"]["workers"]
    assert cfg.layout.panels[0].main == 'glm:"quoted"'
