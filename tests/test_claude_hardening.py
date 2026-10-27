"""Claude CLI の隔離された作業ディレクトリと表示名の無害化。"""

from __future__ import annotations

import os
import stat
import subprocess
import unicodedata
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quotacanvas.providers import claude


@pytest.fixture(autouse=True)
def private_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "Library" / "Caches" / "quota-canvas"


@pytest.mark.parametrize("outcome", ["success", "timeout", "launch_failure"])
def test_refresh_uses_private_empty_project_directory_and_cleans_up(
        monkeypatch, private_home, outcome):
    before = datetime(2026, 10, 25, tzinfo=timezone.utc)
    expiries = iter([before, before + timedelta(hours=1)])
    monkeypatch.setattr(claude, "_refresh_mode", "cli")
    monkeypatch.setattr(claude, "_cooldown_until", None)
    monkeypatch.setattr(claude, "_child", None)
    monkeypatch.setattr(claude, "_find_claude_cli", lambda path: "/fake/bin/claude")
    monkeypatch.setattr(claude, "_current_expiry", lambda: next(expiries))

    project_dirs = []
    killed = []

    class FakeProcess:
        def wait(self, timeout=None):
            if outcome == "timeout" and timeout is not None:
                raise subprocess.TimeoutExpired("claude", timeout)
            return 0

    def fake_popen(*args, **kwargs):
        cwd = kwargs["cwd"]
        project_dirs.append(cwd)
        assert os.path.dirname(cwd) == str(private_home)
        assert os.path.basename(cwd).startswith("claude-refresh-")
        assert stat.S_IMODE(os.stat(private_home).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(cwd).st_mode) == 0o700
        assert os.listdir(cwd) == []
        if outcome == "launch_failure":
            raise OSError("cannot start")
        return FakeProcess()

    monkeypatch.setattr(claude.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(claude, "_kill_process_group", lambda proc: killed.append(proc))

    assert claude._refresh_via_cli() is (outcome == "success")
    assert len(project_dirs) == 1
    assert not os.path.exists(project_dirs[0])
    assert len(killed) == (1 if outcome == "timeout" else 0)
    assert claude._child is None
    assert (claude._cooldown_until is None) == (outcome == "launch_failure")


def test_mkdtemp_failure_is_a_launch_failure(monkeypatch):
    monkeypatch.setattr(claude, "_refresh_mode", "cli")
    monkeypatch.setattr(claude, "_cooldown_until", None)
    monkeypatch.setattr(claude, "_find_claude_cli", lambda path: "/fake/bin/claude")
    monkeypatch.setattr(claude, "_current_expiry", lambda: None)

    def fail_mkdtemp(*args, **kwargs):
        raise OSError("no private directory")

    def unexpected_popen(*args, **kwargs):
        pytest.fail("Popen must not run when mkdtemp fails")

    monkeypatch.setattr(claude.tempfile, "mkdtemp", fail_mkdtemp)
    monkeypatch.setattr(claude.subprocess, "Popen", unexpected_popen)

    assert claude._refresh_via_cli() is False
    assert claude._cooldown_until is None


def test_symlink_cache_base_prevents_launch(monkeypatch, private_home):
    private_home.parent.mkdir(parents=True)
    target = private_home.parent / "target"
    target.mkdir()
    private_home.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(claude, "_refresh_mode", "cli")
    monkeypatch.setattr(claude, "_cooldown_until", None)
    monkeypatch.setattr(claude, "_find_claude_cli", lambda path: "/fake/bin/claude")
    monkeypatch.setattr(claude, "_current_expiry", lambda: None)
    monkeypatch.setattr(claude.subprocess, "Popen", lambda *a, **k:
                        pytest.fail("Popen must not run with an untrusted cache base"))

    assert claude._refresh_via_cli() is False
    assert claude._cooldown_until is None


@pytest.mark.parametrize("old_mode", [0o755, 0o770, 0o707])
def test_existing_cache_base_is_restricted_to_0700(
        monkeypatch, private_home, old_mode):
    private_home.mkdir(parents=True)
    private_home.chmod(old_mode)
    before = datetime(2026, 10, 25, tzinfo=timezone.utc)
    expiries = iter([before, before + timedelta(hours=1)])
    monkeypatch.setattr(claude, "_refresh_mode", "cli")
    monkeypatch.setattr(claude, "_cooldown_until", None)
    monkeypatch.setattr(claude, "_find_claude_cli", lambda path: "/fake/bin/claude")
    monkeypatch.setattr(claude, "_current_expiry", lambda: next(expiries))

    class FakeProcess:
        def wait(self, timeout=None):
            return 0

    def fake_popen(*args, **kwargs):
        assert stat.S_IMODE(os.stat(private_home).st_mode) == 0o700
        return FakeProcess()

    monkeypatch.setattr(claude.subprocess, "Popen", fake_popen)

    assert claude._refresh_via_cli() is True
    assert stat.S_IMODE(os.stat(private_home).st_mode) == 0o700


def test_chmod_failure_prevents_launch(monkeypatch, private_home, caplog):
    private_home.mkdir(parents=True)
    private_home.chmod(0o755)
    monkeypatch.setattr(claude, "_refresh_mode", "cli")
    monkeypatch.setattr(claude, "_cooldown_until", None)
    monkeypatch.setattr(claude, "_find_claude_cli", lambda path: "/fake/bin/claude")
    monkeypatch.setattr(claude, "_current_expiry", lambda: None)

    def fail_chmod(*args, **kwargs):
        raise OSError("chmod unavailable")

    monkeypatch.setattr(claude.os, "chmod", fail_chmod)
    monkeypatch.setattr(claude.subprocess, "Popen", lambda *a, **k:
                        pytest.fail("Popen must not run after chmod failure"))

    assert claude._refresh_via_cli() is False
    assert claude._cooldown_until is None
    assert "claude CLI を起動できなかった" in caplog.text


def test_stale_refresh_directories_are_selected_before_launch(
        monkeypatch, private_home, tmp_path):
    private_home.mkdir(parents=True)
    stale = private_home / "claude-refresh-stale"
    stale.mkdir()
    (stale / "CLAUDE.md").write_text("stale")
    unrelated = private_home / "unrelated"
    unrelated.mkdir()
    prefixed_file = private_home / "claude-refresh-file"
    prefixed_file.write_text("keep")
    target = tmp_path / "target"
    target.mkdir()
    prefixed_link = private_home / "claude-refresh-link"
    prefixed_link.symlink_to(target, target_is_directory=True)
    foreign = private_home / "claude-refresh-foreign"
    foreign.mkdir()
    real_lstat = os.lstat

    def selected_lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if os.fspath(path) == str(foreign):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
        return info

    monkeypatch.setattr(claude.os, "lstat", selected_lstat)
    monkeypatch.setattr(claude, "_refresh_mode", "cli")
    monkeypatch.setattr(claude, "_cooldown_until", None)
    monkeypatch.setattr(claude, "_find_claude_cli", lambda path: "/fake/bin/claude")
    before = datetime(2026, 10, 25, tzinfo=timezone.utc)
    expiries = iter([before, before + timedelta(hours=1)])
    monkeypatch.setattr(claude, "_current_expiry", lambda: next(expiries))

    class FakeProcess:
        def wait(self, timeout=None):
            return 0

    def fake_popen(*args, **kwargs):
        assert not stale.exists()
        assert unrelated.is_dir()
        assert prefixed_file.is_file()
        assert prefixed_link.is_symlink()
        assert target.is_dir()
        assert foreign.is_dir()
        assert os.path.dirname(kwargs["cwd"]) == str(private_home)
        return FakeProcess()

    monkeypatch.setattr(claude.subprocess, "Popen", fake_popen)

    assert claude._refresh_via_cli() is True


def test_foreign_owned_cache_base_prevents_launch(monkeypatch, private_home):
    private_home.mkdir(parents=True)
    real_lstat = os.lstat

    def foreign_owner(path):
        info = real_lstat(path)
        if path == str(private_home):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
        return info

    monkeypatch.setattr(claude.os, "lstat", foreign_owner)
    monkeypatch.setattr(claude, "_refresh_mode", "cli")
    monkeypatch.setattr(claude, "_cooldown_until", None)
    monkeypatch.setattr(claude, "_find_claude_cli", lambda path: "/fake/bin/claude")
    monkeypatch.setattr(claude, "_current_expiry", lambda: None)
    monkeypatch.setattr(claude.subprocess, "Popen", lambda *a, **k:
                        pytest.fail("Popen must not run with a foreign-owned cache base"))

    assert claude._refresh_via_cli() is False
    assert claude._cooldown_until is None


def test_weekly_scoped_name_removes_unicode_controls_from_id_and_label():
    data = {"limits": [
        {"kind": "weekly_scoped", "scope": {"model": {
            "display_name": "  \x1bOp\u202eeus\u200b  "}}, "percent": 25},
        {"kind": "weekly_scoped", "scope": {"model": {
            "display_name": "\x1b\u202e \t"}}, "percent": 40},
    ]}

    _, windows = claude.parse_claude(data)

    assert len(windows) == 1
    assert windows[0].key == "weekly_opeus"
    assert windows[0].id == "claude:weekly_opeus"
    assert "Opeus" in windows[0].label
    for value in (windows[0].id, windows[0].label):
        assert all(not unicodedata.category(ch).startswith("C") for ch in value)
