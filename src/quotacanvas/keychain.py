"""login キーチェーンへの API キー保存 (service "quota-canvas", account <provider>)。

`security` コマンドだけを使う。値は argv に渡さない: `security -i` (コマンドを
標準入力から 1 行ずつ読む対話モード) に add-generic-password の 1 行を渡す
(値なしの `-w` は /dev/tty から読もうとして、tty があるとプロンプトで
TIMEOUT するので使わない)。`security -i` は中のコマンドが失敗しても
終了コード 0 を返すことがあるので、保存後の読み戻し確認は必須。
"""

from __future__ import annotations

import re
import subprocess
from typing import List, Optional

from .i18n import t
from .platform_support import security

SERVICE = "quota-canvas"
TIMEOUT = 15.0

# security の終了コード: errSecItemNotFound
_NOT_FOUND = 44

# security -i の字句解析 (実機で確認): ダブルクオート内は 1 トークンで、空白や
# #$;|<>()&'!% などの記号はそのまま渡る。ただし引用符内でも \ はエスケープ
# 記号として解釈され (\", \\, \x.. など)、非 ASCII は化ける。エスケープの
# 全容は確認しきれないので、" と \ を含む値と制御文字・非 ASCII は拒否する
# (確実な方に倒す)。対象は API キーなので実用上の支障は無い。
_QUOTABLE_RE = re.compile(r"[\x20-\x7e]*")


class KeychainError(Exception):
    """キーチェーン操作の失敗。メッセージはそのまま表示でき、秘密を含まない。"""


def _run(args: List[str], input: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    try:
        return security.run(args, input=input, timeout=TIMEOUT)
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


def _quoted(token: str) -> str:
    """security -i の 1 行に安全に載せられるトークン (ダブルクオートで囲む)。

    囲みだけでは不十分な文字 (引用符・バックスラッシュ・制御文字・非 ASCII) を
    含むときは KeychainError にする。エラーメッセージに値は載せない。
    """
    if (not _QUOTABLE_RE.fullmatch(token) or '"' in token or "\\" in token):
        raise KeychainError(t("cli.keychain.bad_value"))
    return '"' + token + '"'


def write(account: str, value: str, service: str = SERVICE) -> None:
    """値を保存する (既にある項目は -U で更新)。

    `security -i` に 1 行を渡す。値がコマンドの argv に載らないのは従来どおり。
    security -i は中のコマンドの失敗を終了コードに反映しないことがあるので、
    最後に読み戻して同じ値か確認する (値はログ・例外に出さない)。
    """
    if not value or value.strip() != value or "\n" in value or "\r" in value:
        raise KeychainError(t("cli.keychain.bad_value"))
    line = "add-generic-password -a %s -s %s -U -w %s" % (
        _quoted(account), _quoted(service), _quoted(value))
    proc = _run(["-i"], input=line + "\n")
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