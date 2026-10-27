"""run ループの「今すぐ反映」: 反映要求ファイルと、設定変更を見る待ち時間。

メニューバーアプリは `quota-canvas refresh` で DATA_DIR/refresh-request を作る。
run ループは待ち時間を 1 秒刻み (WAIT_TICK) に分け、毎秒このファイルと設定
ファイルの mtime を確認し、どちらかが変わっていれば待ちを打ち切ってすぐ次の
ループへ進む (docs/app-contract.md)。反映要求でも前回の取得から 30 秒未満なら
利用量は取り直さない (HTTP 429 対策)。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional, Union

from .config import Config
from .status import DATA_DIR

log = logging.getLogger("quotacanvas")

#: 待ちの確認間隔 (秒)。この粒度で「設定保存後 1〜2 秒で反映」になる。
WAIT_TICK = 1.0

#: 反映要求があっても、前回の取得からこれ未満なら利用量を取り直さない。
REFRESH_MIN_GAP = 30.0

#: 反映要求ファイル (status.json と同じディレクトリ)。中身は使わない。
REFRESH_REQUEST_PATH = DATA_DIR / "refresh-request"

PathLike = Union[str, Path]


def request_refresh(path: Optional[PathLike] = None) -> Path:
    """反映要求ファイルを作る (空でよい)。作ったパスを返す。"""
    p = Path(path).expanduser() if path is not None else REFRESH_REQUEST_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p


def consume_refresh_request(path: Optional[PathLike] = None) -> bool:
    """反映要求があれば削除して True を返す。中身は見ない。

    削除に失敗しても (権限など) 警告だけで True: 要求はあったものとして扱い、
    毎秒の確認が無限に待ちを打ち切り続けるのを防ぐ。
    """
    p = Path(path).expanduser() if path is not None else REFRESH_REQUEST_PATH
    try:
        if not p.exists():
            return False
    except OSError:
        return False
    try:
        p.unlink()
    except OSError as exc:
        log.warning("反映要求ファイル %s を削除できません (%s)", p, exc)
    return True


def wait_for_wakeup(cfg: Config, seconds: float,
                    display_asleep: Optional[Callable[[], bool]] = None) -> str:
    """最大 seconds 秒待つが、WAIT_TICK 刻みで反映要求と設定ファイルを確認する。

    戻り値は待ちを打ち切った理由:
    - "refresh": 反映要求ファイルを消化した (呼び出し側は利用量を取り直してよい)
    - "config": 設定ファイルが変わっている (再読み込みは呼び出し側の処理に任せる)
    - "display": ディスプレイのスリープ状態が変わった (display_asleep を渡した
      ときだけ。呼び出し側は消灯/復帰の処理をしてすぐ描画し直してよい)
    - "": 時間いっぱいまで待った
    一時停止中 (display.paused) でもそのまま使う: ループは回し続け、画面への
    送画だけ呼び出し側が止める。
    """
    deadline = time.monotonic() + max(0.0, seconds)
    asleep_before = bool(display_asleep()) if display_asleep is not None else False
    while True:
        if consume_refresh_request():
            return "refresh"
        if cfg.is_stale():
            return "config"
        if display_asleep is not None and bool(display_asleep()) != asleep_before:
            return "display"
        rest = deadline - time.monotonic()
        if rest <= 0:
            return ""
        time.sleep(min(WAIT_TICK, rest))
