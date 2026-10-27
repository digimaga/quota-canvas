"""取得失敗中も last good データを表示し続ける (stale_error) のテスト。

経過時間に関係なく失敗中は last_good を返し、失敗理由を stale_error として
添える (last_good 本体・last-good.json は汚さない)。render は stale_error
付きのカード (大きいカード / small パネル) の右上に警告色で描く。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from quotacanvas import providers
from quotacanvas.cli import Poller, build_cards
from quotacanvas.history import History
from quotacanvas.lastgood import LastGood
from quotacanvas.models import ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas import render as render_mod
from quotacanvas.render import PanelSpec, render
from quotacanvas.config import load_config
from quotacanvas.status import build_status

NOW = datetime(2026, 10, 25, 9, 0, 0, tzinfo=timezone.utc)
NOW_L = NOW.astimezone()
LAST_GOOD = "last-good.json"
OLD = NOW - timedelta(hours=5)          # 1 時間の期限をとっくに過ぎた時刻


def _win(provider, key, used=10.0, fetched_for=None):
    return Window(provider, key, key, used,
                  (fetched_for or NOW) + timedelta(days=1), timedelta(days=7))


def _usage(pid, windows=None, error=None, fetched_at=NOW, retry_after=None):
    return ProviderUsage(pid, pid.title(), list(windows or []), error,
                         fetched_at, [], retry_after)


class FakeFetch:
    """providers.fetch_all を差し替える (test_poller.py と同じ型)。"""

    def __init__(self, monkeypatch, behavior):
        self.calls = []
        self.behavior = behavior

        def fake(now=None, enabled=None):
            pids = list(providers.REGISTRY) if enabled is None \
                else [p for p in providers.REGISTRY if p in enabled]
            for pid in pids:
                self.calls.append((pid, now))
            return [self.behavior(pid, now) for pid in pids]

        monkeypatch.setattr(providers, "fetch_all", fake)

    def times_called(self, pid):
        return sum(1 for c in self.calls if c[0] == pid)


def _poller(tmp_path, **kwargs):
    kwargs.setdefault("enabled", {"claude"})
    return Poller(History(tmp_path / "history.jsonl"),
                  last_good_path=tmp_path / LAST_GOOD, **kwargs)


# ------------------------------------------------- Poller: last_good を使う

def test_failure_uses_old_last_good_with_stale_error(tmp_path, monkeypatch):
    # (a) 1 時間以上前の last_good でも失敗時にはそれを返し stale_error が付く
    good = _usage("claude", [_win("claude", "weekly", 30.0)],
                  fetched_at=OLD)
    LastGood(tmp_path / LAST_GOOD).set(good)
    FakeFetch(monkeypatch,
              lambda pid, now: _usage(pid, error="HTTP 500", fetched_at=now))
    poller = _poller(tmp_path)
    result = poller.fetch(NOW)
    assert result[0].error is None
    assert result[0].windows == good.windows
    assert result[0].fetched_at == OLD
    assert result[0].stale_error == "HTTP 500"
    # last_good 本体も last-good.json にも stale_error は入らない
    assert poller.last_good["claude"].stale_error is None
    on_disk = json.loads((tmp_path / LAST_GOOD).read_text(encoding="utf-8"))
    assert "stale_error" not in on_disk["claude"]


def test_429_backoff_uses_old_last_good_with_stale_error(tmp_path, monkeypatch):
    # (b) 429 バックオフ中 (_deferred_usage) も last_good を stale_error 付きで
    good = _usage("claude", [_win("claude", "weekly", 30.0)], fetched_at=OLD)
    LastGood(tmp_path / LAST_GOOD).set(good)
    fake = FakeFetch(monkeypatch, lambda pid, now:
                     _usage(pid, error="HTTP 429", fetched_at=now))
    poller = _poller(tmp_path)
    result = poller.fetch(NOW)                  # 429 -> 5 分のバックオフ開始
    assert result[0].stale_error == "HTTP 429"
    fake.calls.clear()
    later = NOW + timedelta(minutes=1)
    result = poller.fetch(later)                # バックオフ中: 取りに行かない
    assert fake.times_called("claude") == 0
    assert result[0].error is None
    assert result[0].windows == good.windows
    assert result[0].fetched_at == OLD
    assert result[0].stale_error == "HTTP 429"


def test_without_last_good_the_error_is_shown(tmp_path, monkeypatch):
    # (c) last_good が無ければ従来どおりエラーのまま
    fake = FakeFetch(monkeypatch, lambda pid, now:
                     _usage(pid, error="HTTP 503", fetched_at=now))
    poller = _poller(tmp_path)
    result = poller.fetch(NOW)
    assert result[0].error == "HTTP 503"
    assert result[0].stale_error is None
    fake.calls.clear()
    result = poller.fetch(NOW + timedelta(minutes=1))   # 429 でなくても変わらず
    assert result[0].error == "HTTP 503"
    assert result[0].stale_error is None


def test_success_clears_stale_error(tmp_path, monkeypatch):
    # (d) 成功したら stale_error は消える (last_good も更新される)
    LastGood(tmp_path / LAST_GOOD).set(
        _usage("claude", [_win("claude", "weekly", 30.0)], fetched_at=OLD))
    state = {"ok": False}

    def behavior(pid, now):
        if state["ok"]:
            return _usage(pid, [_win(pid, "weekly", 55.0)], fetched_at=now)
        return _usage(pid, error="HTTP 500", fetched_at=now)

    FakeFetch(monkeypatch, behavior)
    poller = _poller(tmp_path)
    assert poller.fetch(NOW)[0].stale_error == "HTTP 500"
    state["ok"] = True
    result = poller.fetch(NOW + timedelta(minutes=1))
    assert result[0].error is None
    assert result[0].stale_error is None
    assert result[0].windows[0].used_percent == 55.0
    assert poller.last_good["claude"].fetched_at == NOW + timedelta(minutes=1)


# ------------------------------------------------------------ render / status

def _stale_cards(provider="claude", keys=("weekly", "5h")):
    """stale_error 付きの取得成功カード (窓 2 つ = main + サブ行)。"""
    wins = []
    for i, key in enumerate(keys):
        length = timedelta(days=7) if key == "weekly" else timedelta(hours=5)
        wins.append(Window(provider, key, "週間" if key == "weekly" else "5時間",
                           40.0 + i * 10, NOW + timedelta(days=3), length))
    pu = ProviderUsage(provider, provider.title(), wins, None, OLD,
                       stale_error="HTTP 429")
    return [(pu, compute_pace(w, [], NOW)) for w in wins]


def _spy_stale_mark(monkeypatch):
    marks = []
    orig = render_mod._draw_stale_mark

    def spy(s, x, y, stale, max_w):
        marks.append(stale)
        orig(s, x, y, stale, max_w)

    monkeypatch.setattr(render_mod, "_draw_stale_mark", spy)
    return marks


def test_render_large_card_with_stale_error(monkeypatch):
    # (e) 大きいカード: サブ行があってもカード単位で 1 回、stale 情報が届く
    marks = _spy_stale_mark(monkeypatch)
    cards = _stale_cards()
    img = render(cards, NOW_L,
                 layout=[PanelSpec("claude:weekly", subs=["claude:5h"])])
    assert img.size == (1920, 462)
    assert marks == [("HTTP 429", OLD)]


def test_render_small_panel_with_stale_error(monkeypatch):
    # (e) small パネルも同じく 1 回
    marks = _spy_stale_mark(monkeypatch)
    img = render(_stale_cards(), NOW_L,
                 layout=[PanelSpec("claude:weekly", size="small")])
    assert img.size == (1920, 462)
    assert marks == [("HTTP 429", OLD)]


def test_render_large_and_small_together_with_stale_error(monkeypatch):
    # 大きいカード 1 枚 + small パネルを 1 枚の 1920x462 に同時に出せる
    marks = _spy_stale_mark(monkeypatch)
    img = render(_stale_cards(), NOW_L,
                 layout=[PanelSpec("claude:weekly"),
                         PanelSpec("claude:weekly", size="small")])
    assert img.size == (1920, 462)
    assert len(marks) == 2


def test_render_healthy_card_has_no_stale_mark(monkeypatch):
    marks = _spy_stale_mark(monkeypatch)
    pu = ProviderUsage("claude", "Claude Max", [_win("claude", "weekly")],
                       None, NOW)
    render([(pu, compute_pace(pu.windows[0], [], NOW))], NOW_L,
           layout=[PanelSpec("claude:weekly")])
    assert marks == []


def test_stale_text_format():
    # HH:MM は fetched_at のローカル時刻 (両辺で同じ変換を比較し書式だけ見る)
    l = OLD.astimezone()
    assert render_mod._stale_text(OLD) == "取得失敗中 %02d:%02d 時点" % (l.hour, l.minute)
    assert render_mod._stale_text(None) == "取得失敗中"


def test_build_status_reports_stale_error(tmp_path):
    # status.json のカードに stale_error を出す (成功時はキーごと省略)
    cfg = load_config(tmp_path / "config.toml")
    stale = _stale_cards()
    status = build_status(cfg, stale, [stale[0][0]], NOW)
    assert [c.get("stale_error") for c in status["cards"]] == ["HTTP 429", "HTTP 429"]

    pu = ProviderUsage("claude", "Claude Max", [_win("claude", "weekly")], None, NOW)
    status = build_status(cfg, [(pu, compute_pace(pu.windows[0], [], NOW))], [pu], NOW)
    assert all("stale_error" not in c for c in status["cards"])


def test_build_cards_keeps_stale_usage_normal(tmp_path):
    # stale_error 付きの usage は error 扱いされず、普通のカードになる
    history = History(tmp_path / "history.jsonl")
    pu = _stale_cards()[0][0]
    cards = build_cards([pu], history, NOW)
    assert len(cards) == 2
    assert all(pace is not None for _, pace in cards)
    assert all(u.stale_error == "HTTP 429" for u, _ in cards)


def test_replace_keeps_persisted_copy_clean(tmp_path):
    # dataclasses.replace のコピーにだけ stale_error が付き、元は無書き換え
    good = _usage("claude", [_win("claude", "weekly")], fetched_at=OLD)
    shown = replace(good, stale_error="HTTP 429")
    assert shown.stale_error == "HTTP 429"
    assert good.stale_error is None
    LastGood(tmp_path / LAST_GOOD).set(good)
    on_disk = json.loads((tmp_path / LAST_GOOD).read_text(encoding="utf-8"))
    assert "stale_error" not in on_disk["claude"]
