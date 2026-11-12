"""provider モジュール共通の部品: HTTP、認証エラー、窓 ID、Provider/Detection 型。

サービスを追加する (例 grok.py) ときは、このモジュールの部品を使って
`PROVIDER = Provider(id, name, needs_key, detect, fetch)` を 1 つ定義すればよい。
"""

from __future__ import annotations

import http.client
import json
import math
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import keychain
from ..i18n import t
from ..keychain import KeychainError
from ..models import ProviderUsage

TIMEOUT = 15.0
USER_AGENT = "quota-canvas"
# 応答本文の読み取り上限と 1 回に読む塊の大きさ、読み取り開始からの総時間上限。
# ソケットのタイムアウト未満の間隔で少しずつ届き続けても、全体の時間を
# 無制限にしない (muse.py の READ_LIMIT / READ_CHUNK と同じ考え方)。
# リダイレクト途中の応答の読み捨てにも同じ上限を使う (時間はリダイレクト
# 全体で共有、サイズは応答ごと。 _BoundedDiscardFP)。
READ_LIMIT = 5 * 1024 * 1024
READ_CHUNK = 64 * 1024
READ_BUDGET = 30.0


class CredentialError(Exception):
    """認証情報が無い・読めない。メッセージは秘密情報を含まない。"""


# ------------------------------------------------------------------- types

@dataclass(frozen=True)
class Detection:
    """detect() の戻り値: 認証情報の有無と、あるときはその取得元の説明 (日本語)。"""

    detected: bool
    source: Optional[str] = None  # 取得元の説明 (例 "Claude Code のログイン（キーチェーン）")
    error: Optional[str] = None   # 未検出の理由 (例 "API キーが未設定")。秘密を含まない


@dataclass(frozen=True)
class Provider:
    """1 サービスぶんの処理。各サービスモジュールが PROVIDER という名前で定義する。

    display_name はメニューバーアプリ向け (detect --json) の長い名前。
    name はカードやエラー表示用の短い名前。
    """

    id: str                                     # "claude" | "codex" | "glm" | ...
    name: str                                   # 短い表示名 (例 "GLM")
    needs_key: bool                             # set-key でキーを登録するサービスか
    detect: Callable[[], Detection]             # 認証情報の有無 (通信しない)
    fetch: Callable[[datetime], ProviderUsage]  # 利用枠の取得
    display_name: str = ""                      # 例 "GLM (Z.ai)"。空なら name
    # 「今使う先」(recommend) の候補にするか。コーディングの作業に使えない
    # サービス (Muse など) は False にして、ヘッダの「〜を使う」に出さない。
    recommendable: bool = True

    @property
    def shown_name(self) -> str:
        return self.display_name or self.name


