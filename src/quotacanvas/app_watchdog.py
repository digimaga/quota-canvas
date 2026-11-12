"""メニューバーアプリの予期しない終了を検知して起動し直す (app watchdog)。

アプリ (QuotaCanvas.app) は起動中ずっと DATA_DIR/app-state.json に
{"bundle_path", "pid", "launched_at", "user_quit"} を書き、ユーザー操作などの
正常終了では user_quit=true に書き換える (原子的)。エンジン (run) は巡回ごとに
このファイルを読み、user_quit=false なのに pid のプロセスが居ない
(または pid が別プロセスに再利用されている) ときだけ `open -g` で起動し直す。
前回試みた時刻は app-relaunch-at の mtime に残し、常駐ループが再起動しても
連打しないようにする。マークを書けない環境でも間隔を守れるよう、プロセス内にも
最終試行時刻を持つ。macOS 以外では何もしない。失敗してもエンジンは止めない。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .status import DATA_DIR

log = logging.getLogger("quotacanvas")

#: アプリが書く状態ファイル (status.json と同じディレクトリ)。
APP_STATE_PATH = DATA_DIR / "app-state.json"

#: 前回「起動し直しを試みた」時刻を保持するファイル (mtime を見る)。
RELAUNCH_MARK_PATH = DATA_DIR / "app-relaunch-at"

#: 起動し直しの最小間隔 (秒)。
RELAUNCH_MIN_GAP = 300.0

#: pid 再利用対策: 実行ファイル名がこれでなければアプリのプロセスとみなさない。
APP_COMM_NAME = "QuotaCanvasApp"

#: プロセス内の最終試行時刻 (time.monotonic)。マークに書けなくても連打を防ぐ。
_last_attempt_at: Optional[float] = None


def _app_process_alive(pid: int) -> bool:
    """pid が生きていて実行ファイル名が QuotaCanvasApp なら True。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # 別ユーザーのプロセスでも存在はする → 名前で確かめる
    except OSError:
        return False
    try:
        proc = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True  # pid は在る。名前を確かめられないときは生きているとみなす
    if proc.returncode != 0 or not proc.stdout.strip():
        return True
    # comm は環境によってフルパスかコマンド名かで返る (basename で比較)
    return Path(proc.stdout.strip()).name == APP_COMM_NAME


def _last_attempt() -> Optional[float]:
    try:
        return RELAUNCH_MARK_PATH.stat().st_mtime
    except OSError:
        return None


def check() -> None:
    """app-state.json を読み、条件がそろえばアプリを `open -g` で起動し直す。

    run ループの 1 巡回に 1 回呼ぶ想定。すべての失敗は WARNING ログに留め、
    例外は外へ出さない。
    """
    if sys.platform != "darwin":
        return
    try:
        _check()
    except Exception as exc:
        log.warning("アプリの起動し直しチェックに失敗しました: %s", exc)


def _check() -> None:
    global _last_attempt_at
    try:
        state = json.loads(APP_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # ファイル無し・読めない・壊れた JSON は何もしない
    if not isinstance(state, dict) or state.get("user_quit") is not False:
        return
    pid = state.get("pid")
    # pid<=0 は os.kill の対象が自分のプロセスグループになるので除外する
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 \
            and _app_process_alive(pid):
        return
    bundle_path = state.get("bundle_path")
    if not isinstance(bundle_path, str) or not bundle_path.endswith(".app") \
            or not Path(bundle_path).is_dir():
        return
    last = _last_attempt()
    if last is not None and time.time() - last < RELAUNCH_MIN_GAP:
        return
    # マークを書けない環境では mtime が頼れないので、プロセス内の記録でも間隔を守る。
    now = time.monotonic()
    if _last_attempt_at is not None and now - _last_attempt_at < RELAUNCH_MIN_GAP:
        return
    _last_attempt_at = now
    try:
        RELAUNCH_MARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        RELAUNCH_MARK_PATH.touch()  # 失敗した試行も記録し、連打を防ぐ
    except OSError:
        pass  # 書けなくても _last_attempt_at があるので起動し直しは続けられる
    try:
        proc = subprocess.run(["open", "-g", bundle_path],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("メニューバーアプリの起動し直しに失敗しました: %s", exc)
        return
    if proc.returncode == 0:
        log.info("メニューバーアプリが動いていないため起動し直しました: %s", bundle_path)
    else:
        log.warning("メニューバーアプリの起動し直しに失敗しました (open rc=%s)",
                    proc.returncode)
