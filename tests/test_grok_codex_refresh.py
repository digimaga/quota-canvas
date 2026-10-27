"""Grok / Codex のトークン自動更新 (providers.<p>.token_refresh = "cli" / "off") のテスト。

subprocess.Popen・認証情報の読み出し (grok._read_entry / codex.read_codex_credentials
/ codex._read_auth_state)・http_get_json・shutil.which をすべてモックし、実際の
grok / codex CLI・auth.json・ネットワークには絶対に触れない (autouse の fake が
両モジュールに効くので、どちらか片方のテストからもう片方の CLI が誤って
起動されることもない)。更新スレッドは既定で同期実行に差し替え、スレッド自身の
挙動 (同時 1 本) だけ実スレッドで確かめる。クールダウンなどのモジュール状態は
テストごとにリセットする。
"""

from __future__ import annotations

import signal
import subprocess
import tempfile
import threading
import types
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import refresh as refresh_mod
from quotacanvas.providers import codex, grok

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
EXPIRED = NOW - timedelta(minutes=10)
FRESH = NOW + timedelta(hours=8)

GROK_PAYLOAD = {"config": {
    "currentPeriod": {"start": "2026-10-19T01:45:37+00:00",
                      "end": "2026-10-26T01:45:37+00:00"},
    "creditUsagePercent": 22.0,
}}
CODEX_PAYLOAD = {"rate_limit": {
    "primary_window": {"used_percent": 12.5, "limit_window_seconds": 18000,
                       "reset_at": 1792872000},
}}

MODULES = (grok, codex)

# autouse fixture が差し替える前の本物の実装 (単フライトのテストで使う)
REAL_SPAWN = {mod: mod._spawn_refresh_thread for mod in MODULES}


def http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "Error", None, None)


def stale_state(mod):
    """更新前 (期限切れ / トークンが古い) の認証状態。"""
    return ("key-old", EXPIRED) if mod is grok else ("tok-old", EXPIRED)


def fresh_state(mod):
    """更新後の認証状態。"""
    return ("key-new", FRESH) if mod is grok else ("tok-new", FRESH)


# ------------------------------------------------------------------- fixtures

@pytest.fixture(params=MODULES, ids=["grok", "codex"])
def mod(request):
    return request.param


