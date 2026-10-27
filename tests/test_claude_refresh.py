"""Claude のトークン自動更新 (providers.claude.token_refresh = "cli" / "off") のテスト。

subprocess.Popen・キーチェーン読み出し (_read_keychain_payload)・http_get_json・
shutil.which をすべてモックし、実際の claude / security / ネットワークには
絶対に触れない。更新スレッドは既定で同期実行に差し替え、スレッド自身の
挙動 (同時 1 本) だけ実スレッドで確かめる。クールダウンなどのモジュール
状態はテストごとにリセットする。
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import threading
import types
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import refresh as refresh_mod
from quotacanvas.providers import claude

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

USAGE_PAYLOAD = {"five_hour": {"utilization": 10.0,
                               "resets_at": "2026-10-25T20:00:00+00:00"}}

# autouse fixture が差し替える前の本物の実装 (単フライトのテストで使う)
REAL_SPAWN = claude._spawn_refresh_thread
DEFAULT_MODE = claude._refresh_mode  # None (未設定)。run ループだけが set_token_refresh する


def creds(token="tok-1", subscription="max", expires_in=None):
    """キーチェーン読み出し 1 回分の戻り値 (token, subscription, expires_at)。"""
    expires = NOW + timedelta(seconds=expires_in) if expires_in is not None else None
    return (token, subscription, expires)


def http_error(code):
    return urllib.error.HTTPError(claude.CLAUDE_USAGE_URL, code, "Error", None, None)


# ------------------------------------------------------------------- fixtures

class FakeClock:
    """claude.time の差し替え。sleep は実際には待たずに記録するだけ。"""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def advance(self, seconds):
        self.now += seconds


class RunningProc:
    """起動中の子プロセスの置き換え (_kill_running_child のテスト用)。"""

    def __init__(self, alive=True):
        self.pid = 999
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(claude, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    """モジュール状態をテストごとにリセットする (モードは未設定に戻す)。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(claude, "_refresh_mode", DEFAULT_MODE)
    monkeypatch.setattr(claude, "_cooldown_until", None)
    monkeypatch.setattr(claude, "_last_not_found", None)
    monkeypatch.setattr(claude, "_last_expiry", None)
    monkeypatch.setattr(claude, "_refresh_thread", None)
    monkeypatch.setattr(claude, "_refresh_started_at", None)
    monkeypatch.setattr(claude, "_child", None)


@pytest.fixture(autouse=True)
def run_cli_mode(_reset_state):
    """ほとんどのテストは run ループ相当 ("cli" モード) で行う。
    未設定 (None) の挙動は、テスト側で明示的に None に戻して確かめる。"""
    claude.set_token_refresh("cli")


@pytest.fixture(autouse=True)
def _fake_claude_path(monkeypatch):
    """shutil.which が claude を見つけたことにする (実行は FakeCli が吸収する)。"""
    monkeypatch.setattr(claude, "shutil",
                        types.SimpleNamespace(which=lambda name, path=None: "/fake/bin/claude"))


@pytest.fixture(autouse=True)
def requested_refresh(monkeypatch):
    """request_refresh をモックし、呼ばれた回数を記録する (実データを守る)。"""
    requested = []
    monkeypatch.setattr(refresh_mod, "request_refresh", lambda *a, **k: requested.append(1))
    return requested


@pytest.fixture(autouse=True)
def sync_refresh(monkeypatch):
    """_spawn_refresh_thread を同期実行に差し替える (worker をその場で呼ぶ)。"""
    spawns = []

    def inline():
        spawns.append(1)
        claude._refresh_started_at = claude.time.monotonic()
        claude._refresh_worker()
        return True

    monkeypatch.setattr(claude, "_spawn_refresh_thread", inline)
    return spawns


# ----------------------------------------------------------------- fake 部品

class FakeKeychain:
    """_read_keychain_payload を差し替える。

    payloads: 読み出し 1 回ごとの creds() 戻り値。足りなくなったら最後の要素を
    使い回す。本物と同じ副作用として claude._last_expiry も更新する。
    """

    def __init__(self, monkeypatch, payloads, events=None):
        self.payloads = list(payloads)
        self.events = events
        self.calls = 0
        monkeypatch.setattr(claude, "_read_keychain_payload", self)

    def __call__(self):
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        if self.events is not None:
            self.events.append("read")
        token, subscription, expires = self.payloads[index]
        claude._last_expiry = expires
        return token, subscription, expires


