"""Existing CLI discovery and POSIX child-process operations."""

import logging
import os
import shutil
import signal
import stat
import subprocess
import tempfile
from shutil import rmtree

log = logging.getLogger("quotacanvas")

PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
ENV_KEYS = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "PATH", "SHELL")
CLAUDE_PATHS = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                "~/.local/bin/claude", "~/.claude/local/claude")
CODEX_PATHS = ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", "~/.codex/bin/codex")
GROK_PATHS = ("/opt/homebrew/bin/grok", "/usr/local/bin/grok", "~/.grok/bin/grok")
AGY_PATH = "/Applications/Gemini.app/Contents/Helpers/agy"


def limited_env(*, grok_home=False):
    keys = ENV_KEYS + (("GROK_HOME",) if grok_home else ())
    env = {key: os.environ[key] for key in keys if key in os.environ}
    extra = os.pathsep + os.environ["PATH"] if os.environ.get("PATH") else ""
    env["PATH"] = PATH_PREFIX + extra
    return env


def find_cli(name, search_path, candidates, *, candidates_first=False, which=None):
    which = which or shutil.which
    def candidate():
        for item in candidates:
            path = os.path.expanduser(item)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None
    if candidates_first:
        return candidate() or which(name, path=search_path)
    return which(name, path=search_path) or candidate()


def start(popen, argv, **kwargs):
    return popen(argv, start_new_session=True, **kwargs)


#: 子プロセスを回収する wait の上限 (秒)。超えたら warning を出して戻る。
KILL_WAIT_TIMEOUT = 5.0


def kill_group(proc, *, wait_timeout=KILL_WAIT_TIMEOUT) -> bool:
    """killpg で子プロセスのグループを終了し、上限付きで回収する。

    終了済み (ESRCH) は何もしない。権限エラーなどそれ以外の失敗は理由を
    warning に残したうえで、待ち合わせは続ける (子プロセス自体は終わるかも
    しれないため)。wait が wait_timeout を超えたら warning を出して戻る。

    戻り値は「終了を確認できたか」。wait がタイムアウトした・エラーになった
    ときは False を返すので、呼び出し側は子プロセスの追跡 (atexit や次回の
    停止対象) を手放さないこと。既存の呼び出しは戻り値を無視しても動く。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass  # すでに終了している (ESRCH)
    except OSError as exc:
        log.warning("子プロセスのグループを終了できません (pid=%s): %s", proc.pid, exc)
    try:
        proc.wait(timeout=wait_timeout)
        return True
    except subprocess.TimeoutExpired:
        log.warning("子プロセス (pid=%s) が %.0f 秒待っても終了しません",
                    proc.pid, wait_timeout)
        return False
    except OSError as exc:
        log.warning("子プロセス (pid=%s) の終了を確認できません: %s", proc.pid, exc)
        return False


def claude_refresh_dir():
    """Create the private empty project used by the Claude refresh command."""
    base = os.path.join(os.path.expanduser("~"), "Library", "Caches", "quota-canvas")
    os.makedirs(base, mode=0o700, exist_ok=True)
    info = os.lstat(base)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("unsafe Claude refresh directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(base, 0o700)
        info = os.lstat(base)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise OSError("unsafe Claude refresh directory")
    with os.scandir(base) as entries:
        for entry in entries:
            if not entry.name.startswith("claude-refresh-"):
                continue
            try:
                old = os.lstat(entry.path)
            except OSError:
                continue
            if stat.S_ISDIR(old.st_mode) and old.st_uid == os.getuid():
                rmtree(entry.path, ignore_errors=True)
    return tempfile.mkdtemp(prefix="claude-refresh-", dir=base)
