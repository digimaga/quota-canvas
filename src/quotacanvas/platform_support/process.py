"""Existing CLI discovery and POSIX child-process operations."""

import os
import shutil
import signal
import stat
import tempfile
from shutil import rmtree

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


def kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait()
    except OSError:
        pass


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