class FakeProc:
    """Popen の戻り値。script = {"exit": rc} か {"timeout": True}。"""

    def __init__(self, script):
        self.pid = 42424
        self.script = script
        self.returncode = script.get("exit", 0)
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.script.get("timeout") and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return self.returncode


class FakeCli:
    """claude モジュール内の subprocess を丸ごと差し替える (Popen は記録だけ)。

    launches: 起動 1 回ごとの script ({"exit": rc} / {"timeout": True} /
    {"popen": "oserror"})。足りなくなったら最後の要素を使い回す。
    popens に (argv, kwargs)、procs に FakeProc を記録する。
    """

    def __init__(self, monkeypatch, launches=({"exit": 0},)):
        self.popens = []
        self.procs = []
        self.launches = list(launches)
        fake = types.SimpleNamespace(
            Popen=self._popen,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        )
        monkeypatch.setattr(claude, "subprocess", fake)

    def _popen(self, argv, **kwargs):
        index = len(self.popens)
        self.popens.append((list(argv), kwargs))
        script = self.launches[min(index, len(self.launches) - 1)]
        if script.get("popen") == "oserror":
            raise OSError("cannot start")
        proc = FakeProc(script)
        self.procs.append(proc)
        return proc


class FakeHttp:
    """http_get_json を差し替える。

    outcomes: 呼び出しごとの結果。成功は dict、失敗は例外インスタンス。
    足りなくなったら最後の要素を使い回す。calls に (url, headers) を記録する。
    """

    def __init__(self, monkeypatch, outcomes, events=None):
        self.outcomes = list(outcomes)
        self.events = events
        self.calls = []
        monkeypatch.setattr(claude, "http_get_json", self)

    def __call__(self, url, headers, timeout=None):
        self.calls.append((url, dict(headers)))
        if self.events is not None:
            self.events.append("http")
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# ----------------------------------------------------------- token_refresh 設定

def test_set_token_refresh_rejects_unknown():
    with pytest.raises(ValueError):
        claude.set_token_refresh("auto")


# ------------------------------------------------------------------- off のとき

def test_off_never_calls_cli_and_asks_manual_launch(monkeypatch, clock, sync_refresh):
    """off では 401/403 で CLI を呼ばず「一度起動してください」の CredentialError。"""
    claude.set_token_refresh("off")
    events = []
    FakeKeychain(monkeypatch, [creds("tok-old")], events)
    cli = FakeCli(monkeypatch)
    FakeHttp(monkeypatch, [http_error(401), http_error(403)], events)

    with pytest.raises(claude.CredentialError) as excinfo:
        claude.fetch(NOW)
    assert str(excinfo.value) == "トークン期限切れ（claude を一度起動してください）"
    with pytest.raises(claude.CredentialError) as excinfo:
        claude.fetch(NOW)
    assert "一度起動してください" in str(excinfo.value)  # 403 も同じ

    assert events == ["read", "http", "read", "http"]  # spawn しない
    assert cli.popens == []
    assert sync_refresh == []


def test_off_fetches_even_when_expiry_is_near(monkeypatch, clock, sync_refresh):
    """off では期限切れ間近でも何もせず、今あるトークンで普通に取得する。"""
    claude.set_token_refresh("off")
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60)])
    cli = FakeCli(monkeypatch)
    http = FakeHttp(monkeypatch, [USAGE_PAYLOAD])

    usage = claude.fetch(NOW)

    assert usage.provider == "claude"
    assert cli.popens == []
    assert sync_refresh == []
    assert http.calls[0][1]["Authorization"] == "Bearer tok-old"


def test_off_401_message_english(monkeypatch, clock, sync_refresh):
    """en のとき off の 401 案内文も英語になる。"""
    from quotacanvas import i18n

    claude.set_token_refresh("off")
    FakeKeychain(monkeypatch, [creds("tok-old")])
    FakeCli(monkeypatch)
    FakeHttp(monkeypatch, [http_error(401)])

    i18n.set_language("en")
    try:
        with pytest.raises(claude.CredentialError) as excinfo:
            claude.fetch(NOW)
        assert str(excinfo.value) == "Token expired (run claude once)"
    finally:
        i18n.set_language("ja")


# ------------------------------------------------------------------- cli のとき

