"""Tests for keychain.py. subprocess はモックし、本物のキーチェーンには触れない。"""

from __future__ import annotations

import pytest

from quotacanvas import keychain
from quotacanvas.keychain import KeychainError


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture_run(monkeypatch, returncode=0, stdout="", stderr=""):
    """subprocess.run を差し替え、(args, input) を記録する。"""
    calls = []

    def fake_run(args, input=None, capture_output=False, text=False, timeout=None):
        calls.append({"args": list(args), "input": input, "timeout": timeout})
        return FakeProc(returncode, stdout, stderr)

    monkeypatch.setattr(keychain.subprocess, "run", fake_run)
    return calls


# ------------------------------------------------------------------ write

def test_write_passes_value_via_stdin_never_argv(monkeypatch):
    # 書き込み後の読み戻し確認も成功させる
    monkeypatch.setattr(keychain, "read", lambda account, service=keychain.SERVICE: "secret")
    calls = _capture_run(monkeypatch)
    keychain.write("glm", "secret")
    assert len(calls) == 1
    args = calls[0]["args"]
    assert args[0] == "security" and args[1] == "add-generic-password"
    assert "-U" in args and args[-1] == "-w"
    assert "-a" in args and "glm" in args
    assert "-s" in args and keychain.SERVICE in args
    # 値は argv に現れず、stdin の 2 行 (password + retype) で渡る
    assert "secret" not in args
    assert calls[0]["input"] == "secret\nsecret\n"


def test_write_readback_mismatch_raises(monkeypatch):
    monkeypatch.setattr(keychain, "read", lambda account, service=keychain.SERVICE: "OTHER")
    _capture_run(monkeypatch)
    with pytest.raises(KeychainError):
        keychain.write("glm", "secret")


def test_write_failure_returncode_raises(monkeypatch):
    _capture_run(monkeypatch, returncode=45, stderr="SecKeychainSearch")
    with pytest.raises(KeychainError):
        keychain.write("glm", "secret", service="quota-canvas-test")


@pytest.mark.parametrize("value", ["", "  x", "x ", "a\nb"])
def test_write_rejects_bad_values(monkeypatch, value):
    def boom(*a, **k):
        raise AssertionError("security を呼んではいけない")

    monkeypatch.setattr(keychain.subprocess, "run", boom)
    with pytest.raises(KeychainError):
        keychain.write("glm", value)


# ------------------------------------------------------------------- read

def test_read_found(monkeypatch):
    _capture_run(monkeypatch, stdout="stored-value\n")
    assert keychain.read("glm") == "stored-value"


def test_read_missing_returns_none(monkeypatch):
    _capture_run(monkeypatch, returncode=44, stderr="item could not be found")
    assert keychain.read("glm") is None


def test_read_empty_value_returns_none(monkeypatch):
    _capture_run(monkeypatch, stdout="\n")
    assert keychain.read("glm") is None


def test_read_other_error_raises(monkeypatch):
    _capture_run(monkeypatch, returncode=51, stderr="user interaction is not allowed")
    with pytest.raises(KeychainError):
        keychain.read("glm")


# ----------------------------------------------------------------- delete

def test_delete_found(monkeypatch):
    calls = _capture_run(monkeypatch)
    assert keychain.delete("glm") is True
    assert calls[0]["args"] == ["security", "delete-generic-password",
                                "-a", "glm", "-s", keychain.SERVICE]


def test_delete_missing_is_success(monkeypatch):
    _capture_run(monkeypatch, returncode=44)
    assert keychain.delete("glm") is False


def test_delete_error_raises(monkeypatch):
    _capture_run(monkeypatch, returncode=1)
    with pytest.raises(KeychainError):
        keychain.delete("glm")


def test_custom_service_is_passed(monkeypatch):
    monkeypatch.setattr(keychain, "read", lambda a, s: "v")
    calls = _capture_run(monkeypatch)
    keychain.write("glm", "v", service="quota-canvas-test")
    assert "quota-canvas-test" in calls[0]["args"]