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

def test_write_uses_security_i_single_stdin_line(monkeypatch):
    """書き込みは `security -i` に 1 行を渡す (値は argv にも素の stdin にも
    載せない。tty があると値なし -w が /dev/tty のプロンプトで TIMEOUT する
    ので、対話モードのコマンド行で渡す)。"""
    monkeypatch.setattr(keychain, "read", lambda account, service=keychain.SERVICE: "secret")
    calls = _capture_run(monkeypatch)
    keychain.write("glm", "secret")
    assert len(calls) == 1
    args = calls[0]["args"]
    assert args == ["security", "-i"]           # 値も指標も argv に無い
    assert calls[0]["input"] == \
        'add-generic-password -a "glm" -s "quota-canvas" -U -w "secret"\n'
    assert calls[0]["timeout"] == keychain.TIMEOUT


def test_write_quotes_value_with_spaces_and_symbols(monkeypatch):
    monkeypatch.setattr(keychain, "read",
                        lambda account, service=keychain.SERVICE: "p@ss w0rd!#%+;")
    calls = _capture_run(monkeypatch)
    keychain.write("glm", "p@ss w0rd!#%+;")
    # security -i の字句解析に合わせて 1 トークンとしてダブルクオートで囲む
    assert calls[0]["input"].endswith('-w "p@ss w0rd!#%+;"\n')


def test_write_readback_mismatch_raises(monkeypatch):
    """security -i は中のコマンドの失敗を終了コードに反映しないことがある
    ので、読み戻し確認は必須 (値が違えば verify_failed)。"""
    monkeypatch.setattr(keychain, "read", lambda account, service=keychain.SERVICE: "OTHER")
    _capture_run(monkeypatch)
    with pytest.raises(KeychainError):
        keychain.write("glm", "secret")


def test_write_failure_returncode_raises(monkeypatch):
    _capture_run(monkeypatch, returncode=45, stderr="SecKeychainSearch")
    with pytest.raises(KeychainError):
        keychain.write("glm", "secret", service="quota-canvas-test")


@pytest.mark.parametrize("value", [
    "", "  x", "x ", "a\nb",      # 従来の検証 (空・前後空白・改行)
    'quote"in',                   # 引用符: エスケープで渡せるが確実でないので拒否
    "back\\slash",                # バックスラッシュ: \x.. などに解釈されるので拒否
    "caf\xc3\xa9",                # 非 ASCII: security -i で化ける (実機確認済み)
    "tab\tinside",                # 制御文字
])
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
    assert '"quota-canvas-test"' in calls[0]["input"]