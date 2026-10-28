"""反映要求と run ループの待ち打ち切り (refresh モジュール) のテスト。

待ち (wait_for_wakeup) と run ループは fake 時計で駆動し、実時間は待たない。
ネットワーク・画面・キーチェーン・ユーザーのデータディレクトリは使わない。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from quotacanvas import cli, providers, refresh
from quotacanvas.config import load_config
from quotacanvas.models import ProviderUsage, Window

CONFIG_TOML = "[poll]\ninterval = 60\nredraw = 30\n"


@pytest.fixture(autouse=True)
def _request_path(tmp_path, monkeypatch):
    """反映要求ファイルを tmp_path へ向け、ユーザーのデータディレクトリを守る。"""
    monkeypatch.setattr(refresh, "REFRESH_REQUEST_PATH", tmp_path / "refresh-request")


class StopLoop(Exception):
    """fake 時計が上限に達したら sleep から投げて run ループを抜ける。"""


class FakeClock:
    """monotonic() / sleep() の差し替え。sleep は時刻を進め、plan に登録した
    (時刻, 関数) を通過時に実行し、limit を越えたら StopLoop を投げる。"""

    def __init__(self, limit, plan=()):
        self.now = 0.0
        self.limit = limit
        self.plan = sorted(plan)
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        while self.plan and self.plan[0][0] <= self.now:
            self.plan.pop(0)[1]()
        if self.now >= self.limit:
            raise StopLoop


def _patch_clock(monkeypatch, clock):
    monkeypatch.setattr(refresh, "time", clock)
    monkeypatch.setattr(cli, "time", clock)


# ------------------------------------------------------- wait_for_wakeup 単体

def test_wait_breaks_on_refresh_request(monkeypatch, tmp_path):
    request = tmp_path / "refresh-request"
    clock = FakeClock(limit=60, plan=[(2.5, request.touch)])
    _patch_clock(monkeypatch, clock)
    assert refresh.wait_for_wakeup(load_config(None), 60) == "refresh"
    assert clock.now < 10  # 時間いっぱいまで待たずに打ち切る
    assert not request.exists()  # 要求ファイルは削除して消化する


def test_wait_tick_is_one_second(monkeypatch):
    clock = FakeClock(limit=60)
    _patch_clock(monkeypatch, clock)
    assert refresh.wait_for_wakeup(load_config(None), 5) == ""
    assert clock.sleeps and max(clock.sleeps) <= 1.0  # 1 秒刻みで確認する


def test_wait_breaks_on_config_change(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_TOML, encoding="utf-8")

    def touch_config():
        cfg_path.write_text("# changed\n" + CONFIG_TOML, encoding="utf-8")

    clock = FakeClock(limit=60, plan=[(2.0, touch_config)])
    _patch_clock(monkeypatch, clock)
    assert refresh.wait_for_wakeup(load_config(cfg_path), 60) == "config"


def test_wait_runs_full_duration_when_nothing_happens(monkeypatch):
    clock = FakeClock(limit=60)
    _patch_clock(monkeypatch, clock)
    assert refresh.wait_for_wakeup(load_config(None), 4) == ""
    assert clock.now == 4.0


def test_consume_missing_request_is_false(tmp_path):
    assert refresh.consume_refresh_request(tmp_path / "none") is False


def test_consume_unlink_failure_warns_but_consumes(monkeypatch, tmp_path):
    p = tmp_path / "refresh-request"
    p.touch()

    def boom(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", boom)
    assert refresh.consume_refresh_request(p) is True  # 削除失敗でも要求は処理した
    assert refresh.consume_refresh_request(p) is False
    clock = FakeClock(limit=60)
    _patch_clock(monkeypatch, clock)
    assert refresh.wait_for_wakeup(load_config(None), 4) == ""
    assert clock.now == 4
    p.write_text("new request", encoding="utf-8")
    assert refresh.consume_refresh_request(p) is True
    assert refresh.consume_refresh_request(p) is False


def test_request_refresh_creates_file_and_parents(tmp_path):
    target = tmp_path / "d" / "refresh-request"
    assert refresh.request_refresh(target) == target
    assert target.exists()


# ------------------------------------------------------------- refresh コマンド

def test_refresh_command_creates_request_file(capsys, tmp_path):
    target = tmp_path / "refresh-request"
    assert cli.main(["refresh", "--json"]) == 0
    assert target.exists()  # エンジンが動いていなくてもファイルだけ作る
    assert json.loads(capsys.readouterr().out) == {"requested": True}


def test_refresh_command_without_json_is_human_readable(capsys, tmp_path):
    assert cli.main(["refresh"]) == 0
    out = capsys.readouterr().out
    assert not out.lstrip().startswith("{")


# ------------------------------------------------------------------ run ループ

def _mock_fetch(monkeypatch, calls):
    """providers.fetch_all をモックし、呼ばれるたびに (fake 時計の monotonic
    時刻, now) を calls に記録する。"""
    usage = ProviderUsage("claude", "Claude Max",
                          [Window("claude", "weekly", "週間", 10.0, None, timedelta(days=7))],
                          None, None)

    def fetch_all(now=None, enabled=None):
        calls.append((cli.time.monotonic(), now))
        return [ProviderUsage("claude", "Claude Max",
                              [Window("claude", "weekly", "週間", 10.0, None, timedelta(days=7))],
                              None, now)]

    monkeypatch.setattr(providers, "fetch_all", fetch_all)


def _run_loop(monkeypatch, tmp_path, clock, plan=(), config_text=CONFIG_TOML):
    """run を --no-screen で動かし、fake 時計が limit に達するまで回す。

    戻り値は providers.fetch_all の呼び出し記録。"""
    _patch_clock(monkeypatch, clock)
    clock.plan = sorted(plan)
    calls = []
    _mock_fetch(monkeypatch, calls)
    monkeypatch.setattr(cli, "render",
                        lambda cards, now, **kw: Image.new("RGB", (8, 8)))
    monkeypatch.setattr(cli, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(cli, "write_frame", lambda *a, **k: None)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(config_text, encoding="utf-8")
    with pytest.raises(StopLoop):
        cli.main(["--config", str(cfg_path), "--no-screen",
                  "--history", str(tmp_path / "history.jsonl"), "run"])
    return calls


def test_run_defers_refetch_to_30s_after_last_fetch(monkeypatch, tmp_path):
    """30 秒未満の反映要求は捨てない: すぐには取り直さず、前回の取得の
    30 秒後に 1 回取り直す。"""
    request = tmp_path / "refresh-request"
    # t=0 に取得 → t=2 に反映要求: t=2 の周回では取り直さず、t=30 に取り直す
    calls = _run_loop(monkeypatch, tmp_path, FakeClock(limit=40),
                      plan=[(2.0, request.touch)])
    assert len(calls) == 2
    assert calls[0][0] == 0.0          # 最初の定期取得
    assert 30.0 <= calls[1][0] < 35.0  # 延期分は前回の取得の 30 秒後に 1 回だけ


def test_run_refetches_when_request_is_30s_after_last_fetch(monkeypatch, tmp_path):
    request = tmp_path / "refresh-request"
    # t=0 に取得 → t=36 に反映要求: 30 秒以上あいたので取り直す
    calls = _run_loop(monkeypatch, tmp_path, FakeClock(limit=70),
                      plan=[(36.0, request.touch)])
    assert len(calls) == 2
    # 取り直した t=36 の次の定期取得は interval (60 秒) 後 = t=96。
    # limit=70 まで回しても 3 回目が起きないことで、ずれていることを確認する。


def test_run_breaks_wait_at_deferred_fetch_before_redraw(monkeypatch, tmp_path):
    """延期した取り直しのあと待ちが redraw より短く縮む: redraw=60 でも
    前回の取得の 30 秒後で打ち切られ、+60 秒まで遅れない。"""
    request = tmp_path / "refresh-request"
    # t=0 に取得 → t=2 に反映要求 → redraw=60 を待たず t=30 で取り直す。
    # limit=80: t=90 の定期取得を回さない (取得回数を 2 回に固定するため)。
    calls = _run_loop(monkeypatch, tmp_path, FakeClock(limit=80),
                      plan=[(2.0, request.touch)],
                      config_text="[poll]\ninterval = 60\nredraw = 60\n")
    assert len(calls) == 2
    assert calls[1][0] == pytest.approx(30.0, abs=2.0)  # redraw (=60) まで遅れない


def test_run_deferred_refetch_does_not_add_extra_fetches(monkeypatch, tmp_path):
    """延期した取り直しのあと、さらに余計な取り直しが起きない: 延期分の
    1 回だけで、定期取得はそこから interval 置きに組み直される。"""
    request = tmp_path / "refresh-request"
    # t=0 取得 → t=2 要求 → t=30 延期分の取り直し → 次の定期取得は t=90。
    # limit=95 まで回して、t=60 などの中間で取り直しが増えないことを確かめる。
    calls = _run_loop(monkeypatch, tmp_path, FakeClock(limit=95),
                      plan=[(2.0, request.touch)])
    assert [mono for mono, _ in calls] == [0.0, 30.0, 90.0]


def test_run_extra_request_while_deferred_still_fetches_once(monkeypatch, tmp_path):
    """延期の待ちのあいだにさらに反映要求が来ても、延期時刻の取り直しは
    1 回だけ (2 つ目の要求で取り直しが増えない)。"""
    request = tmp_path / "refresh-request"
    # t=0 に取得 → t=2 に反映要求 (t=30 へ延期) → t=10 に 2 つ目の要求。
    # 2 つ目も 30 秒未満なので延期時刻は同じく t=30 のまま: 取り直しは 1 回。
    calls = _run_loop(monkeypatch, tmp_path, FakeClock(limit=40),
                      plan=[(2.0, request.touch), (10.0, request.touch)])
    assert [mono for mono, _ in calls] == [0.0, 30.0]


def test_run_config_change_alone_does_not_refetch(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_TOML, encoding="utf-8")

    def touch_config():
        cfg_path.write_text("# changed\n" + CONFIG_TOML, encoding="utf-8")

    calls = _run_loop(monkeypatch, tmp_path, FakeClock(limit=40),
                      plan=[(3.0, touch_config)])
    assert len(calls) == 1  # 設定変更だけでは利用量を取り直さない


def test_run_stays_quiet_while_paused_but_loop_keeps_going(monkeypatch, tmp_path):
    request = tmp_path / "refresh-request"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[display]\npaused = true\n" + CONFIG_TOML, encoding="utf-8")
    calls = []
    _mock_fetch(monkeypatch, calls)

    def fake_render(cards, now, **kwargs):
        raise AssertionError("一時停止中は画面に描かない")

    monkeypatch.setattr(cli, "render", fake_render)
    written = []
    monkeypatch.setattr(cli, "write_status", lambda payload, *a, **k: written.append(payload))
    monkeypatch.setattr(cli, "write_frame", lambda *a, **k: None)

    def wake_and_touch():
        request.touch()
        cfg_path.write_text("# changed\n[display]\npaused = true\n" + CONFIG_TOML,
                            encoding="utf-8")

    clock = FakeClock(limit=40, plan=[(5.0, wake_and_touch)])
    _patch_clock(monkeypatch, clock)
    with pytest.raises(StopLoop):
        cli.main(["--config", str(cfg_path), "--no-screen",
                  "--history", str(tmp_path / "history.jsonl"), "run"])
    assert len(calls) == 2  # 要求は捨てられず、前回の取得の 30 秒後に取り直される
    assert calls[1][0] == pytest.approx(30.0, abs=2.0)
    assert len(written) >= 2  # 一時停止中でも status.json は更新され続ける
    assert all(p["paused"] for p in written)


# --------------------------------------------------- Mac スリープ連携 (run ループ)

class FakeScreen:
    """turzx.Screen の置き換え: show / brightness の呼び出しを記録する。"""

    def __init__(self):
        self.calls = []

    def show(self, img):
        self.calls.append(("show", img.size, img.getpixel((0, 0))))
        return True

    def brightness(self, value):
        self.calls.append(("brightness", value))


def _run_with_screen(monkeypatch, tmp_path, clock, screen, config_text=CONFIG_TOML):
    """fake screen + fake clock で run を --no-screen 抜きで回し StopLoop まで待つ。"""
    _patch_clock(monkeypatch, clock)
    clock.plan = sorted(clock.plan)
    calls = []
    _mock_fetch(monkeypatch, calls)
    clock.fetches = calls  # 利用量の取得記録 (必要なテストだけ見る)
    renders = []

    def fake_render(cards, now, **kwargs):
        renders.append(now)
        return Image.new("RGB", (8, 8), (9, 9, 9))

    monkeypatch.setattr(cli, "render", fake_render)
    monkeypatch.setattr(cli, "_open_screen", lambda: screen)
    written = []
    monkeypatch.setattr(cli, "write_status", lambda payload, *a, **k: written.append(payload))
    monkeypatch.setattr(cli, "write_frame", lambda *a, **k: None)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(config_text, encoding="utf-8")
    with pytest.raises(StopLoop):
        cli.main(["--config", str(cfg_path),
                  "--history", str(tmp_path / "history.jsonl"), "run"])
    return renders, written


def test_run_dims_on_display_sleep_and_restores_on_wake(monkeypatch, tmp_path):
    screen = FakeScreen()
    state = {"asleep": False}
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: state["asleep"])
    monkeypatch.setattr(cli.macpower, "start_watch", lambda on_sleep, on_wake: True)
    request = tmp_path / "refresh-request"

    def go_asleep():
        state["asleep"] = True

    def wake_up():
        state["asleep"] = False

    # t=2 でディスプレイがスリープ → t=5 に反映要求 (前回の取得から 30 秒未満なの
    # で t=30 へ延期、眠っている間は取りに行かない) → t=35 に復帰。延期分の取り直し
    # は復帰の周回で 1 回だけ行われる。
    # limit=39: t=65 の定期再描画を回さない (render の回数を 2 回に固定するため)。
    clock = FakeClock(limit=39, plan=[(2.0, go_asleep), (5.0, request.touch), (35.0, wake_up)])
    renders, written = _run_with_screen(monkeypatch, tmp_path, clock, screen)

    # 明るさ: 起動時 100 → 消灯 0 → 復帰で 100 に戻す
    assert [c[1] for c in screen.calls if c[0] == "brightness"] == [100, 0, 100]
    # 黒一色の 1920x462 は眠り始めの 1 回だけ (反映要求が来ても増やさない)
    blacks = [c for c in screen.calls if c[0] == "show" and c[1] == (1920, 462)
              and c[2] == (0, 0, 0)]
    assert len(blacks) == 1
    # 通常の描画は眠る前と復帰後の 2 回だけ。黒フレームはその間に入る。
    assert len(renders) == 2
    shows = [c for c in screen.calls if c[0] == "show"]
    assert [c[1] for c in shows] == [(8, 8), (1920, 462), (8, 8)]
    # 眠っている間も status.json は更新され続ける
    assert len(written) >= 3


def test_run_does_not_fetch_while_display_asleep(monkeypatch, tmp_path):
    screen = FakeScreen()
    state = {"asleep": False}
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: state["asleep"])
    monkeypatch.setattr(cli.macpower, "start_watch", lambda on_sleep, on_wake: True)

    def go_asleep():
        state["asleep"] = True

    def wake_up():
        state["asleep"] = False

    # interval=60。t=2 で眠り t=130 に起きる: 眠っている間 (t=62, 122 の期限) は
    # 取りに行かず、起きた直後に期限切れの分を 1 回だけ取り直す。
    clock = FakeClock(limit=140, plan=[(2.0, go_asleep), (130.0, wake_up)])
    _run_with_screen(monkeypatch, tmp_path, clock, screen)
    assert len(clock.fetches) == 2


def test_run_does_not_fetch_deferred_request_while_display_asleep(monkeypatch, tmp_path):
    """眠っている間に届いた 30 秒未満の反映要求も、延期時刻を過ぎても眠っている
    あいだは取りに行かない。待ちも 0 秒に縮めず redraw 刻みのまま空回りせず、
    起きた周回で 1 回だけ取り直す。"""
    screen = FakeScreen()
    state = {"asleep": False}
    checks = {"n": 0}

    def display_asleep():
        # 待ちが 0 秒に縮められると眠っている間のループは時計を進められず空回り
        # する (wake_up が永久に来ない)。display_asleep の呼び出し数の上限で
        # 検知して失敗させる (正常な 1 周回あたり数回、全体でも 200 回弱)。
        checks["n"] += 1
        if checks["n"] > 1000:
            raise AssertionError("スリープ中に待ちが 0 秒になり、ループが空回りしています")
        return state["asleep"]

    monkeypatch.setattr(cli.macpower, "display_asleep", display_asleep)
    monkeypatch.setattr(cli.macpower, "start_watch", lambda on_sleep, on_wake: True)

    def go_asleep():
        state["asleep"] = True

    def wake_up():
        state["asleep"] = False

    # t=0 に取得 → t=2 で眠り、t=2.5 に反映要求 (30 秒未満なので t=30 へ延期)。
    # 延期時刻 (t=30) は眠っている間に過ぎるが取りに行かず、redraw (30 秒) 刻みで
    # 回り、t=130 に起きた周回で期限切れの定期分と合わせて 1 回だけ取り直す。
    request = tmp_path / "refresh-request"
    clock = FakeClock(limit=140, plan=[(2.0, go_asleep), (2.5, request.touch),
                                       (130.0, wake_up)])
    _, written = _run_with_screen(monkeypatch, tmp_path, clock, screen)
    assert [mono for mono, _ in clock.fetches] == [0.0, 130.0]
    # 眠っている間の周回は redraw 刻み (約 30 秒に 1 回): 待ちが縮まされて
    # 空回りしていれば status.json の更新回数が跳ね上がる。
    assert len(written) < 30


def test_run_refetches_sleep_arrived_request_right_after_wake(monkeypatch, tmp_path):
    """眠っている間に届いた (前回の取得から 30 秒以上たった) 反映要求は、
    その場では取らず、起きた周回で 1 回だけ取り直す。"""
    screen = FakeScreen()
    state = {"asleep": False}
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: state["asleep"])
    monkeypatch.setattr(cli.macpower, "start_watch", lambda on_sleep, on_wake: True)

    def go_asleep():
        state["asleep"] = True

    def wake_up():
        state["asleep"] = False

    # t=0 に取得 → t=2 で眠り、t=40 (前回の取得から 30 秒以上) に反映要求。
    # 眠っているので要求の周回では取らず、t=50 に復帰した周回で 1 回だけ取る。
    # limit=80: t=80 の定期取得 (復帰後の取得から interval 後) を回さない。
    request = tmp_path / "refresh-request"
    clock = FakeClock(limit=80, plan=[(2.0, go_asleep), (40.0, request.touch),
                                      (50.0, wake_up)])
    renders, _ = _run_with_screen(monkeypatch, tmp_path, clock, screen)
    assert [mono for mono, _ in clock.fetches] == [0.0, 50.0]
    assert len(renders) == 2  # 取り直しは復帰後の再描画と同じ周回で行われる


def test_run_sleeps_and_wakes_via_system_sleep_callbacks(monkeypatch, tmp_path):
    screen = FakeScreen()
    watched = []
    monkeypatch.setattr(cli.macpower, "start_watch",
                        lambda on_sleep, on_wake: watched.extend([on_sleep, on_wake]) or True)
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: False)  # ディスプレイは眠っていない

    def do_sleep():
        watched[0]()  # on_sleep: IOKit の監視スレッドから呼ばれるのを擬似する

    def do_wake():
        watched[1]()  # on_wake

    # limit=36: t=37 の定期再描画を回さない (render の回数を 2 回に固定するため)。
    clock = FakeClock(limit=36, plan=[(2.0, do_sleep), (6.0, do_wake)])
    renders, _ = _run_with_screen(monkeypatch, tmp_path, clock, screen)

    # ディスプレイが起きていても、システムスリープの報告で黒 1 枚 + 明るさ 0 になる
    assert [c[1] for c in screen.calls if c[0] == "brightness"] == [100, 0, 100]
    blacks = [c for c in screen.calls if c[0] == "show" and c[1] == (1920, 462)
              and c[2] == (0, 0, 0)]
    assert len(blacks) == 1
    assert len(renders) == 2  # 復帰後に明るさを設定し直して再描画する


def test_run_ignores_sleep_when_sleep_with_mac_disabled(monkeypatch, tmp_path):
    screen = FakeScreen()
    watched = []
    monkeypatch.setattr(cli.macpower, "start_watch",
                        lambda on_sleep, on_wake: watched.extend([on_sleep, on_wake]) or True)
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: True)  # ディスプレイは眠っている

    def do_sleep():
        watched[0]()

    config_text = "[display]\nsleep_with_mac = false\n" + CONFIG_TOML
    clock = FakeClock(limit=40, plan=[(2.0, do_sleep)])
    renders, _ = _run_with_screen(monkeypatch, tmp_path, clock, screen,
                                  config_text=config_text)

    # 無効なら消灯しない: 明るさは起動時の 1 回だけ、黒フレームも送らない
    assert [c[1] for c in screen.calls if c[0] == "brightness"] == [100]
    assert all(c[1] == (8, 8) for c in screen.calls if c[0] == "show")
    assert len(renders) == 2  # redraw=30 の定期再描画だけが走る


def test_wait_breaks_when_display_sleep_state_changes(monkeypatch):
    state = {"asleep": False}
    clock = FakeClock(limit=60, plan=[(2.5, lambda: state.update(asleep=True))])
    _patch_clock(monkeypatch, clock)
    assert refresh.wait_for_wakeup(load_config(None), 60,
                                   lambda: state["asleep"]) == "display"
    assert clock.now < 10  # 時間いっぱいまで待たずに打ち切る


def test_wait_runs_full_duration_when_display_state_unchanged(monkeypatch):
    clock = FakeClock(limit=60)
    _patch_clock(monkeypatch, clock)
    assert refresh.wait_for_wakeup(load_config(None), 4, lambda: False) == ""
    assert clock.now == 4.0


def test_wait_breaks_right_after_system_wake_and_next_loop_restores(monkeypatch, tmp_path):
    """on_sleep で消灯 → on_wake したら、定期再描画 (redraw=30) を待たずに
    待ちが "display" で打ち切られ、次の周回で明るさが再設定されフレームが送られる。"""
    screen = FakeScreen()
    watched = []
    monkeypatch.setattr(cli.macpower, "start_watch",
                        lambda on_sleep, on_wake: watched.extend([on_sleep, on_wake]) or True)
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: False)  # ディスプレイは眠っていない

    marks = {"wake": [], "render_at": []}

    def do_sleep():
        watched[0]()  # on_sleep: 黒 1 枚 + 明るさ 0 で消灯される

    def do_wake():
        watched[1]()  # on_wake: 報告の解除だけ (消灯の解除はループ側)
        marks["wake"].append(cli.time.monotonic())

    # t=2 にスリープ (消灯) → t=4 に復帰。redraw=30 の定期再描画よりずっと早く戻るはず。
    clock = FakeClock(limit=20, plan=[(2.0, do_sleep), (4.0, do_wake)])
    _patch_clock(monkeypatch, clock)
    clock.plan = sorted(clock.plan)
    _mock_fetch(monkeypatch, [])
    monkeypatch.setattr(cli, "render",
                        lambda cards, now, **kw: marks["render_at"].append(cli.time.monotonic())
                        or Image.new("RGB", (8, 8), (9, 9, 9)))
    monkeypatch.setattr(cli, "_open_screen", lambda: screen)
    monkeypatch.setattr(cli, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(cli, "write_frame", lambda *a, **k: None)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_TOML, encoding="utf-8")
    with pytest.raises(StopLoop):
        cli.main(["--config", str(cfg_path),
                  "--history", str(tmp_path / "history.jsonl"), "run"])

    # 復帰 (t=4) から 1 秒後の確認タイミングで待ちが打ち切られ、次の周回で
    # 明るさが再設定されフレームが送られる (定期再描画の t=30 は待たない)。
    assert len(marks["render_at"]) == 2
    assert marks["render_at"][1] - marks["wake"][0] <= 2.0
    assert marks["render_at"][1] < 10  # redraw=30 を待っていない
    # 明るさ: 起動時 100 → on_sleep で 0 → 復帰後すぐ 100
    assert [c[1] for c in screen.calls if c[0] == "brightness"] == [100, 0, 100]
    shows = [c for c in screen.calls if c[0] == "show"]
    assert [c[1] for c in shows] == [(8, 8), (1920, 462), (8, 8)]  # 通常 → 黒 → 通常


class FlakyScreen(FakeScreen):
    """brightness(0) を最初の fail_zero 回だけ失敗させる画面。"""

    def __init__(self):
        super().__init__()
        self.fail_zero = 0

    def brightness(self, value):
        if value == 0 and self.fail_zero > 0:
            self.fail_zero -= 1
            self.calls.append(("brightness-fail", 0))
            raise RuntimeError("usb busy")
        self.calls.append(("brightness", value))


def test_dim_retry_when_brightness_zero_fails(monkeypatch, tmp_path):
    """明るさ 0 に失敗したら dimmed を立てず、次の周回で再試行する (review #3)。

    失敗しても待ちは打ち切られない (mismatch が変わらない) ので、redraw=10 の
    次の定期周回 (t=12) で再試行が起きることを確かめる。 wake は再試行の後。
    """
    screen = FlakyScreen()
    screen.fail_zero = 1  # 最初の消灯だけ失敗させる
    state = {"asleep": False}
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: state["asleep"])
    monkeypatch.setattr(cli.macpower, "start_watch", lambda on_sleep, on_wake: True)

    def go_asleep():
        state["asleep"] = True

    def wake_up():
        state["asleep"] = False

    # t=2 に消灯開始 (失敗) → t=12 に定期周回で再試行 (成功) → t=14 に復帰。
    # limit=23: t=24 の定期再描画を回さない (render の回数を 2 回に固定)。
    clock = FakeClock(limit=23, plan=[(2.0, go_asleep), (14.0, wake_up)])
    renders, _ = _run_with_screen(
        monkeypatch, tmp_path, clock, screen,
        config_text="[display]\nsleep_with_mac = true\n"
                    "[poll]\ninterval = 60\nredraw = 10\n")

    # 失敗した明るさ 0 は ("brightness-fail", 0) として記録され、dimmed が立たない
    # ので 1 秒後の次の周回で再試行され、今度は成功する
    assert ("brightness-fail", 0) in screen.calls
    assert [c[1] for c in screen.calls if c[0] == "brightness"] == [100, 0, 100]
    blacks = [c for c in screen.calls if c[0] == "show" and c[1] == (1920, 462)
              and c[2] == (0, 0, 0)]
    assert len(blacks) == 1
    shows = [c for c in screen.calls if c[0] == "show"]
    assert [c[1] for c in shows] == [(8, 8), (1920, 462), (8, 8)]  # 通常 → 黒 → 通常
    assert len(renders) == 2


def test_draw_branch_rechecks_sleep_inside_lock(monkeypatch, tmp_path):
    """通常描画の分岐でもロック直後に眠りを取り直す: 眠っていれば dimmed を
    戻さず、render 済みのフレームも送らない (review #2)。"""
    from collections import deque
    screen = FakeScreen()
    watched = []
    monkeypatch.setattr(cli.macpower, "start_watch",
                        lambda on_sleep, on_wake: watched.extend([on_sleep, on_wake]) or True)
    # display_asleep の呼ばれ順を脚本化する (system_asleep が True のあいだは
    # 短絡して呼ばれない点に注意):
    script = deque([
        False, False, False, False,   # t=0 周回: 計算 / ロック直後 / 待ち開始 / tick
        False,                        # t=4 tick: on_wake 直後 → 待ち打ち切り
        False,                        # t=4 周回: sleeping の計算は「起きている」
        True,                         # t=4 周回: ロック直後の取り直しで「眠っている」(競合)
        True,                         # t=4 待ち開始: 眠ったまま (dimmed 済みで一致)
        True, True, True, True, True, True, True,  # t=5..11 の tick
    ])
    monkeypatch.setattr(cli.macpower, "display_asleep",
                        lambda: script.popleft() if script else False)

    marks = {"wake": [], "frames": []}

    def do_sleep():
        watched[0]()  # on_sleep: 黒 1 枚 + 明るさ 0

    def do_wake():
        watched[1]()
        marks["wake"].append(cli.time.monotonic())

    clock = FakeClock(limit=20, plan=[(2.0, do_sleep), (4.0, do_wake)])
    _patch_clock(monkeypatch, clock)
    clock.plan = sorted(clock.plan)
    _mock_fetch(monkeypatch, [])
    monkeypatch.setattr(cli, "render",
                        lambda cards, now, **kw: Image.new("RGB", (8, 8), (9, 9, 9)))
    monkeypatch.setattr(cli, "_open_screen", lambda: screen)
    monkeypatch.setattr(cli, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(cli, "write_frame",
                        lambda img, *a, **k: marks["frames"].append(img))
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_TOML, encoding="utf-8")
    with pytest.raises(StopLoop):
        cli.main(["--config", str(cfg_path),
                  "--history", str(tmp_path / "history.jsonl"), "run"])

    # 競合の周回 (t=4) では dimmed を戻さずフレームも送らない: 明るさは
    # [起動 100, on_sleep 0, 本物の復帰後 100] のまま、送られた frame.png も
    # 通常 2 回だけ (t=4 周回の render は破棄される)。
    assert [c[1] for c in screen.calls if c[0] == "brightness"] == [100, 0, 100]
    shows = [c for c in screen.calls if c[0] == "show"]
    assert [c[1] for c in shows] == [(8, 8), (1920, 462), (8, 8)]  # 通常 → 黒 → 通常
    assert len(marks["frames"]) == 2


def test_on_system_sleep_does_nothing_while_paused(monkeypatch, tmp_path):
    """一時停止中は on_system_sleep で消灯しない (review #4)。"""
    screen = FakeScreen()
    watched = []
    monkeypatch.setattr(cli.macpower, "start_watch",
                        lambda on_sleep, on_wake: watched.extend([on_sleep, on_wake]) or True)
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: False)

    config_text = "[display]\npaused = true\n" + CONFIG_TOML
    clock = FakeClock(limit=10, plan=[
        (2.0, lambda: watched[0]()),  # on_sleep
        (4.0, lambda: watched[1]()),  # on_wake
    ])
    renders, _ = _run_with_screen(monkeypatch, tmp_path, clock, screen,
                                  config_text=config_text)

    # 一時停止中: 消灯処理は何もしない (起動時の明るさ設定だけで、黒も明るさ 0 も送らない)
    assert screen.calls == [("brightness", 100)]
    assert renders == []


# --------------------------------------------- 送信失敗時の画面の開き直し

class BrokenScreen(FakeScreen):
    """show が常に False を返す画面 (USB 再接続後の古いハンドルなど)。"""

    def show(self, img):
        self.calls.append(("show-fail", img.size))
        return False


def test_run_reopens_screen_after_send_failure_and_sets_brightness(monkeypatch, tmp_path):
    """送信に失敗したら _output が画面を開き直して送り直し、run ループは
    入れ替わった screen に明るさを設定し直して、以後それを使い続ける。"""
    old, new = BrokenScreen(), FakeScreen()
    opened = iter([old, new])  # 起動時 → old、_output での開き直し → new
    monkeypatch.setattr(cli, "_open_screen", lambda: next(opened))
    clock = FakeClock(limit=32)
    _patch_clock(monkeypatch, clock)
    _mock_fetch(monkeypatch, [])
    monkeypatch.setattr(cli, "render",
                        lambda cards, now, **kw: Image.new("RGB", (8, 8), (9, 9, 9)))
    monkeypatch.setattr(cli, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(cli, "write_frame", lambda *a, **k: None)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_TOML, encoding="utf-8")
    with pytest.raises(StopLoop):
        cli.main(["--config", str(cfg_path),
                  "--history", str(tmp_path / "history.jsonl"), "run"])

    # 旧画面: 起動時の明るさ 100 → 送信失敗の 1 回だけ (以後触らない)
    assert old.calls == [("brightness", 100), ("show-fail", (8, 8))]
    # 新しい画面: 送り直した直後に明るさ 100 を設定され、t=30 の定期再描画では
    # 再設定なしでそのまま使い続ける
    assert new.calls == [("show", (8, 8), (9, 9, 9)), ("brightness", 100),
                         ("show", (8, 8), (9, 9, 9))]


def test_fetched_at_stays_while_display_asleep(monkeypatch, tmp_path):
    # 眠っている間も status.json は書き続けるが、取りに行かないので各カードの
    # fetched_at は進まない (updated_at だけが進む)。起きて取り直すと進む。
    screen = FakeScreen()
    state = {"asleep": False}
    monkeypatch.setattr(cli.macpower, "display_asleep", lambda: state["asleep"])
    monkeypatch.setattr(cli.macpower, "start_watch", lambda on_sleep, on_wake: True)
    base = cli.datetime(2026, 10, 25, 9, 0, 0, tzinfo=cli.timezone.utc)

    class FakeDatetime(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            return base + timedelta(seconds=clock.now)

    monkeypatch.setattr(cli, "datetime", FakeDatetime)

    def go_asleep():
        state["asleep"] = True

    def wake_up():
        state["asleep"] = False

    clock = FakeClock(limit=140, plan=[(2.0, go_asleep), (130.0, wake_up)])
    _, written = _run_with_screen(monkeypatch, tmp_path, clock, screen)
    first = written[0]["cards"][0]["fetched_at"]
    asleep = [p for p in written if p["updated_at"] != written[0]["updated_at"]
              and p["cards"][0]["fetched_at"] == first]
    assert len(asleep) >= 3                         # 眠っている間の周回
    assert len({p["updated_at"] for p in asleep}) == len(asleep)   # updated_at は進む
    assert written[-1]["cards"][0]["fetched_at"] > first           # 起きて取り直すと進む


def test_run_filters_cached_usage_on_config_reload_and_restores_on_enable(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.toml"
    enabled = "[routing]\nenabled = true\n" + CONFIG_TOML
    disabled = "[providers.claude]\nenabled = false\n" + enabled
    calls, written, rendered = [], [], []
    _mock_fetch(monkeypatch, calls)
    monkeypatch.setattr(cli, "render", lambda cards, now, **kw: rendered.append(
        [pu.provider for pu, _ in cards]) or Image.new("RGB", (8, 8)))
    monkeypatch.setattr(cli, "render_error", lambda *a, **k: rendered.append([]) or Image.new("RGB", (8, 8)))
    monkeypatch.setattr(cli, "write_status", lambda payload, *a, **kw: written.append(payload))
    monkeypatch.setattr(cli, "write_frame", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(cli.gate, "BURN_PATH", tmp_path / "burn.json")
    clock = FakeClock(limit=8, plan=[
        (2, lambda: cfg_path.write_text(disabled, encoding="utf-8")),
        (4, lambda: cfg_path.write_text(enabled, encoding="utf-8")),
    ])
    _patch_clock(monkeypatch, clock)
    cfg_path.write_text(enabled, encoding="utf-8")
    with pytest.raises(StopLoop):
        cli.main(["--config", str(cfg_path), "--no-screen",
                  "--history", str(tmp_path / "history.jsonl"), "run"])
    assert len(calls) == 1
    assert rendered[:3] == [["claude"], [], ["claude"]]
    first, hidden, restored = written[:3]
    assert first["cards"] and first["windows"]
    assert first["recommendation"]["provider"] == "claude"
    assert hidden["cards"] == hidden["windows"] == hidden["errors"] == []
    assert "recommendation" not in hidden
    assert hidden["routing"]["workers"] == []
    assert restored["cards"] and restored["windows"]
    assert restored["recommendation"]["provider"] == "claude"
