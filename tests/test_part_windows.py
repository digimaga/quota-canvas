"""内訳窓 (part_of 付き) の扱いに関する cli / status のテスト。

- Poller は内訳窓の履歴を取らない (親窓だけ history に入る)。
- build_cards は内訳窓を自分の key/id ではなく「親窓が残るか」で残し、
  並び順も親の直後に揃える。
- status.json の cards には内訳窓を出さない (メニューバーアプリが別カード
  として表示しない)。
- cli._render_layout の自動 subs にも内訳が入る (表示のサブ行の元)。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quotacanvas import providers
from quotacanvas.cli import Poller, build_cards, _render_layout
from quotacanvas.config import Config, Panel
from quotacanvas.history import History
from quotacanvas.models import Pace, PaceStatus, ProviderUsage, Window
from quotacanvas.status import build_status

NOW = datetime(2026, 10, 25, 9, 0, 0, tzinfo=timezone.utc)


def _grok_usage(used=22.0, parts=(("Build", 20.0), ("Tasks", 2.0))):
    reset = NOW + timedelta(days=5)
    main = Window("grok", "weekly", "週間", used, reset, timedelta(days=7))
    windows = [main]
    for name, pct in parts:
        windows.append(Window("grok", "weekly_" + name.lower(),
                              "週間 " + name, pct, reset, timedelta(days=7),
                              part_of="weekly"))
    return ProviderUsage("grok", "Grok", windows, None, NOW)


def _cards(usages, history=None, **kwargs):
    return build_cards(usages, history or History(), NOW, **kwargs)


# ------------------------------------------------------- Poller: 履歴は親だけ

def test_poller_records_only_parent_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(
        providers, "fetch_all",
        lambda now=None, enabled=None: [_grok_usage(used=30.0)])
    poller = Poller(History(tmp_path / "history.jsonl"), enabled={"grok"})
    poller.fetch(NOW)
    history = poller.history
    assert len(history.samples("grok:weekly")) == 1      # 親は記録する
    assert history.samples("grok:weekly_build") == []    # 内訳は記録しない
    assert history.samples("grok:weekly_tasks") == []

    # 次の取得で値が変わっても内訳の履歴は増えない
    monkeypatch.setattr(
        providers, "fetch_all",
        lambda now=None, enabled=None: [_grok_usage(used=55.0)])
    poller.fetch(NOW + timedelta(minutes=15))
    assert len(history.samples("grok:weekly")) == 2
    assert history.samples("grok:weekly_build") == []


# ------------------------------------------------------------ build_cards

def test_build_cards_orders_parts_right_after_parent():
    cards = _cards([_grok_usage()])
    assert [pace.window.id for _, pace in cards] == \
        ["grok:weekly", "grok:weekly_build", "grok:weekly_tasks"]


def test_build_cards_parts_follow_parent_filters():
    # providers.<p>.windows は親の key で判定する (内訳の key は見ない)
    cards = _cards([_grok_usage()],
                   provider_windows={"grok": {"weekly"}})
    assert [pace.window.id for _, pace in cards] == \
        ["grok:weekly", "grok:weekly_build", "grok:weekly_tasks"]

    # 親がフィルタで落ちると内訳も落ちる (内訳だけの指定では残らない)
    cards = _cards([_grok_usage()],
                   provider_windows={"grok": {"weekly_build"}})
    assert cards == []

    # display.windows (--only / display.windows) も親の id で判定する
    cards = _cards([_grok_usage()], order=["grok:weekly"])
    assert [pace.window.id for _, pace in cards] == \
        ["grok:weekly", "grok:weekly_build", "grok:weekly_tasks"]
    assert _cards([_grok_usage()], order=["grok:weekly_tasks"]) == []

    # 親窓が API 応答に無い内訳は出さない
    usage = _grok_usage()
    orphans = [w for w in usage.windows if w.part_of is not None]
    usage.windows = orphans              # 親を取り除く
    assert _cards([usage]) == []


def test_build_cards_part_never_before_its_parent():
    """並び順キーは親 > 内訳: pos での位置も親に合わせ、同位置なら親が先。"""
    usage = _grok_usage()
    cards = _cards([usage], order=["grok:weekly_tasks", "grok:weekly"])
    ids = [pace.window.id for _, pace in cards]
    assert ids.index("grok:weekly") < ids.index("grok:weekly_tasks")
    assert ids.index("grok:weekly") < ids.index("grok:weekly_build")


# -------------------------------------------------------------- status.json

def test_status_cards_exclude_part_windows():
    from quotacanvas.config import load_config
    cfg = load_config(None)
    usage = _grok_usage()
    paces = [(usage, Pace(usage.windows[0], PaceStatus.TIGHT, 2.0 / 7.0,
                          200.0 / 7.0, 13.5, 94.0, None, 9.0, -33.0, []))]
    for w in usage.windows[1:]:
        paces.append((usage, Pace(w, PaceStatus.UNKNOWN, 0.0, 0.0, None,
                                  None, None, None, None, [])))
    status = build_status(cfg, paces, [usage], NOW)
    assert [c["id"] for c in status["cards"]] == ["grok:weekly"]
    # layout (自動配置) の subs に内訳 id が入ることはある: アプリは未知の id
    # も文字列として表示できる (Models.swift の try? デコードと生 id 表示)。
    subs = [s for e in status["layout"] for s in e["subs"]]
    assert "grok:weekly_build" in subs


# --------------------------------------------------- cli._render_layout

def test_render_layout_auto_subs_include_parts():
    """subs 省略の自動サブにも同じサービスの内訳が入る (render のサブ行)。"""
    cards = _cards([_grok_usage()])
    cfg = Config()
    cfg.layout.panels = [Panel(main="grok:weekly")]   # subs 省略 = 自動
    specs = _render_layout(cfg, cards)
    assert specs[0].subs == ["grok:weekly_build", "grok:weekly_tasks"]