def test_fetch_near_expiry_starts_refresh_without_blocking(
        monkeypatch, clock, sync_refresh, requested_refresh):
    """cli では期限切れ間近で更新を開始し、今あるトークンでそのまま取得する。"""
    events = []
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60),
                               creds("tok-old", expires_in=60),
                               creds("tok-new", expires_in=3600)], events)
    cli = FakeCli(monkeypatch, launches=({"exit": 0},))
    http = FakeHttp(monkeypatch, [USAGE_PAYLOAD], events)

    usage = claude.fetch(NOW)

    assert usage.provider == "claude"
    assert sync_refresh == [1]                       # 更新を開始した
    assert events == ["read", "read", "read", "http"]  # fetch の読み → 更新前後の読み → 取得
    assert http.calls[0][1]["Authorization"] == "Bearer tok-old"  # ブロックせず旧トークンで取得
    assert len(cli.popens) == 1
    assert requested_refresh == [1]                  # 成功したので run ループに反映要求
    # REFRESH_MIN_GAP (30 秒) で捨てられないよう、開始から gap+1 秒待ってから出す
    assert clock.sleeps == [refresh_mod.REFRESH_MIN_GAP + 1.0]


def test_fetch_401_starts_refresh_and_reports_auto_refresh(
        monkeypatch, clock, sync_refresh, requested_refresh):
    """cli では 401/403 で更新を開始し「トークン期限切れ（自動更新中）」を上げる。"""
    events = []
    FakeKeychain(monkeypatch, [creds("tok-old"),
                               creds("tok-old"),
                               creds("tok-new", expires_in=3600)], events)
    cli = FakeCli(monkeypatch, launches=({"exit": 0},))
    FakeHttp(monkeypatch, [http_error(401)], events)

    with pytest.raises(claude.CredentialError) as excinfo:
        claude.fetch(NOW)

    assert str(excinfo.value) == "トークン期限切れ（自動更新中）"
    assert events == ["read", "http", "read", "read"]
    assert len(cli.popens) == 1
    assert requested_refresh == [1]  # 更新が成功したので取り直させる
    assert clock.sleeps == [refresh_mod.REFRESH_MIN_GAP + 1.0]  # gap を待ってから反映要求


def test_refresh_success_requires_expiry_advance(monkeypatch, clock, requested_refresh):
    """exit 0 でも expiresAt が進んでいなければ失敗。クールダウンは 30 分でなく
    5 分 (CLI は期限前にはまだ更新しないので、期限切れ後すぐ再試行できるように)。"""
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60),
                               creds("tok-old", expires_in=60),
                               creds("tok-old", expires_in=60),
                               creds("tok-new", expires_in=3600)])
    cli = FakeCli(monkeypatch, launches=({"exit": 0}, {"exit": 0}))

    assert claude._refresh_via_cli() is False
    assert len(cli.popens) == 1
    assert requested_refresh == []
    assert claude._cooldown_until == 1000.0 + claude.CLI_RETRY_SOON  # 5 分

    clock.advance(claude.CLI_RETRY_SOON + 1.0)
    assert claude._refresh_via_cli() is True  # 5 分後にはもう起動できる
    assert len(cli.popens) == 2


def test_refresh_nonzero_exit_fails(monkeypatch, clock):
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60)])
    cli = FakeCli(monkeypatch, launches=({"exit": 1},))

    assert claude._refresh_via_cli() is False
    assert len(cli.popens) == 1
    assert claude._cooldown_until == 1000.0 + claude.CLI_REFRESH_COOLDOWN  # 30 分


def test_popen_failure_does_not_start_cooldown(monkeypatch, clock):
    """Popen 失敗 (起動できず) はクールダウンに入れない: すぐ再試行できる。"""
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60),
                               creds("tok-old", expires_in=60),
                               creds("tok-new", expires_in=3600)])
    cli = FakeCli(monkeypatch, launches=({"popen": "oserror"}, {"exit": 0}))

    assert claude._refresh_via_cli() is False       # 1 回目: 起動に失敗
    assert claude._cooldown_until is None           # 起動できていないのでクールダウンにも入らない
    assert claude._refresh_via_cli() is True        # 2 回目: クールダウンなしですぐ起動
    assert len(cli.popens) == 2


def test_timeout_kills_process_group(monkeypatch, clock):
    """タイムアウトしたらプロセスグループを SIGKILL してから待つ。"""
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60)])
    cli = FakeCli(monkeypatch, launches=({"timeout": True},))
    killed = []
    monkeypatch.setattr(claude.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(claude.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))

    assert claude._refresh_via_cli() is False
    assert killed == [(42424 + 1, signal.SIGKILL)]  # proc.pid のグループを kill
    assert cli.procs[0].waits == [120.0, None]      # timeout 付き待ち → kill 後の始末待ち
    assert claude._child is None                    # 子の記録は片付く
    assert claude._cooldown_until == 1000.0 + claude.CLI_REFRESH_COOLDOWN  # 30 分


