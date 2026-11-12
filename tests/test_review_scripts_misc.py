"""scripts-misc のレビューで追加した macpower (スリープ監視) のテスト。

実機の IOKit / RunLoop には触れず、偽の libs だけを使う。
"""

from __future__ import annotations

import ctypes
import threading
import time
from types import SimpleNamespace

import pytest

from quotacanvas import macpower

# conftest の autouse fixture が macpower.start_watch を差し替える前の本体。
START_WATCH = macpower.start_watch

MODE = 0x9999
ROOT_PORT = 0x3000
PORT = 0x1000
NOTIFIER = 0x2000
LOOP = 0x4000
SOURCE = 0x5000


class FakeIokit:
    def __init__(self, source=SOURCE):
        self.source = source
        self.registered = 0
        self.callback = None
        self.deregister_args = []
        self.deregistered = []
        self.closed = []
        self.destroyed = []
        self.released = []
        self.allowed = []

    @staticmethod
    def _value(obj):
        return getattr(obj, "value", obj)

    def IORegisterForSystemPower(self, refcon, port_ref, callback, notifier):
        self.registered += 1
        self.callback = callback
        port_ref._obj.value = PORT
        notifier._obj.value = NOTIFIER
        return ROOT_PORT

    def IONotificationPortGetRunLoopSource(self, port):
        return self.source

    def IOAllowPowerChange(self, port, capability):
        self.allowed.append((self._value(port), capability))

    def IODeregisterForSystemPower(self, notifier):
        # SDK どおり notifier へのポインター (byref) を受け取る
        self.deregister_args.append(notifier)
        self.deregistered.append(self._value(getattr(notifier, "_obj", notifier)))

    def IOServiceClose(self, port):
        self.closed.append(self._value(port))

    def IONotificationPortDestroy(self, port):
        self.destroyed.append(self._value(port))

    def IOObjectRelease(self, obj):
        # IODeregisterForSystemPower が notifier を解放するので呼ばれてはいけない
        self.released.append(self._value(obj))
        return 0


class FakeCoreFoundation:
    def __init__(self, stop):
        self.stop = stop
        self.added = []
        self.removed = []
        self.running = threading.Event()

    def CFRunLoopGetCurrent(self):
        return LOOP

    def CFRunLoopAddSource(self, run_loop, source, mode):
        self.added.append((run_loop, source, mode))

    def CFRunLoopRemoveSource(self, run_loop, source, mode):
        self.removed.append((run_loop, source, mode))

    def CFRunLoopRun(self):
        self.running.set()
        self.stop.wait(10)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(macpower, "_watching", False)
    monkeypatch.setattr(macpower, "_watch_generation", 0)
    monkeypatch.setattr(macpower, "_keep_callbacks", [])
    monkeypatch.setattr(macpower, "_keep_port", [])
    monkeypatch.setattr(macpower, "_run_loop_default_mode", lambda libs: MODE)
    stop = threading.Event()
    iokit = FakeIokit()
    cf = FakeCoreFoundation(stop)
    monkeypatch.setattr(
        macpower, "_load_libs",
        lambda: SimpleNamespace(iokit=iokit, cf=cf, cg=None))
    return SimpleNamespace(stop=stop, iokit=iokit, cf=cf)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_start_watch_registers_source_and_cleans_up(env):
    assert START_WATCH(lambda: None, lambda: None) is True
    assert macpower._watching is True
    assert env.cf.added == [(LOOP, SOURCE, MODE)]
    assert env.iokit.registered == 1

    env.stop.set()  # 監視スレッドの RunLoop が終わる = 監視が止まる
    assert _wait_until(lambda: macpower._watching is False)
    assert env.cf.removed == [(LOOP, SOURCE, MODE)]
    # SDK の契約: IODeregisterForSystemPower には notifier へのポインターを渡し、
    # 続けて IOServiceClose(root_port)、最後に通知ポートを破棄する。
    assert env.iokit.deregistered == [NOTIFIER]
    assert env.iokit.deregister_args[0]._obj.value == NOTIFIER
    assert env.iokit.closed == [ROOT_PORT]
    assert env.iokit.destroyed == [PORT]
    assert env.iokit.released == []  # notifier は IODeregisterForSystemPower が解放する
    assert macpower._keep_callbacks == []
    assert macpower._keep_port == []


