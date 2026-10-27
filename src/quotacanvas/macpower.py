"""Mac の電源状態と画面の連携 (追加依存なし、ctypes のみ)。

- display_asleep(): 内蔵ディスプレイがスリープ中か (CoreGraphics)
- start_watch(): システムのスリープ/復帰を IOKit で監視し、コールバックを
  バックグラウンドの daemon スレッドで受ける

macOS 以外やフレームワークを読めないときは機能が無効になるだけで、
呼び出し側 (quota-canvas run) はそのまま動き続ける。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger("quotacanvas")

# IOKit の電源管理メッセージ (IOKit/pwr_mgt/IOPMLib.h)
K_IO_MESSAGE_CAN_SYSTEM_SLEEP = 0xE0000270      # まもなくスリープ (拒否も可)
K_IO_MESSAGE_SYSTEM_WILL_SLEEP = 0xE0000280     # 今すぐスリープする
K_IO_MESSAGE_SYSTEM_HAS_POWERED_ON = 0xE0000300  # スリープから復帰した

_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_IO_KIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

# IOServiceInterestCallback: (refcon, service, messageType, messageArgument)
SLEEP_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_void_p)

# ctypes のコールバックオブジェクトは GC で回収されないようモジュールで保持する
_keep_callbacks: list = []
_keep_port: list = []  # RunLoop スレッドが使う port/iterator も併せて保持

_watch_lock = threading.Lock()
_watching = False


class _Libs:
    """読み込んだフレームワークと、型付け済みの関数たち。"""

    def __init__(self) -> None:
        self.cg = ctypes.CDLL(_CORE_GRAPHICS)
        self.cg.CGMainDisplayID.restype = ctypes.c_uint32   # CGDirectDisplayID
        self.cg.CGMainDisplayID.argtypes = []
        self.cg.CGDisplayIsAsleep.restype = ctypes.c_int    # Boolean (0/1)
        self.cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]

        self.iokit = ctypes.CDLL(_IO_KIT)
        self.iokit.IORegisterForSystemPower.restype = ctypes.c_uint32  # io_connect_t
        self.iokit.IORegisterForSystemPower.argtypes = [
            ctypes.c_void_p,                   # void *refCon
            ctypes.POINTER(ctypes.c_void_p),   # IONotificationPortRef *
            SLEEP_CALLBACK,                    # IOServiceInterestCallback
            ctypes.POINTER(ctypes.c_uint32),   # io_iterator_t *notifier
        ]
        self.iokit.IOAllowPowerChange.restype = ctypes.c_int  # IOReturn
        self.iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_long]
        self.iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
        self.iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]

        self.cf = ctypes.CDLL(_CORE_FOUNDATION)
        self.cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        self.cf.CFRunLoopGetCurrent.argtypes = []
        self.cf.CFRunLoopAddSource.restype = None
        self.cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_void_p]
        self.cf.CFRunLoopRun.restype = None
        self.cf.CFRunLoopRun.argtypes = []


_libs: object = None  # None = 未試行 / False = 読めない / _Libs


def _load_libs() -> Optional[_Libs]:
    """フレームワークを読む。macOS 以外や失敗時は None (再試行はしない)。"""
    global _libs
    if _libs is None:
        if sys.platform != "darwin":
            _libs = False
        else:
            try:
                _libs = _Libs()
            except OSError as exc:
                log.warning("CoreGraphics/IOKit を読めないため Mac スリープ連携は無効です: %s", exc)
                _libs = False
    return _libs or None


def display_asleep() -> bool:
    """Mac のディスプレイがスリープ中なら True。macOS 以外や不明なときは False。"""
    libs = _load_libs()
    if libs is None:
        return False
    try:
        return bool(libs.cg.CGDisplayIsAsleep(libs.cg.CGMainDisplayID()))
    except Exception as exc:  # 実機依存の失敗で呼び出し側 (run) を止めない
        log.warning("ディスプレイのスリープ状態を取得できません: %s", exc)
        return False


def start_watch(on_sleep: Callable[[], None], on_wake: Callable[[], None]) -> bool:
    """システムのスリープ/復帰の監視を開始する。

    スリープ直前 (kIOMessageSystemWillSleep) に on_sleep を、復帰後
    (kIOMessageSystemHasPoweredOn) に on_wake を、監視スレッドから呼ぶ。
    on_sleep が戻るまで実際のスリープは始まらないので、呼び出し側は画面消灯
    などの短い処理にとどめること。コールバック内の例外は握りつぶしてログに
    残し、スリープの可否には必ず IOAllowPowerChange で応える (拒否しない)。
    kIOMessageCanSystemSleep は即 IOAllowPowerChange で許可する。

    すでに監視中なら True を返し何もしない。開始に失敗したらログを出して
    False を返す (呼び出し側はスリープ連携なしで動き続けてよい)。
    """
    global _watching
    with _watch_lock:
        if _watching:
            return True
        libs = _load_libs()
        if libs is None:
            log.warning("Mac のシステムスリープ監視はこの環境では使えません")
            return False

        root_port = ctypes.c_uint32(0)
        port_ref = ctypes.c_void_p()
        notifier = ctypes.c_uint32(0)

        def _handle(_refcon, _service, message, argument) -> None:
            # IOKit の監視スレッドから呼ばれる。例外は握りつぶしてログだけ残し、
            # スリープの可否には必ず答える。
            capability = int(argument) if argument else 0
            try:
                if message == K_IO_MESSAGE_CAN_SYSTEM_SLEEP:
                    libs.iokit.IOAllowPowerChange(root_port, capability)
                elif message == K_IO_MESSAGE_SYSTEM_WILL_SLEEP:
                    try:
                        on_sleep()
                    except Exception:
                        log.exception("スリープ前のコールバック (on_sleep) でエラー")
                    libs.iokit.IOAllowPowerChange(root_port, capability)
                elif message == K_IO_MESSAGE_SYSTEM_HAS_POWERED_ON:
                    try:
                        on_wake()
                    except Exception:
                        log.exception("スリープ復帰のコールバック (on_wake) でエラー")
            except Exception:
                log.exception("電源管理メッセージ (0x%X) の処理に失敗しました", message)

        callback = SLEEP_CALLBACK(_handle)
        try:
            root_port.value = libs.iokit.IORegisterForSystemPower(
                None, ctypes.byref(port_ref), callback, ctypes.byref(notifier))
        except Exception as exc:
            log.warning("IORegisterForSystemPower に失敗したためシステムスリープ監視は無効です: %s", exc)
            return False
        if not root_port.value:  # MACH_PORT_NULL = 失敗
            log.warning("IORegisterForSystemPower に失敗したためシステムスリープ監視は無効です")
            return False

        def _pump() -> None:
            # このスレッドの RunLoop に通知ソースを登録して回し続ける (daemon)
            try:
                source = libs.iokit.IONotificationPortGetRunLoopSource(port_ref)
                mode = ctypes.c_void_p.in_dll(libs.cf, "kCFRunLoopDefaultMode")
                libs.cf.CFRunLoopAddSource(libs.cf.CFRunLoopGetCurrent(), source, mode)
                libs.cf.CFRunLoopRun()
            except Exception:
                log.exception("システムスリープ監視スレッドが異常終了しました")

        thread = threading.Thread(target=_pump, name="quotacanvas-macpower", daemon=True)
        try:
            thread.start()
        except Exception as exc:
            log.warning("システムスリープ監視スレッドを開始できません: %s", exc)
            return False
        _keep_callbacks.append(callback)  # GC で回収されないように保持
        _keep_port.extend([port_ref, notifier, root_port])
        _watching = True
        return True
