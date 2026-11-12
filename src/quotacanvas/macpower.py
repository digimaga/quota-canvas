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
#: 監視の世代番号。古いスレッドが新しい監視の _watching を消さないための識別子。
_watch_generation = 0


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
        # IODeregisterForSystemPower は IORegisterForSystemPower が返した notifier
        # (io_object_t) 自体へのポインターを要求する。整数を渡すと解除時に不正な
        # アドレスを参照するので POINTER を宣言する (IOPMLib.h)。
        self.iokit.IODeregisterForSystemPower.restype = ctypes.c_int  # IOReturn
        self.iokit.IODeregisterForSystemPower.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),  # io_object_t *notifier
        ]
        self.iokit.IOServiceClose.restype = ctypes.c_int              # IOReturn
        self.iokit.IOServiceClose.argtypes = [ctypes.c_uint32]        # io_connect_t
        self.iokit.IONotificationPortDestroy.restype = None
        self.iokit.IONotificationPortDestroy.argtypes = [ctypes.c_void_p]
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
        self.cf.CFRunLoopRemoveSource.restype = None
        self.cf.CFRunLoopRemoveSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
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


#: 監視スレッドの初期化 (通知ソースの RunLoop 登録) を待つ上限 (秒)。
_START_TIMEOUT = 10.0


def _run_loop_default_mode(libs: _Libs):
    """kCFRunLoopDefaultMode の CFStringRef。"""
    return ctypes.c_void_p.in_dll(libs.cf, "kCFRunLoopDefaultMode")


def _start_thread(target) -> threading.Thread:
    thread = threading.Thread(target=target, name="quotacanvas-macpower", daemon=True)
    thread.start()
    return thread


def _value(obj):
    """ctypes の値オブジェクト/素の値のどちらでも整数として返す。"""
    return getattr(obj, "value", obj) if obj is not None else 0


def _unregister(libs, root_port, port_ref, notifier, source=None,
                run_loop=None) -> None:
    """IOKit の電源監視を登録解除する。失敗しても例外にしない (ログのみ)。

    順序は IOPMLib.h の契約どおり: RunLoop から通知ソースを外す →
    IODeregisterForSystemPower(&notifier) → IOServiceClose(root_port) →
    IONotificationPortDestroy(port)。notifier は IODeregisterForSystemPower が
    解放するので、ここで IOObjectRelease してはいけない (二重解放になる)。
    """
    if source and run_loop:
        try:
            libs.cf.CFRunLoopRemoveSource(run_loop, source,
                                          _run_loop_default_mode(libs))
        except Exception:
            log.exception("RunLoop からスリープ監視の通知ソースを外せませんでした")
    if _value(notifier):
        try:
            libs.iokit.IODeregisterForSystemPower(ctypes.byref(notifier))
        except Exception:
            log.exception("IORegisterForSystemPower の登録を解除できませんでした")
    if _value(root_port):
        try:
            libs.iokit.IOServiceClose(root_port)
        except Exception:
            log.exception("IORegisterForSystemPower の接続を閉じられませんでした")
    if _value(port_ref):
        try:
            libs.iokit.IONotificationPortDestroy(port_ref)
        except Exception:
            log.exception("スリープ監視の通知ポートを破棄できませんでした")


def _forget(callback, *objects) -> None:
    """保持リストから外す (監視が終わったオブジェクトを GC 可能にする)。"""
    try:
        _keep_callbacks.remove(callback)
    except ValueError:
        pass
    for obj in objects:
        try:
            _keep_port.remove(obj)
        except ValueError:
            pass