def test_null_run_loop_source_reports_failure_and_unregisters(env, caplog):
    env.iokit.source = None
    with caplog.at_level("WARNING", logger="quotacanvas"):
        assert START_WATCH(lambda: None, lambda: None) is False
    assert macpower._watching is False
    assert env.cf.added == []                       # RunLoop には登録しない
    assert env.iokit.deregistered == [NOTIFIER]     # 登録済みの port は漏らさない
    assert env.iokit.closed == [ROOT_PORT]
    assert env.iokit.destroyed == [PORT]
    assert env.iokit.released == []
    assert "スリープ監視" in caplog.text
    # 失敗したあとで再度開始を試せる (_watching が True のまま残らない)
    env.iokit.source = SOURCE
    assert START_WATCH(lambda: None, lambda: None) is True
    env.stop.set()
    assert _wait_until(lambda: macpower._watching is False)


def test_thread_start_failure_unregisters_port(env, monkeypatch, caplog):
    def fail(target):
        raise RuntimeError("no thread")

    monkeypatch.setattr(macpower, "_start_thread", fail)
    with caplog.at_level("WARNING", logger="quotacanvas"):
        assert START_WATCH(lambda: None, lambda: None) is False
    assert macpower._watching is False
    assert env.cf.added == []
    assert env.iokit.deregistered == [NOTIFIER]
    assert env.iokit.closed == [ROOT_PORT]
    assert env.iokit.destroyed == [PORT]
    assert env.iokit.released == []
    assert macpower._keep_callbacks == []
    assert macpower._keep_port == []
    assert "スリープ監視スレッドを開始できません" in caplog.text


def test_stale_watch_generation_cannot_clear_new_watch(monkeypatch):
    monkeypatch.setattr(macpower, "_watching", True)
    monkeypatch.setattr(macpower, "_watch_generation", 2)
    macpower._mark_stopped(1)  # 古い世代のスレッドの後始末
    assert macpower._watching is True
    macpower._mark_stopped(2)  # いま有効な監視
    assert macpower._watching is False


def test_restart_while_old_thread_alive_keeps_new_watch(env, monkeypatch):
    threads = []
    real_start = macpower._start_thread

    def capture(target):
        thread = real_start(target)
        threads.append(thread)
        return thread

    monkeypatch.setattr(macpower, "_start_thread", capture)
    assert START_WATCH(lambda: None, lambda: None) is True

    # 初期化タイムアウト後 (_watching=False)、古いスレッドはまだ生きている状態で再開始する
    stop2 = threading.Event()
    iokit2 = FakeIokit()
    cf2 = FakeCoreFoundation(stop2)
    monkeypatch.setattr(
        macpower, "_load_libs",
        lambda: SimpleNamespace(iokit=iokit2, cf=cf2, cg=None))
    monkeypatch.setattr(macpower, "_watching", False)
    assert START_WATCH(lambda: None, lambda: None) is True

    env.stop.set()  # 1 本目だけ終了させ、後始末まで済ませる
    threads[0].join(5)
    assert not threads[0].is_alive()
    assert env.iokit.closed == [ROOT_PORT]
    assert macpower._watching is True  # 古いスレッドが新しい監視を消していない

    stop2.set()
    assert _wait_until(lambda: macpower._watching is False)


