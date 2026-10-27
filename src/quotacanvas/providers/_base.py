"""provider モジュール共通の部品: HTTP、認証エラー、窓 ID、Provider/Detection 型。

サービスを追加する (例 grok.py) ときは、このモジュールの部品を使って
`PROVIDER = Provider(id, name, needs_key, detect, fetch)` を 1 つ定義すればよい。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..i18n import t
from ..keychain import KeychainError
from ..models import ProviderUsage

TIMEOUT = 15.0
USER_AGENT = "quota-canvas"


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


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """認証ヘッダーを別 origin へ引き継ぐリダイレクトを拒否する。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


def _open(request: urllib.request.Request, timeout: float) -> Any:
    return _OPENER.open(request, timeout=timeout)


def http_get_json(url: str, headers: Dict[str, str], timeout: float = TIMEOUT) -> Any:
    """GET url and return the decoded JSON body."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with _open(request, timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
