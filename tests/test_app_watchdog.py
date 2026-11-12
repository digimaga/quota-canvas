"""app_watchdog (アプリの予期しない終了の起動し直し) のテスト。

subprocess (ps / open) と os.kill は差し替え、実コマンドは一切呼ばない。
ユーザーのデータディレクトリにも触れない (パスは tmp_path)。
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from quotacanvas import app_watchdog

BUNDLE = "/Applications/QuotaCanvas.app"
APP_COMM = BUNDLE + "/Contents/MacOS/QuotaCanvasApp"


@pytest.fixture(autouse=True)
def _watchdog_env(tmp_path, monkeypatch):
    """状態ファイル・マークを tmp_path に向け、macOS として振る舞わせる。"""
    monkeypatch.setattr(app_watchdog, "APP_STATE_PATH", tmp_path / "app-state.json")
    monkeypatch.setattr(app_watchdog, "RELAUNCH_MARK_PATH", tmp_path / "app-relaunch-at")
    monkeypatch.setattr(app_watchdog, "_last_attempt_at", None)
    monkeypatch.setattr(sys, "platform", "darwin")
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    """subprocess.run を記録する。ps は「アプリが生きている」と答える既定。"""
    record = []

    def fake_run(args, **kw):
        record.append(list(args))
        if args[0] == "ps":
            return SimpleNamespace(returncode=0, stdout=APP_COMM + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_watchdog.subprocess, "run", fake_run)
    return record


def _write_state(tmp_path, **kw):
    state = {"bundle_path": BUNDLE, "pid": 4242, "launched_at": 1_700_000_000,
             "user_quit": False}
    state.update(kw)
    (tmp_path / "app-state.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def _dead_kill(pid, sig):
    raise ProcessLookupError


def _live_kill(pid, sig):
    return None


def _open_calls(calls):
    return [c for c in calls if c[0] == "open"]


def test_not_macos_does_nothing(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(sys, "platform", "linux")
    _write_state(tmp_path)
    monkeypatch.setattr(os, "kill", _dead_kill)
    app_watchdog.check()
    assert calls == []


def test_missing_file_and_broken_json_do_not_raise(tmp_path, monkeypatch, calls):
    app_watchdog.check()  # ファイル無し
    (tmp_path / "app-state.json").write_text("{not json", encoding="utf-8")
    app_watchdog.check()  # 壊れた JSON
    (tmp_path / "app-state.json").write_text("[1, 2]", encoding="utf-8")
    app_watchdog.check()  # dict でない JSON
    assert calls == []


def test_user_quit_true_does_not_relaunch(tmp_path, monkeypatch, calls):
    _write_state(tmp_path, user_quit=True)
    monkeypatch.setattr(os, "kill", _dead_kill)
    app_watchdog.check()
    assert _open_calls(calls) == []


def test_alive_app_process_does_not_relaunch(tmp_path, monkeypatch, calls):
    _write_state(tmp_path)
    monkeypatch.setattr(os, "kill", _live_kill)
    app_watchdog.check()
    assert _open_calls(calls) == []
    assert any(c[0] == "ps" for c in calls)  # pid 再利用対策の comm 確認は行う


def test_dead_pid_relaunches(tmp_path, monkeypatch, calls):
    bundle = tmp_path / "QuotaCanvas.app"
    bundle.mkdir()
    _write_state(tmp_path, bundle_path=str(bundle))
    monkeypatch.setattr(os, "kill", _dead_kill)
    app_watchdog.check()
    assert _open_calls(calls) == [["open", "-g", str(bundle)]]


def test_pid_reused_by_other_process_relaunches(tmp_path, monkeypatch, calls):
    bundle = tmp_path / "QuotaCanvas.app"
    bundle.mkdir()
    _write_state(tmp_path, bundle_path=str(bundle))
    monkeypatch.setattr(os, "kill", _live_kill)

    def fake_run(args, **kw):
        calls.append(list(args))
        if args[0] == "ps":
            return SimpleNamespace(returncode=0, stdout="/usr/bin/python3\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_watchdog.subprocess, "run", fake_run)
    app_watchdog.check()
    assert _open_calls(calls) == [["open", "-g", str(bundle)]]


def test_relaunch_rate_limited(tmp_path, monkeypatch, calls):
    bundle = tmp_path / "QuotaCanvas.app"
    bundle.mkdir()
    _write_state(tmp_path, bundle_path=str(bundle))
    monkeypatch.setattr(os, "kill", _dead_kill)
    app_watchdog.check()
    app_watchdog.check()  # 300 秒未満の 2 回目は試みない
    assert _open_calls(calls) == [["open", "-g", str(bundle)]]
    mark = tmp_path / "app-relaunch-at"
    assert mark.exists()


def test_relaunch_rate_limited_when_marker_cannot_be_written(tmp_path, monkeypatch, calls):
    """マークを書けなくても、プロセス内の最終試行時刻で 300 秒の間隔を守る。"""
    bundle = tmp_path / "QuotaCanvas.app"
    bundle.mkdir()
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(app_watchdog, "RELAUNCH_MARK_PATH",
                        blocker / "app-relaunch-at")
    _write_state(tmp_path, bundle_path=str(bundle))
    monkeypatch.setattr(os, "kill", _dead_kill)
    app_watchdog.check()
    app_watchdog.check()  # マークは無いが、直前の試行から 300 秒未満なので試みない
    assert _open_calls(calls) == [["open", "-g", str(bundle)]]
    assert not (blocker / "app-relaunch-at").exists()


def test_marker_failure_retries_after_interval(tmp_path, monkeypatch, calls):
    """マークが書けなくても、間隔が過ぎれば起動し直しをやめない。"""
    bundle = tmp_path / "QuotaCanvas.app"
    bundle.mkdir()
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(app_watchdog, "RELAUNCH_MARK_PATH",
                        blocker / "app-relaunch-at")
    _write_state(tmp_path, bundle_path=str(bundle))
    monkeypatch.setattr(os, "kill", _dead_kill)
    clock = {"now": 1000.0}
    monkeypatch.setattr(app_watchdog.time, "monotonic", lambda: clock["now"])
    app_watchdog.check()
    clock["now"] += app_watchdog.RELAUNCH_MIN_GAP
    app_watchdog.check()
    assert len(_open_calls(calls)) == 2


def test_bad_bundle_path_does_not_relaunch(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(os, "kill", _dead_kill)
    _write_state(tmp_path, bundle_path="/nonexistent/QuotaCanvas.app")
    app_watchdog.check()  # 存在しないディレクトリ
    _write_state(tmp_path, bundle_path=str(tmp_path))  # .app で終わらない
    app_watchdog.check()
    assert _open_calls(calls) == []


def test_open_failure_logs_warning_and_does_not_raise(tmp_path, monkeypatch,
                                                      calls, caplog):
    bundle = tmp_path / "QuotaCanvas.app"
    bundle.mkdir()
    _write_state(tmp_path, bundle_path=str(bundle))
    monkeypatch.setattr(os, "kill", _dead_kill)

    def failing_run(args, **kw):
        if args[0] == "open":
            raise OSError("open not found")
        return SimpleNamespace(returncode=0, stdout=APP_COMM + "\n", stderr="")

    monkeypatch.setattr(app_watchdog.subprocess, "run", failing_run)
    with caplog.at_level("WARNING", logger="quotacanvas"):
        app_watchdog.check()  # 例外は外へ出さない
    assert "起動し直しに失敗" in caplog.text


def test_ps_failure_counts_as_alive(tmp_path, monkeypatch, calls):
    """pid は在るが ps で名前を確かめられないときは起動し直さない。"""
    bundle = tmp_path / "QuotaCanvas.app"
    bundle.mkdir()
    _write_state(tmp_path, bundle_path=str(bundle))
    monkeypatch.setattr(os, "kill", _live_kill)

    def fake_run(args, **kw):
        calls.append(list(args))
        if args[0] == "ps":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_watchdog.subprocess, "run", fake_run)
    app_watchdog.check()
    assert _open_calls(calls) == []