def test_timeout_cancels_pending_watch_and_restart_is_clean(env, monkeypatch,
                                                            caplog):
    """タイムアウトでキャンセルが確定したら、スレッドは RunLoop に入らず解除する。

    初期化の途中 (RunLoop への登録後、キャンセル判定の前) でスレッドを止め、
    その間に開始側をタイムアウトさせる。キャンセル確定後にスレッドを進めても
    CFRunLoopRun に入ってはいけない (= False を返した監視が残らない) ので、
    再開始しても監視は重複しない。
    """
    monkeypatch.setattr(macpower, "_START_TIMEOUT", 0.05)
    init_gate = threading.Event()
    real_add = env.cf.CFRunLoopAddSource

    def add_source_then_wait(run_loop, source, mode):
        real_add(run_loop, source, mode)
        init_gate.wait(5)  # キャンセル判定の直前で止める

    monkeypatch.setattr(env.cf, "CFRunLoopAddSource", add_source_then_wait)

    with caplog.at_level("WARNING", logger="quotacanvas"):
        assert START_WATCH(lambda: None, lambda: None) is False
    assert macpower._watching is False
    assert "初期化が" in caplog.text

    init_gate.set()  # キャンセル確定後にスレッドの初期化を再開する
    assert _wait_until(lambda: env.iokit.destroyed == [PORT])
    assert not env.cf.running.is_set()  # CFRunLoopRun には入らない
    assert env.cf.removed == [(LOOP, SOURCE, MODE)]
    assert env.iokit.deregistered == [NOTIFIER]
    assert env.iokit.closed == [ROOT_PORT]
    assert env.iokit.released == []
    assert macpower._keep_callbacks == []
    assert macpower._keep_port == []

    # 重複なく再開始できる (古い登録の解除は 1 回だけ)
    assert START_WATCH(lambda: None, lambda: None) is True
    assert env.iokit.registered == 2
    env.stop.set()
    assert _wait_until(lambda: macpower._watching is False)
    assert env.iokit.deregistered == [NOTIFIER, NOTIFIER]


def test_watch_commit_wins_race_with_timeout(env, monkeypatch):
    """タイムアウトの直後に監視開始が確定したら、開始側は成功として扱う。

    ready.wait が必ずタイムアウトする偽の Event で開始側を「タイムアウト後、
    キャンセル確定前」に駐車し、その間にスレッドへ監視開始を確定させる
    (判定の直後に開始側がタイムアウトする実行順の再現)。確定済みの監視は
    生きているので True を返し、解除は後の停止時に 1 回だけ行われる。
    """
    starter_parked = threading.Event()
    release_starter = threading.Event()

    class TimeoutEvent:
        """ready の代わり。wait は開始側を駐車させて必ずタイムアウト扱い。"""

        def __init__(self):
            self._flag = False

        def set(self):
            self._flag = True

        def is_set(self):
            return self._flag

        def clear(self):
            self._flag = False

        def wait(self, timeout=None):
            starter_parked.set()
            return release_starter.wait(5) and False  # 常にタイムアウト

    monkeypatch.setattr(macpower.threading, "Event", TimeoutEvent)

    result = {}

    def call_start():
        result["value"] = START_WATCH(lambda: None, lambda: None)

    caller = threading.Thread(target=call_start)
    caller.start()
    assert starter_parked.wait(5)   # 開始側はタイムアウト、確定待ちで駐車
    assert env.cf.running.wait(5)   # スレッドは監視開始を確定し RunLoop に入った
    release_starter.set()           # 開始側のキャンセル判定を進める
    caller.join(5)
    assert not caller.is_alive()

    assert result["value"] is True  # 開始が先に確定したので成功として扱う
    assert macpower._watching is True
    assert env.iokit.deregistered == []  # 生きている監視は解除しない
    assert env.iokit.closed == []
    assert env.iokit.destroyed == []
    assert env.cf.removed == []

    env.stop.set()
    assert _wait_until(lambda: macpower._watching is False)
    assert env.iokit.deregistered == [NOTIFIER]  # 停止時に 1 回だけ解除
    assert env.iokit.closed == [ROOT_PORT]
    assert env.iokit.destroyed == [PORT]


def test_libs_declare_sdk_pointer_contract(monkeypatch):
    class FakeFunction:
        pass

    class FakeFramework:
        def __getattr__(self, name):
            func = FakeFunction()
            setattr(self, name, func)
            return func

    monkeypatch.setattr(macpower.ctypes, "CDLL", lambda _path: FakeFramework())
    libs = macpower._Libs()
    assert libs.iokit.IORegisterForSystemPower.argtypes[-1] == \
        ctypes.POINTER(ctypes.c_uint32)
    assert libs.iokit.IODeregisterForSystemPower.argtypes == \
        [ctypes.POINTER(ctypes.c_uint32)]
    assert libs.iokit.IOServiceClose.argtypes == [ctypes.c_uint32]
    assert libs.iokit.IONotificationPortDestroy.argtypes == [ctypes.c_void_p]