# ----------------------------------------------------------------- helpers

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    """ISO8601 string -> timezone-aware UTC datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _from_epoch(seconds: Any) -> Optional[datetime]:
    """Epoch seconds -> timezone-aware UTC datetime, or None."""
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _from_epoch_millis(millis: Any) -> Optional[datetime]:
    """Epoch milliseconds -> timezone-aware UTC datetime, or None."""
    try:
        return _from_epoch(float(millis) / 1000.0)
    except (TypeError, ValueError):
        return None


def _window_identity(seconds: int) -> Tuple[str, str, timedelta]:
    """(key, label, length) for a window length in seconds. label は表示言語由来。"""
    if seconds == 7 * 86400:
        return "weekly", t("providers.window.weekly"), timedelta(days=7)
    if seconds == 5 * 3600:
        return "5h", t("providers.window.five_hours"), timedelta(hours=5)
    if seconds % 86400 == 0:
        days = seconds // 86400
        return "%dd" % days, t("providers.window.days", n=days), timedelta(days=days)
    hours = seconds / 3600.0
    if hours.is_integer():
        n = int(hours)
        return "%dh" % n, t("providers.window.hours", n=n), timedelta(hours=n)
    hours = round(hours, 2)
    key = ("%gh" % hours)
    return key, t("providers.window.hours", n="%g" % hours), timedelta(seconds=seconds)


# ------------------------------------------------------------------- HTTP

def _origin(url: str) -> Tuple[str, Optional[str], Optional[int]]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, parts.hostname, port


# リダイレクト途中の読み捨ての総時間の元締め (READ_BUDGET 秒)。1 つの要求の
# リダイレクト全体で共有し、残り時間を要求オブジェクトのこの属性で引き継ぐ。
_REDIRECT_DEADLINE_ATTR = "_redirect_discard_deadline"


class _BoundedDiscardFP:
    """リダイレクト元 (途中の応答) の本文を読み捨てるときの fp の proxy。

    HTTPRedirectHandler.http_error_302 は次の要求を送る前に、元の応答の本文を
    fp.read() (上限なし) で読み捨てる。途中の応答にも最終応答と同じ
    READ_LIMIT / 残り時間の上限を適用するため、本物の fp をこれに差し替える。"""

    def __init__(self, fp: Any, timeout: float, deadline: float) -> None:
        self._fp = fp
        self._timeout = timeout
        self._deadline = deadline

    def read(self, amt: Optional[int] = None) -> Any:
        if amt:  # amt 指定の読み出しはそのまま渡す (fp.read(0) など)
            return self._fp.read(amt)
        return _read_bounded(self._fp, self._timeout,
                             max(self._deadline - time.monotonic(), 0.0))

    def close(self) -> None:
        self._fp.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fp, name)


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """認証ヘッダーを別 origin へ引き継ぐリダイレクトを拒否する。

    あわせて、リダイレクト元 (途中の応答) の本文の読み捨てにも最終応答と
    同じ上限 (READ_LIMIT / READ_BUDGET) を適用する。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        # 読み捨ての残り時間はリダイレクト全体で共有するため、次の要求に引き継ぐ
        deadline = getattr(req, _REDIRECT_DEADLINE_ATTR, None)
        if new is not None and deadline is not None:
            setattr(new, _REDIRECT_DEADLINE_ATTR, deadline)
        return new

    def _discard_fp(self, req, fp):
        """途中の応答の fp を、上限付きの読み捨てのできるものに差し替える。"""
        timeout = req.timeout if isinstance(req.timeout, (int, float)) else TIMEOUT
        deadline = getattr(req, _REDIRECT_DEADLINE_ATTR, None)
        if deadline is None:
            deadline = time.monotonic() + READ_BUDGET
            setattr(req, _REDIRECT_DEADLINE_ATTR, deadline)
        return _BoundedDiscardFP(fp, timeout, deadline)

    # 301 / 303 は 302 の別名、307 / 308 も同じ形で本文を読み捨てるので、
    # どの経路でも途中の応答の fp を上限付きに差し替えてから任せる。
    def http_error_301(self, req, fp, code, msg, headers):
        return super().http_error_301(req, self._discard_fp(req, fp), code, msg, headers)

    def http_error_302(self, req, fp, code, msg, headers):
        return super().http_error_302(req, self._discard_fp(req, fp), code, msg, headers)

    def http_error_303(self, req, fp, code, msg, headers):
        return super().http_error_303(req, self._discard_fp(req, fp), code, msg, headers)

    def http_error_307(self, req, fp, code, msg, headers):
        return super().http_error_307(req, self._discard_fp(req, fp), code, msg, headers)

    def http_error_308(self, req, fp, code, msg, headers):
        return super().http_error_308(req, self._discard_fp(req, fp), code, msg, headers)


_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


def _open(request: urllib.request.Request, timeout: float) -> Any:
    return _OPENER.open(request, timeout=timeout)


def _set_sock_timeout(response: Any, seconds: float) -> None:
    """可能なら下の socket の受信タイムアウトを seconds にする。

    http.client の応答は fp.raw._sock に生 socket を持つが、取れない実装
    (テストの fake など) でも落ちないよう全部握りつぶす。"""
    try:
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None),
                       "_sock", None)
        if sock is not None:
            sock.settimeout(max(seconds, 0.1))
    except (AttributeError, OSError):
        pass