def test_launch_env_is_restricted_and_args_match(monkeypatch, clock, tmp_path):
    """子プロセスの env は 7 変数だけ、PATH は前付け + 元 PATH + バイナリの dir。"""
    for key, value in (("HOME", str(tmp_path)), ("USER", "tester"),
                       ("LOGNAME", "tester"), ("TMPDIR", "/tmp"),
                       ("LANG", "ja_JP.UTF-8"), ("SHELL", "/bin/zsh")):
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/original/bin:/usr/bin")
    monkeypatch.setenv("QUOTA_CANVAS_SECRET", "leak-me")  # 漏れてはいけない
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60),
                               creds("tok-new", expires_in=3600)])
    cli = FakeCli(monkeypatch, launches=({"exit": 0},))
    original_popen = claude.subprocess.Popen

    def checked_popen(*args, **kwargs):
        assert stat.S_IMODE(os.stat(kwargs["cwd"]).st_mode) == 0o700
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(claude.subprocess, "Popen", checked_popen)

    assert claude._refresh_via_cli() is True

    argv, kwargs = cli.popens[0]
    assert argv == ["/fake/bin/claude", "-p", "ok", "--model", "haiku",
                    "--max-turns", "1", "--no-session-persistence",
                    "--setting-sources", "project", "--strict-mcp-config",
                    "--disable-slash-commands"]
    env = kwargs["env"]
    assert set(env) == {"HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "PATH", "SHELL"}
    assert "leak-me" not in env.values()
    parts = env["PATH"].split(":")
    assert parts[:4] == ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    assert "/usr/original/bin" in parts and "/usr/bin" in parts
    assert parts[-1] == "/fake/bin"  # 見つけたバイナリのディレクトリも足す
    assert kwargs["start_new_session"] is True
    base = tmp_path / "Library" / "Caches" / "quota-canvas"
    assert os.path.dirname(kwargs["cwd"]) == str(base)
    assert os.path.basename(kwargs["cwd"]).startswith("claude-refresh-")
    assert stat.S_IMODE(os.stat(base).st_mode) == 0o700
    assert not os.path.exists(kwargs["cwd"])
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert claude._child is None  # 成功後も子の記録は片付く


def test_success_starts_cooldown(monkeypatch, clock):
    """成功したら 30 分クールダウン: 2 回目は起動しない。"""
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60),
                               creds("tok-new", expires_in=3600)])
    cli = FakeCli(monkeypatch, launches=({"exit": 0},))

    assert claude._refresh_via_cli() is True
    assert claude._cooldown_until == 1000.0 + claude.CLI_REFRESH_COOLDOWN
    assert claude._refresh_via_cli() is False  # クールダウン中
    assert len(cli.popens) == 1


def test_claude_not_found_returns_false_without_cooldown(monkeypatch, clock, tmp_path):
    """claude が見つからなければ False。起動していないのでクールダウンにも入らない。"""
    monkeypatch.setattr(claude, "shutil",
                        types.SimpleNamespace(which=lambda name, path=None: None))
    # 実在しない候補だけに差し替えて、実環境の ~/.local/bin/claude 等を見ない
    monkeypatch.setattr(claude, "_CLAUDE_FALLBACKS",
                        tuple(str(tmp_path / "missing" / n / "claude")
                              for n in ("a", "b", "c", "d")))
    cli = FakeCli(monkeypatch)

    assert claude._refresh_via_cli() is False
    assert cli.popens == []
    assert claude._cooldown_until is None  # 起動していないのでクールダウンにも入らない
    assert claude._refresh_via_cli() is False  # クールダウンではないのでまた探す
    assert cli.popens == []


# --------------------------------------------------- スレッドの同時 1 本制御

def test_spawn_runs_at_most_one_thread(monkeypatch):
    """走っている更新スレッドがあるあいだは次を立てない (実スレッドで確認)。"""
    monkeypatch.setattr(claude, "_spawn_refresh_thread", REAL_SPAWN)
    started, release = threading.Event(), threading.Event()
    done = []

    def blocking_worker():
        started.set()
        release.wait(5)
        done.append(1)

    monkeypatch.setattr(claude, "_refresh_worker", blocking_worker)

    assert claude._spawn_refresh_thread() is True
    assert started.wait(5)
    assert claude._spawn_refresh_thread() is False  # 同時に 1 本だけ
    release.set()
    thread = claude._refresh_thread
    thread.join(5)
    assert done == [1]
    assert claude._spawn_refresh_thread() is True  # 終わればまた立てられる
    claude._refresh_thread.join(5)