class FakeClock:
    """<module>.time の差し替え。sleep は実際には待たずに記録するだけ。"""

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
    for mod in MODULES:
        monkeypatch.setattr(mod, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """モジュール状態をテストごとにリセットする (モードは未設定に戻す)。"""
    for mod in MODULES:
        for name in ("_refresh_mode", "_cooldown_until", "_last_not_found",
                     "_refresh_thread", "_refresh_started_at", "_child"):
            monkeypatch.setattr(mod, name, None)
    monkeypatch.setattr(grok, "_soon_retries", 0)


@pytest.fixture(autouse=True)
def run_cli_mode(_reset_state):
    """ほとんどのテストは run ループ相当 ("cli" モード) で行う。
    未設定 (None) の挙動は、テスト側で明示的に None に戻して確かめる。"""
    for mod in MODULES:
        mod.set_token_refresh("cli")


@pytest.fixture(autouse=True)
def _fake_cli_path(monkeypatch, tmp_path):
    """shutil.which が CLI を見つけたことにする (実行は FakeCli が吸収する)。"""
    for mod in MODULES:
        name = "grok" if mod is grok else "codex"
        monkeypatch.setattr(mod, "shutil",
                            types.SimpleNamespace(
                                which=lambda cmd, path=None, _n=name: "/fake/bin/" + _n))
        monkeypatch.setattr(mod, "_GROK_FALLBACKS" if mod is grok else "_CODEX_FALLBACKS",
                            tuple(str(tmp_path / "missing" / n / name)
                                  for n in ("a", "b", "c")))


class FakeStates:
    """認証情報の読み出し順序を差し替える。

    states: 読み出し 1 回ごとの (token_or_key, expires_or_last_refresh)。
    足りなくなったら最後の要素を使い回す。
    """

    def __init__(self, monkeypatch, mod, states):
        self.mod = mod
        self.states = list(states)
        self.calls = 0
        if mod is grok:
            monkeypatch.setattr(mod, "_read_entry", self)
        else:
            monkeypatch.setattr(mod, "_read_auth_state", self)
            monkeypatch.setattr(mod, "read_codex_credentials",
                                lambda: ("codex-token", "acct-1"))

    def __call__(self, *args):
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        return state


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
            raise subprocess.TimeoutExpired(cmd="cli", timeout=timeout)
        return self.returncode


class FakeCli:
    """モジュール内の subprocess を丸ごと差し替える (Popen は記録だけ)。

    launches: 起動 1 回ごとの script ({"exit": rc} / {"timeout": True} /
    {"popen": "oserror"})。足りなくなったら最後の要素を使い回す。
    popens に (argv, kwargs)、procs に FakeProc を記録する。
    """

    def __init__(self, monkeypatch, mod, launches=({"exit": 0},)):
        self.mod = mod
        self.popens = []
        self.procs = []
        self.launches = list(launches)
        fake = types.SimpleNamespace(
            Popen=self._popen,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        )
        monkeypatch.setattr(mod, "subprocess", fake)

    def configure(self, *launches):
        """起動ごとの script を差し替える (autouse の fake を使い回す)。"""
        self.launches = list(launches)

    def _popen(self, argv, **kwargs):
        index = len(self.popens)
        self.popens.append((list(argv), kwargs))
        script = self.launches[min(index, len(self.launches) - 1)]
        if script.get("popen") == "oserror":
            raise OSError("cannot start")
        proc = FakeProc(script)
        self.procs.append(proc)
        return proc


@pytest.fixture(autouse=True)
def cli(monkeypatch):
    """両モジュールの subprocess を FakeCli に替える (うっかり実行を防ぐ)。"""
    fakes = {mod: FakeCli(monkeypatch, mod) for mod in MODULES}
    return fakes


class FakeHttp:
    """http_get_json を差し替える。

    outcomes: 呼び出しごとの結果。成功は dict、失敗は例外インスタンス。
    足りなくなったら最後の要素を使い回す。
    """

    def __init__(self, monkeypatch, mod, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        monkeypatch.setattr(mod, "http_get_json", self)

    def __call__(self, url, headers, timeout=None):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    """既定ではどのモジュールも HTTP しない (うっかり実行を防ぐ)。"""
    for mod in MODULES:
        def blocked(url, headers, timeout=None):
            raise AssertionError("unexpected http call: " + url)
        monkeypatch.setattr(mod, "http_get_json", blocked)


@pytest.fixture(autouse=True)
def requested_refresh(monkeypatch):
    """request_refresh をモックし、呼ばれた回数を記録する (実データを守る)。"""
    requested = []
    monkeypatch.setattr(refresh_mod, "request_refresh", lambda *a, **k: requested.append(1))
    return requested


@pytest.fixture(autouse=True)
def sync_refresh(monkeypatch, clock):
    """両モジュールの _spawn_refresh_thread を同期実行に差し替える
    (worker をその場で呼ぶ。実スレッドは立たない)。"""
    spawns = {mod: [] for mod in MODULES}

    def make_inline(m):
        def inline():
            spawns[m].append(1)
            m._refresh_started_at = m.time.monotonic()
            m._refresh_worker()
            return True
        return inline

    for mod in MODULES:
        monkeypatch.setattr(mod, "_spawn_refresh_thread", make_inline(mod))
    return spawns


# ----------------------------------------------------------- token_refresh 設定

def test_set_token_refresh_rejects_unknown(mod):
    with pytest.raises(ValueError):
        mod.set_token_refresh("auto")


# ------------------------------------------------------------------- off のとき

def test_off_never_calls_cli(monkeypatch, mod, clock, sync_refresh, cli):
    """off では CLI を呼ばず、従来どおりのエラーのまま (grok: 期限切れの
    CredentialError / codex: HTTP 401 がそのまま伝わる)。"""
    mod.set_token_refresh("off")
    if mod is grok:
        FakeStates(monkeypatch, mod, [stale_state(mod)])
        with pytest.raises(mod.CredentialError) as excinfo:
            mod.fetch(NOW)
        assert str(excinfo.value) == "ログインの期限切れ（grok を一度起動してください）"
    else:
        FakeHttp(monkeypatch, mod, [http_error(401)])
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            mod.fetch(NOW)
        assert excinfo.value.code == 401
    assert cli[mod].popens == []
    assert sync_refresh[mod] == []


def test_unset_mode_waits_for_run(monkeypatch, mod, clock, sync_refresh, cli):
    """未設定 (run が set_token_refresh する前) は CLI を起動せず off と同じ。"""
    monkeypatch.setattr(mod, "_refresh_mode", None)
    if mod is grok:
        FakeStates(monkeypatch, mod, [stale_state(mod)])
        with pytest.raises(mod.CredentialError) as excinfo:
            mod.fetch(NOW)
        assert str(excinfo.value) == "ログインの期限切れ（grok を一度起動してください）"
    else:
        FakeHttp(monkeypatch, mod, [http_error(401)])
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            mod.fetch(NOW)
        assert excinfo.value.code == 401
    assert cli[mod].popens == []
    assert sync_refresh[mod] == []


def test_off_message_english(monkeypatch, clock, sync_refresh, cli):
    """en のとき grok off の期限切れ案内文も英語になる。"""
    from quotacanvas import i18n

    grok.set_token_refresh("off")
    FakeStates(monkeypatch, grok, [stale_state(grok)])

    i18n.set_language("en")
    try:
        with pytest.raises(grok.CredentialError) as excinfo:
            grok.fetch(NOW)
        assert str(excinfo.value) == "Login expired (run grok once)"
    finally:
        i18n.set_language("ja")
    assert cli[grok].popens == []


def test_refreshing_message_english(monkeypatch, mod, clock, sync_refresh, cli):
    """en のとき更新中のエラー文も英語になる。"""
    from quotacanvas import i18n

    FakeStates(monkeypatch, mod, [stale_state(mod), fresh_state(mod)])
    if mod is codex:
        FakeHttp(monkeypatch, mod, [http_error(401)])
    cli[mod].configure({"exit": 0})

    i18n.set_language("en")
    try:
        with pytest.raises(mod.CredentialError) as excinfo:
            mod.fetch(NOW)
        assert str(excinfo.value) == (
            "Login expired (auto-refreshing)" if mod is grok
            else "Token expired (auto-refreshing)")
    finally:
        i18n.set_language("ja")


# ------------------------------------------------------------------- cli のとき

def test_fetch_error_starts_refresh_and_reports_auto_refresh(
        monkeypatch, mod, clock, sync_refresh, requested_refresh, cli):
    """cli では期限切れ / 401 で更新を開始し「自動更新中」を上げる。
    更新が成功したら run ループに反映要求を出し、次の取得は成功する。"""
    if mod is grok:
        # fetch の読み (期限切れ) → 更新前後の読み
        states = FakeStates(monkeypatch, mod,
                            [stale_state(mod), stale_state(mod), fresh_state(mod)])
    else:
        states = FakeStates(monkeypatch, mod, [stale_state(mod), fresh_state(mod)])
        FakeHttp(monkeypatch, mod, [http_error(401), CODEX_PAYLOAD])
    cli[mod].configure({"exit": 0})

    with pytest.raises(mod.CredentialError) as excinfo:
        mod.fetch(NOW)

    assert str(excinfo.value) == (
        "ログインの期限切れ（自動更新中）" if mod is grok else "トークン期限切れ（自動更新中）")
    assert sync_refresh[mod] == [1]      # 更新を開始した
    assert len(cli[mod].popens) == 1
    assert requested_refresh == [1]      # 成功したので run ループに反映要求
    assert states.calls == (3 if mod is grok else 2)  # fetch / 更新前 / 更新後
    # REFRESH_MIN_GAP (30 秒) で捨てられないよう、開始から gap+1 秒待ってから出す
    assert clock.sleeps == [refresh_mod.REFRESH_MIN_GAP + 1.0]

    # 次の取得は認証情報が更新されていて成功する
    if mod is grok:
        FakeStates(monkeypatch, mod, [fresh_state(mod)])
        FakeHttp(monkeypatch, mod, [GROK_PAYLOAD])
    usage = mod.fetch(NOW)
    assert usage.provider == ("grok" if mod is grok else "codex")
    assert usage.error is None


def test_grok_http_401_starts_refresh(monkeypatch, mod, clock, sync_refresh,
                                      requested_refresh, cli):
    """grok: ローカルの expires_at が先でなくても、401 なら更新を開始する。"""
    if mod is not grok:
        pytest.skip("grok 専用")
    FakeStates(monkeypatch, grok, [stale_state(grok), stale_state(grok), fresh_state(grok)])
    FakeHttp(monkeypatch, grok, [http_error(401)])
    cli[grok].configure({"exit": 0})

    with pytest.raises(grok.CredentialError) as excinfo:
        grok.fetch(NOW)

    assert str(excinfo.value) == "ログインの期限切れ（自動更新中）"
    assert len(cli[grok].popens) == 1
    assert requested_refresh == [1]


def test_codex_403_is_treated_like_401(monkeypatch, mod, clock, sync_refresh, cli):
    if mod is not codex:
        pytest.skip("codex 専用")
    FakeStates(monkeypatch, codex, [stale_state(codex), fresh_state(codex)])
    FakeHttp(monkeypatch, codex, [http_error(403)])
    cli[codex].configure({"exit": 0})

    with pytest.raises(codex.CredentialError) as excinfo:
        codex.fetch(NOW)
    assert str(excinfo.value) == "トークン期限切れ（自動更新中）"
    assert len(cli[codex].popens) == 1


def test_missing_credentials_never_spawns_cli(monkeypatch, mod, clock, sync_refresh, cli):
    """認証情報そのものが無いときは更新させず、素のエラーのまま。"""
    if mod is grok:
        def no_entry(*args):
            raise grok.CredentialError("認証情報なし")
        monkeypatch.setattr(grok, "_read_entry", no_entry)
    else:
        def no_creds():
            raise codex.CredentialError("認証情報なし")
        monkeypatch.setattr(codex, "read_codex_credentials", no_creds)

    with pytest.raises(mod.CredentialError) as excinfo:
        mod.fetch(NOW)
    assert str(excinfo.value) == "認証情報なし"
    assert cli[mod].popens == []
    assert sync_refresh[mod] == []


# ------------------------------------------------- _refresh_via_cli の成否判定

def test_refresh_success_requires_advance(monkeypatch, mod, clock, requested_refresh, cli):
    """exit 0 でも認証情報が進んでいなければ失敗。"""
    stale = stale_state(mod)
    FakeStates(monkeypatch, mod, [stale, stale])  # 更新前後とも古いまま
    cli[mod].configure({"exit": 0}, {"exit": 0})

    assert mod._refresh_via_cli() is False
    assert len(cli[mod].popens) == 1
    assert requested_refresh == []


def test_grok_unadvanced_retries_soon_up_to_limit(monkeypatch, clock, cli):
    """grok models は枠を使わないので 5 分で再試行するが、続くのは
    CLI_RETRY_SOON_MAX 回まで。その次は 30 分あける。"""
    stale = stale_state(grok)
    for i in range(grok.CLI_RETRY_SOON_MAX):
        FakeStates(monkeypatch, grok, [stale, stale])
        cli[grok].configure({"exit": 0})
        assert grok._refresh_via_cli() is False
        assert grok._cooldown_until == clock.now + grok.CLI_RETRY_SOON
        clock.advance(grok.CLI_RETRY_SOON + 1.0)
    FakeStates(monkeypatch, grok, [stale, stale])
    cli[grok].configure({"exit": 0})
    assert grok._refresh_via_cli() is False
    assert grok._cooldown_until == clock.now + grok.CLI_REFRESH_COOLDOWN

    clock.advance(grok.CLI_REFRESH_COOLDOWN + 1.0)
    FakeStates(monkeypatch, grok, [stale, fresh_state(grok)])
    cli[grok].configure({"exit": 0})
    assert grok._refresh_via_cli() is True
    assert grok._soon_retries == 0  # 成功で数え直し


def test_codex_unadvanced_keeps_full_cooldown(monkeypatch, clock, cli):
    """codex exec は枠を使うので、トークンが進まなくても 30 分あける。"""
    stale = stale_state(codex)
    FakeStates(monkeypatch, codex, [stale, stale])
    cli[codex].configure({"exit": 0})
    assert codex._refresh_via_cli() is False
    assert codex._cooldown_until == clock.now + codex.CLI_REFRESH_COOLDOWN
    assert not hasattr(codex, "CLI_RETRY_SOON")


def test_grok_child_env_keeps_grok_home(monkeypatch):
    monkeypatch.setenv("GROK_HOME", "/tmp/grok-home-test")
    assert grok._prefixed_env()["GROK_HOME"] == "/tmp/grok-home-test"
    monkeypatch.delenv("GROK_HOME")
    assert "GROK_HOME" not in grok._prefixed_env()


def test_refresh_nonzero_exit_fails(monkeypatch, mod, clock, cli):
    FakeStates(monkeypatch, mod, [stale_state(mod)])
    cli[mod].configure({"exit": 1})

    assert mod._refresh_via_cli() is False
    assert len(cli[mod].popens) == 1
    assert mod._cooldown_until == 1000.0 + mod.CLI_REFRESH_COOLDOWN  # 30 分


def test_popen_failure_does_not_start_cooldown(monkeypatch, mod, clock, cli):
    """Popen 失敗 (起動できず) はクールダウンに入れない: すぐ再試行できる。"""
    FakeStates(monkeypatch, mod, [stale_state(mod), stale_state(mod), fresh_state(mod)])
    cli[mod].configure({"popen": "oserror"}, {"exit": 0})

    assert mod._refresh_via_cli() is False       # 1 回目: 起動に失敗
    assert mod._cooldown_until is None           # 起動できていないのでクールダウンにも入らない
    assert mod._refresh_via_cli() is True        # 2 回目: クールダウンなしですぐ起動
    assert len(cli[mod].popens) == 2


def test_timeout_kills_process_group(monkeypatch, mod, clock, cli):
    """タイムアウトしたらプロセスグループを SIGKILL してから待つ。"""
    FakeStates(monkeypatch, mod, [stale_state(mod)])
    cli[mod].configure({"timeout": True})
    killed = []
    monkeypatch.setattr(mod.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(mod.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))

    assert mod._refresh_via_cli() is False
    assert killed == [(42424 + 1, signal.SIGKILL)]  # proc.pid のグループを kill
    assert cli[mod].procs[0].waits == [120.0, None]  # timeout 付き待ち → kill 後の始末待ち
    assert mod._child is None                        # 子の記録は片付く
    assert mod._cooldown_until == 1000.0 + mod.CLI_REFRESH_COOLDOWN  # 30 分


def test_launch_env_is_restricted_and_args_match(monkeypatch, mod, clock, cli):
    """子プロセスの env は 7 変数だけ、PATH は前付け + 元 PATH + バイナリの dir。"""
    for key, value in (("HOME", "/home/tester"), ("USER", "tester"),
                       ("LOGNAME", "tester"), ("TMPDIR", "/tmp"),
                       ("LANG", "ja_JP.UTF-8"), ("SHELL", "/bin/zsh")):
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/original/bin:/usr/bin")
    monkeypatch.setenv("QUOTA_CANVAS_SECRET", "leak-me")  # 漏れてはいけない
    monkeypatch.delenv("GROK_HOME", raising=False)  # grok は設定されていれば渡す (別テスト)
    FakeStates(monkeypatch, mod, [stale_state(mod), fresh_state(mod)])
    cli[mod].configure({"exit": 0})

    assert mod._refresh_via_cli() is True

    argv, kwargs = cli[mod].popens[0]
    if mod is grok:
        assert argv == ["/fake/bin/grok", "models"]  # モデルを呼ばない (枠を消費しない)
    else:
        assert argv == ["/fake/bin/codex", "exec", "--skip-git-repo-check",
                        "--sandbox", "read-only", "--ephemeral",
                        "-C", tempfile.gettempdir(), "ok"]
    env = kwargs["env"]
    assert set(env) == {"HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "PATH", "SHELL"}
    assert "leak-me" not in env.values()
    parts = env["PATH"].split(":")
    assert parts[:4] == ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    assert "/usr/original/bin" in parts and "/usr/bin" in parts
    assert parts[-1] == "/fake/bin"  # 見つけたバイナリのディレクトリも足す
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == tempfile.gettempdir()
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert mod._child is None  # 成功後も子の記録は片付く


def test_success_starts_cooldown(monkeypatch, mod, clock, cli):
    """成功したら 30 分クールダウン: 2 回目は起動しない。"""
    FakeStates(monkeypatch, mod, [stale_state(mod), fresh_state(mod), fresh_state(mod)])

    assert mod._refresh_via_cli() is True
    assert mod._cooldown_until == 1000.0 + mod.CLI_REFRESH_COOLDOWN
    assert mod._refresh_via_cli() is False  # クールダウン中
    assert len(cli[mod].popens) == 1


def test_cli_not_found_returns_false_without_cooldown(
        monkeypatch, mod, clock, cli, tmp_path):
    """CLI が見つからなければ False。起動していないのでクールダウンにも入らない。"""
    monkeypatch.setattr(mod, "shutil",
                        types.SimpleNamespace(which=lambda cmd, path=None: None))

    assert mod._refresh_via_cli() is False
    assert cli[mod].popens == []
    assert mod._cooldown_until is None  # 起動していないのでクールダウンにも入らない
    assert mod._refresh_via_cli() is False  # クールダウンではないのでまた探す
    assert cli[mod].popens == []


# --------------------------------------------------- スレッドの同時 1 本制御

def test_spawn_runs_at_most_one_thread(monkeypatch, mod):
    """走っている更新スレッドがあるあいだは次を立てない (実スレッドで確認)。"""
    monkeypatch.setattr(mod, "_spawn_refresh_thread", REAL_SPAWN[mod])
    started, release = threading.Event(), threading.Event()
    done = []

    def blocking_worker():
        started.set()
        release.wait(5)
        done.append(1)

    monkeypatch.setattr(mod, "_refresh_worker", blocking_worker)

    assert mod._spawn_refresh_thread() is True
    assert started.wait(5)
    assert mod._spawn_refresh_thread() is False  # 同時に 1 本だけ
    release.set()
    thread = mod._refresh_thread
    thread.join(5)
    assert done == [1]
    assert mod._spawn_refresh_thread() is True  # 終わればまた立てられる
    mod._refresh_thread.join(5)


def test_spawn_does_nothing_when_off(monkeypatch, mod):
    monkeypatch.setattr(mod, "_spawn_refresh_thread", REAL_SPAWN[mod])
    mod.set_token_refresh("off")
    assert mod._spawn_refresh_thread() is False


def test_worker_skips_when_mode_turned_off(monkeypatch, mod, clock, cli):
    """スレッドが動く前に off に変わっていたら何もしない。"""
    mod.set_token_refresh("off")
    mod._refresh_worker()
    assert cli[mod].popens == []


# --------------------------------------- 子プロセスの後始末 (atexit / off)

def test_kill_running_child_via_atexit_handler(monkeypatch, mod, clock):
    """atexit で登録したのと同じ _kill_running_child が、生きた子を killpg する。"""
    killed = []

    def fake_kill(proc):
        killed.append(proc)
        proc.alive = False  # SIGKILL したことにする

    monkeypatch.setattr(mod, "_kill_process_group", fake_kill)
    child = RunningProc(alive=True)
    mod._child = child

    mod._kill_running_child()
    assert killed == [child]
    mod._kill_running_child()  # 終わったあとの 2 回目は何もしない
    assert killed == [child]


def test_set_token_refresh_off_kills_running_child(monkeypatch, mod, clock):
    killed = []
    monkeypatch.setattr(mod, "_kill_process_group", lambda proc: killed.append(proc))
    child = RunningProc(alive=True)
    mod._child = child

    mod.set_token_refresh("off")

    assert killed == [child]


def test_set_token_refresh_off_ignores_finished_child(monkeypatch, mod, clock):
    killed = []
    monkeypatch.setattr(mod, "_kill_process_group", lambda proc: killed.append(proc))
    mod._child = RunningProc(alive=False)

    mod.set_token_refresh("off")
    assert killed == []


def test_worker_does_not_request_refresh_when_mode_turned_off(
        monkeypatch, mod, clock, requested_refresh, cli):
    """子が終わったあとに off に変わっていたら request_refresh しない。"""
    mod._refresh_started_at = clock.now

    def ok_then_off():
        mod.set_token_refresh("off")  # 子の終了と同じタイミングで off になった想定
        return True

    monkeypatch.setattr(mod, "_refresh_via_cli", ok_then_off)
    mod._refresh_worker()
    assert cli[mod].popens == []
    assert requested_refresh == []


# ------------------------------------------- 反映要求を出す前の待ち (gap 対策)

def test_worker_skips_wait_when_gap_already_elapsed(
        monkeypatch, mod, clock, requested_refresh, cli):
    """更新開始から REFRESH_MIN_GAP+1 秒経っていたら待たずに反映要求を出す。"""
    FakeStates(monkeypatch, mod, [stale_state(mod), fresh_state(mod)])
    cli[mod].configure({"exit": 0})
    mod._refresh_started_at = clock.now - (refresh_mod.REFRESH_MIN_GAP + 5.0)

    mod._refresh_worker()

    assert clock.sleeps == []        # 待たない
    assert requested_refresh == [1]
    assert len(cli[mod].popens) == 1