class _DeadlineSocket:
    """受信のたびに socket のタイムアウトを期限までの残り時間に縮める proxy。

    HTTPResponse.read1() は chunked のサイズ行や trailer の処理で内部の recv を
    複数回呼ぶ。外側のループでタイムアウトを設定するだけでは、1 回の read1()
    の途中で総時間上限を超えうるため、生 socket を差し替えて各 recv の前に
    残り時間を反映する。"""

    def __init__(self, sock: Any, deadline: float, io_timeout: float) -> None:
        self._sock = sock
        self._deadline = deadline
        self._io_timeout = io_timeout

    def _arm(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("response read timed out")
        self._sock.settimeout(max(min(self._io_timeout, remaining), 0.1))

    def recv_into(self, *args: Any, **kwargs: Any) -> Any:
        self._arm()
        return self._sock.recv_into(*args, **kwargs)

    def recv(self, *args: Any, **kwargs: Any) -> Any:
        self._arm()
        return self._sock.recv(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


def _read_bounded(response: Any, timeout: float = TIMEOUT,
                  budget: float = READ_BUDGET) -> bytes:
    """本文を READ_CHUNK ずつ読む。READ_LIMIT を超えたら ValueError、
    読み取り開始から budget 秒を超えたら TimeoutError で打ち切る。

    response.read() は内部で受信を繰り返すので、1 recv で戻る read1()
    (無ければ read) を使い、各読み込みの前に残り時間を確認する。
    Content-Length の宣言より先に EOF になったら IncompleteRead にする
    (read1() は本文不足でも空 bytes を返すため、JSON として成功させない)。"""
    deadline = time.monotonic() + budget
    raw = getattr(getattr(response, "fp", None), "raw", None)
    original_sock = getattr(raw, "_sock", None)
    wrapped = False
    if original_sock is not None:
        try:
            raw._sock = _DeadlineSocket(original_sock, deadline, timeout)
            wrapped = True
        except (AttributeError, OSError):
            pass
    chunks: List[bytes] = []
    total = 0
    read = getattr(response, "read1", None) or response.read
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response read timed out")
            if not wrapped:  # proxy が張れない fake などは従来どおり外側で設定
                _set_sock_timeout(response, min(timeout, remaining))
            chunk = read(READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > READ_LIMIT:
                raise ValueError("response too large")
            chunks.append(chunk)
        # chunked の応答は length が None。Content-Length があるのに宣言長へ
        # 届かず EOF になったら、途中までの JSON を成功にしない。
        expected = getattr(response, "length", None)
        if isinstance(expected, int) and expected > 0:
            raise http.client.IncompleteRead(b"", expected)
        return b"".join(chunks)
    finally:
        # 読み終わりで接続が閉じられると raw._sock は None になっている
        if wrapped and getattr(raw, "_sock", None) is not None:
            try:
                raw._sock = original_sock
            except (AttributeError, OSError):
                pass


def _json_body(response: Any, timeout: float) -> Any:
    return json.loads(_read_bounded(response, timeout).decode("utf-8"))


def http_get_json(url: str, headers: Dict[str, str], timeout: float = TIMEOUT) -> Any:
    """GET url and return the decoded JSON body."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with _open(request, timeout) as response:
        return _json_body(response, timeout)


def http_post_json(url: str, headers: Dict[str, str], body: Dict[str, Any],
                   timeout: float = TIMEOUT) -> Any:
    """POST url with a JSON body and return the decoded JSON response."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
    with _open(request, timeout) as response:
        return _json_body(response, timeout)


# ------------------------------------------------------------ error summary

def _short_reason(exc: BaseException) -> str:
    """Short, secret-free description of a fetch failure."""
    if isinstance(exc, (CredentialError, KeychainError)):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %s" % exc.code
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return "timeout"
        return "network error"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "invalid JSON"
    return type(exc).__name__  # never embed exception text (may contain secrets)


def locate_api_key(provider: str, variable: str) -> Tuple[str, str]:
    """Read a provider key without making a network request."""
    stored = keychain.read(provider)
    if stored:
        return stored, t("providers.detect.keychain", provider=provider)
    value = os.environ.get(variable)
    if value and value.strip():
        return value.strip(), t("providers.detect.env", variable=variable)
    raise CredentialError(t("providers.api_key_not_set"))


def detect_api_key(provider: str, name: str, variable: str) -> Detection:
    """Describe whether the provider has a locally available API key."""
    try:
        _, source = locate_api_key(provider, variable)
    except CredentialError as exc:
        return Detection(False, None, str(exc))
    return Detection(True, t("providers.detect.api_key", name=name, source=source))


def finite_number(value: Any) -> float:
    """Parse an API monetary value and reject non-finite or boolean values."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid numeric value")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("invalid numeric value")
    return result