def test_spawn_does_nothing_when_off(monkeypatch):
    monkeypatch.setattr(claude, "_spawn_refresh_thread", REAL_SPAWN)
    claude.set_token_refresh("off")
    assert claude._spawn_refresh_thread() is False


def test_worker_skips_when_mode_turned_off(monkeypatch, clock):
    """スレッドが動く前に off に変わっていたら何もしない。"""
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60)])
    cli = FakeCli(monkeypatch)
    claude.set_token_refresh("off")
    claude._refresh_worker()
    assert cli.popens == []


# ------------------------------------------------------- 未設定 (run 以外)

def test_unset_mode_waits_for_run(monkeypatch, clock, sync_refresh):
    """未設定 (run が set_token_refresh する前) は CLI を起動せず off と同じ。"""
    monkeypatch.setattr(claude, "_refresh_mode", None)
    events = []
    FakeKeychain(monkeypatch, [creds("tok-old")], events)
    cli = FakeCli(monkeypatch)
    FakeHttp(monkeypatch, [http_error(401)], events)

    with pytest.raises(claude.CredentialError) as excinfo:
        claude.fetch(NOW)

    assert str(excinfo.value) == "トークン期限切れ（claude を一度起動してください）"
    assert events == ["read", "http"]
    assert cli.popens == []
    assert sync_refresh == []


# --------------------------------------------- 子プロセスの後始末 (atexit / off)

def test_kill_running_child_via_atexit_handler(monkeypatch, clock):
    """atexit で登録したのと同じ _kill_running_child が、生きた子を killpg する。"""
    killed = []

    def fake_kill(proc):
        killed.append(proc)
        proc.alive = False  # SIGKILL したことにする

    monkeypatch.setattr(claude, "_kill_process_group", fake_kill)
    child = RunningProc(alive=True)
    claude._child = child

    claude._kill_running_child()
    assert killed == [child]
    claude._kill_running_child()  # 終わったあとの 2 回目は何もしない
    assert killed == [child]


def test_set_token_refresh_off_kills_running_child(monkeypatch, clock):
    killed = []
    monkeypatch.setattr(claude, "_kill_process_group", lambda proc: killed.append(proc))
    child = RunningProc(alive=True)
    claude._child = child

    claude.set_token_refresh("off")

    assert killed == [child]


def test_set_token_refresh_off_ignores_finished_child(monkeypatch, clock):
    killed = []
    monkeypatch.setattr(claude, "_kill_process_group", lambda proc: killed.append(proc))
    claude._child = RunningProc(alive=False)

    claude.set_token_refresh("off")
    assert killed == []


def test_worker_does_not_request_refresh_when_mode_turned_off(
        monkeypatch, clock, requested_refresh):
    """子が終わったあとに off に変わっていたら request_refresh しない。"""
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60),
                               creds("tok-new", expires_in=3600)])
    cli = FakeCli(monkeypatch, launches=({"exit": 0},))
    claude._refresh_started_at = clock.now

    def ok_then_off():
        claude.set_token_refresh("off")  # 子の終了と同じタイミングで off になった想定
        return True

    monkeypatch.setattr(claude, "_refresh_via_cli", ok_then_off)
    claude._refresh_worker()
    assert len(cli.popens) == 0
    assert requested_refresh == []


# ------------------------------------------- 反映要求を出す前の待ち (gap 対策)

def test_worker_skips_wait_when_gap_already_elapsed(monkeypatch, clock, requested_refresh):
    """更新開始から REFRESH_MIN_GAP+1 秒経っていたら待たずに反映要求を出す。"""
    FakeKeychain(monkeypatch, [creds("tok-old", expires_in=60),
                               creds("tok-new", expires_in=3600)])
    cli = FakeCli(monkeypatch, launches=({"exit": 0},))
    claude._refresh_started_at = clock.now - (refresh_mod.REFRESH_MIN_GAP + 5.0)

    claude._refresh_worker()

    assert clock.sleeps == []        # 待たない
    assert requested_refresh == [1]
    assert len(cli.popens) == 1
