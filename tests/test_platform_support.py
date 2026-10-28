"""Platform boundaries retain the current behavior with dummy inputs."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from quotacanvas import i18n, keychain
from quotacanvas.platform_support import appearance, paths, process, security
from quotacanvas.providers import claude


def test_paths_keep_existing_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert paths.config_path() == tmp_path / ".config" / "quota-canvas" / "config.toml"
    assert paths.data_dir() == tmp_path / "Library" / "Application Support" / "quota-canvas"


def test_discovery_order_and_limited_env(monkeypatch, tmp_path):
    candidate = tmp_path / "agy"
    candidate.write_text("dummy", encoding="utf-8")
    candidate.chmod(0o700)
    called = []
    def which(name, path=None):
        called.append((name, path))
        return "/dummy/path"
    assert process.find_cli("agy", "p", (str(candidate),), candidates_first=True, which=which) == str(candidate)
    assert called == []
    assert process.find_cli("agy", "p", (str(candidate),), which=which) == "/dummy/path"
    monkeypatch.setenv("PATH", "/dummy/bin")
    monkeypatch.setenv("GROK_HOME", "/dummy/grok")
    monkeypatch.setenv("QUOTA_CANVAS_TEST_SECRET", "dummy-secret")
    env = process.limited_env(grok_home=True)
    assert env["GROK_HOME"] == "/dummy/grok"
    assert env["PATH"] == process.PATH_PREFIX + process.os.pathsep + "/dummy/bin"
    assert "QUOTA_CANVAS_TEST_SECRET" not in env
    assert "GROK_HOME" not in process.limited_env()


def test_security_boundary_preserves_provider_errors(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=44, stdout="")
    monkeypatch.setattr(security.subprocess, "run", run)
    assert keychain.read("glm") is None
    with pytest.raises(claude.CredentialError):
        claude.read_claude_credentials()
    assert calls[0][0] == ["security", "find-generic-password", "-a", "glm", "-s", "quota-canvas", "-w"]
    assert calls[1][0] == ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]


def test_security_unavailable_and_timeout_remain_distinct(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("missing")
    monkeypatch.setattr(security.subprocess, "run", fail)
    with pytest.raises(keychain.KeychainError):
        keychain.read("glm")
    with pytest.raises(claude.CredentialError, match="security"):
        claude.read_claude_credentials()
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("security", 1)
    monkeypatch.setattr(security.subprocess, "run", timeout)
    with pytest.raises(claude.CredentialError, match="timeout"):
        claude.read_claude_credentials()


def test_language_and_font_candidates_keep_current_behavior(monkeypatch):
    monkeypatch.setattr(appearance, "language_output", lambda: '(\n    "en-US",\n)')
    assert i18n.system_language() == "en"
    def unavailable():
        raise OSError("missing")
    monkeypatch.setattr(appearance, "language_output", unavailable)
    assert i18n.system_language() == "ja"
    assert appearance.font_paths("w8") == (
        appearance.FONT_ROOT + "ヒラギノ角ゴシック W8.ttc",
        appearance.FONT_ROOT + "ヒラギノ角ゴシック W7.ttc",
        appearance.FONT_ROOT + "ヒラギノ角ゴシック W3.ttc",
    )


def test_default_state_paths_share_the_existing_directory():
    from quotacanvas import gate, history, lastgood, periods, refresh, status

    root = paths.data_dir()
    assert history.DEFAULT_PATH == root / "history.jsonl"
    assert periods.DEFAULT_PATH == root / "periods.jsonl"
    assert lastgood.DEFAULT_PATH == root / "last-good.json"
    assert status.STATUS_PATH == root / "status.json"
    assert status.FRAME_PATH == root / "frame.png"
    assert gate.BURN_PATH == root / "routing-burn.json"
    assert refresh.REFRESH_REQUEST_PATH == root / "refresh-request"


def test_turzx_pattern_uses_existing_font_without_usb(monkeypatch):
    from turzx import cli

    img = object()
    font_calls = []
    draw = SimpleNamespace(rectangle=lambda *a, **kw: None, text=lambda *a, **kw: None)
    monkeypatch.setattr(cli.Image, "new", lambda *a, **kw: img)
    monkeypatch.setattr(cli.ImageDraw, "Draw", lambda image: draw)
    monkeypatch.setattr(cli.ImageFont, "truetype", lambda path, size: font_calls.append((path, size)))
    assert cli.test_pattern() is img
    assert font_calls == [("/System/Library/Fonts/Helvetica.ttc", 200),
                          ("/System/Library/Fonts/Helvetica.ttc", 60)]
