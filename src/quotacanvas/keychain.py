"""login キーチェーンへの API キー保存 (service "quota-canvas", account <provider>)。

`security` コマンドだけを使う。値は argv に渡さない: 値なしの `-w` は
「password / retype」として stdin から 2 行読むので、そこへ値を 2 回流す。
(`security add-generic-password -a ACC -s SVC -U -w`)
"""

from __future__ import annotations

import subprocess
from typing import List, Optional

from .i18n import t

SERVICE = "quota-canvas"
TIMEOUT = 15.0

# security の終了コード: errSecItemNotFound
_NOT_FOUND = 44


class KeychainError(Exception):
    """キーチェーン操作の失敗。メッセージはそのまま表示でき、秘密を含まない。"""


def _run(args: List[str], input: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(
            ["security", *args],
            input=input, capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise KeychainError(t("cli.keychain.timeout")) from None
    except OSError:
        raise KeychainError(t("cli.keychain.cannot_run")) from None


def read(account: str, service: str = SERVICE) -> Optional[str]:
    """保存した値を返す。項目が無い (または空) ときは None。"""
    proc = _run(["find-generic-password", "-a", account, "-s", service, "-w"])
    if proc.returncode == _NOT_FOUND:
        return None
    if proc.returncode != 0:
        raise KeychainError(t("cli.keychain.read_failed", rc=proc.returncode))
    value = proc.stdout.rstrip("\r\n")  # security -w は値 + 改行で出力する
    return value or None


def write(account: str, value: str, service: str = SERVICE) -> None:
    """値を保存する (既にある項目は -U で更新)。"""

    if not value or value.strip() != value or "\n" in value or "\r" in value:
        raise KeychainError(t("cli.keychain.bad_value"))
    proc = _run(
        ["add-generic-password", "-a", account, "-s", service, "-U", "-w"],
        input=value + "\n" + value + "\n",  # password と retype の 2 行
    )
    if proc.returncode != 0:
        raise KeychainError(t("cli.keychain.write_failed", rc=proc.returncode))
    if read(account, service) != value:  # 読み戻せたかだけ確認する (値はログに出さない)
        raise KeychainError(t("cli.keychain.verify_failed"))


def delete(account: str, service: str = SERVICE) -> bool:
    """項目を削除する。もともと無くても成功扱い。戻り値は「削除したか」。"""
    proc = _run(["delete-generic-password", "-a", account, "-s", service])
    if proc.returncode == 0:
        return True
    if proc.returncode == _NOT_FOUND:
        return False
    raise KeychainError(t("cli.keychain.delete_failed", rc=proc.returncode))