def _mark_stopped(generation: int) -> None:
    """この世代の監視だけを停止済みにする。

    初期化の失敗・タイムアウト後に再開始していると、古いスレッドの終了処理が
    新しい監視の _watching を消してしまい、その後の開始で監視が重複する。
    世代番号が一致する (いま有効な監視を持つ) スレッドだけに解除させる。
    """
    global _watching
    with _watch_lock:
        if generation == _watch_generation:
            _watching = False


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
    監視スレッドの初期化に失敗したとき・スレッドが終了したときは、IOKit の
    登録を解除して _watching を False に戻す (以後は失敗に気付ける)。
    初期化待ちのタイムアウトと監視開始の確定が競合したときは、確定済みの
    監視を優先して True を返す (キャンセルと開始の確定は排他なので、False を
    返した監視が CFRunLoopRun に入り残ることはない)。
    """
    global _watching, _watch_generation
    with _watch_lock:
        if _watching:
            return True
        _watch_generation += 1
        generation = _watch_generation
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

        ready = threading.Event()
        state = {"ok": False, "cancelled": False}
        # 「キャンセルの確定」(開始側のタイムアウト) と「監視開始の確定」
        # (スレッドの CFRunLoopRun 直前) を排他にする。スレッドが
        # cancelled を読んだ直後に開始側がタイムアウトすると、開始側は
        # False を返したのにスレッドが CFRunLoopRun に入り、再開始で監視が
        # 重複する (世代番号では防げない)。このロックでどちらか一方だけが
        # 勝つ: 開始が先に確定していれば開始側は成功として扱い、キャンセルが
        # 先ならスレッドは登録を解除して終わる。
        decide = threading.Lock()

        def _pump() -> None:
            # このスレッドの RunLoop に通知ソースを登録して回し続ける (daemon)。
            # 初期化の成否は ready と state で開始側に伝える。
            run_loop = None
            source = None
            try:
                source = libs.iokit.IONotificationPortGetRunLoopSource(port_ref)
                if not source:
                    raise RuntimeError(
                        "IONotificationPortGetRunLoopSource が NULL を返しました")
                run_loop = libs.cf.CFRunLoopGetCurrent()
                if not run_loop:
                    raise RuntimeError("CFRunLoopGetCurrent が NULL を返しました")
                libs.cf.CFRunLoopAddSource(run_loop, source,
                                           _run_loop_default_mode(libs))
                with decide:
                    if state["cancelled"]:
                        # 開始側がタイムアウトでキャンセルを確定させた。
                        # 登録は finally で解除して終わる。
                        return
                    state["ok"] = True
                    ready.set()
                libs.cf.CFRunLoopRun()
                log.warning("システムスリープ監視スレッドが終了しました")
            except Exception:
                log.exception("システムスリープ監視スレッドが異常終了しました")
            finally:
                # 登録解除と保持リストの整理を終えてから開始側を起こす
                # (初期化失敗時に、開始側が解除済みの状態を観測できるように)。
                _unregister(libs, root_port, port_ref, notifier, source, run_loop)
                _forget(callback, port_ref, notifier, root_port)
                ready.set()  # 開始側を待たせたままにしない
                _mark_stopped(generation)

        _keep_callbacks.append(callback)  # GC で回収されないように保持
        _keep_port.extend([port_ref, notifier, root_port])
        try:
            _start_thread(_pump)
        except Exception as exc:
            log.warning("システムスリープ監視スレッドを開始できません: %s", exc)
            _unregister(libs, root_port, port_ref, notifier)
            _forget(callback, port_ref, notifier, root_port)
            return False

        if not ready.wait(_START_TIMEOUT):
            # タイムアウトの直後にスレッドが監視開始を確定させる競合があるので、
            # decide で排他してどちらか一方だけを確定させる。すでに確定済みなら
            # その監視は CFRunLoopRun に入るので、成功として扱う。
            # 旧スレッドの後始末が遅れても、世代が進んだ次の開始の _watching は
            # 消さない (_mark_stopped が世代を照合する)。
            with decide:
                if state["ok"]:
                    _watching = True
                    return True
                state["cancelled"] = True
            log.warning("システムスリープ監視スレッドの初期化が %.0f 秒以内に終わりません",
                        _START_TIMEOUT)
            return False
        if not state["ok"]:
            log.warning("システムスリープ監視を開始できませんでした")
            return False
        _watching = True
        return True
