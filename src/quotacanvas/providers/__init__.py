"""Fetch current rate-limit usage for Claude, Codex and GLM (Z.ai).

Standard library only. Each provider is fetched independently; failures are
reported as ProviderUsage.error and never leak exceptions (or secrets) outward.

サービスは 1 ファイル 1 サービス (claude.py / codex.py / glm.py)。各モジュールは
`PROVIDER = Provider(id, name, needs_key, detect, fetch)` を定義する。
サービスを増やす (例 grok.py) ときは、モジュールを足して下の _MODULES に 1 行加えるだけ。
"""

from __future__ import annotations

import math
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from ..i18n import t
from ..models import ProviderUsage, Window  # noqa: F401  (re-export)
from ._base import (  # noqa: F401  (re-export)
    TIMEOUT, USER_AGENT,
    CredentialError, Detection, Provider,
    _from_epoch, _from_epoch_millis, _parse_iso, _short_reason, _utc_now,
    _window_identity, http_get_json,
)
from . import claude, codex, gemini, glm, grok, openrouter, deepseek, kimi  # noqa: F401

CLAUDE_USAGE_URL = claude.CLAUDE_USAGE_URL
CODEX_USAGE_URL = codex.CODEX_USAGE_URL
CODEX_RESET_CREDITS_URL = codex.CODEX_RESET_CREDITS_URL  # 閲覧 (GET) 専用
GLM_USAGE_URL = glm.GLM_USAGE_URL

read_claude_credentials = claude.read_claude_credentials
read_codex_credentials = codex.read_codex_credentials
read_glm_api_key = glm.read_glm_api_key

parse_claude = claude.parse_claude
parse_codex = codex.parse_codex
parse_glm = glm.parse_glm
parse_reset_credits = codex.parse_reset_credits

# サービスを追加するときは、モジュールを 1 ファイル足してここに並べる。
_MODULES = (claude, codex, glm, grok, gemini, openrouter, deepseek, kimi)
REGISTRY: Dict[str, Provider] = {m.PROVIDER.id: m.PROVIDER for m in _MODULES}


def _retry_after_seconds(exc: urllib.error.HTTPError, now: datetime) -> Optional[float]:
    """HTTP 429 の Retry-After ヘッダを秒にして返す (解釈できなければ None)。

    秒数の整数のほか HTTP-date 形式も解釈する。壊れた値や過去の時刻は None/0。
    """
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - now).total_seconds())


def fetch_all(now: Optional[datetime] = None,
              enabled: Optional[Set[str]] = None) -> List[ProviderUsage]:
    """Fetch every provider; each failure becomes ProviderUsage.error.

    `enabled` が集合なら、その中に含まれる provider だけを取得する。
    HTTP 429 のときは error "HTTP 429" に加えて Retry-After (秒) を
    ProviderUsage.retry_after に入れる (ヘッダが無ければ None)。
    """
    moment = now if now is not None else _utc_now()
    results: List[ProviderUsage] = []
    for pid, provider in REGISTRY.items():
        if enabled is not None and pid not in enabled:
            continue
        try:
            usage = provider.fetch(moment)
        except urllib.error.HTTPError as exc:
            usage = ProviderUsage(pid, provider.name, [], _short_reason(exc), moment)
            if exc.code == 429:
                usage.retry_after = _retry_after_seconds(exc, moment)
        except Exception as exc:  # noqa: BLE001 - 常に usage に変換する
            usage = ProviderUsage(pid, provider.name, [], _short_reason(exc), moment)
        if usage.fetched_at is None:
            usage.fetched_at = moment
        results.append(usage)
    return results


def detect_all(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """detect --json の中身: 各サービスの認証情報の有無と、使えるならプラン/窓。

    使えるサービスは利用枠を 1 回だけ取得して plan と windows を調べる
    (AI トークンは消費しない)。決して例外を漏らさない。
    """
    moment = now if now is not None else _utc_now()
    out: List[Dict[str, Any]] = []
    for pid, provider in REGISTRY.items():
        entry: Dict[str, Any] = {
            "provider": pid,
            "name": provider.shown_name,
            "detected": False,
            "source": None,
            "needs_key": provider.needs_key,
            "plan": None,
            "windows": [],
            "error": None,
        }
        try:
            detection = provider.detect()
        except Exception as exc:  # noqa: BLE001
            entry["error"] = _short_reason(exc)
            out.append(entry)
            continue
        entry["detected"] = detection.detected
        entry["source"] = detection.source
        if not detection.detected:
            entry["error"] = detection.error or t("providers.no_credentials")
            out.append(entry)
            continue
        try:
            usage = provider.fetch(moment)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = _short_reason(exc)
            out.append(entry)
            continue
        if usage.error:
            entry["error"] = usage.error
            out.append(entry)
            continue
        entry["plan"] = _plan_name(provider, usage.name)
        seen = set()
        for window in usage.windows:
            if window.part_of is not None:
                continue  # 内訳窓: 窓ピッカー・窓チェックには出さない
            if window.key in seen:
                continue
            seen.add(window.key)
            entry["windows"].append({"key": window.key, "label": window.label})
        out.append(entry)
    return out


def _plan_name(provider: Provider, usage_name: str) -> Optional[str]:
    """usage 名 (例 "Claude Max") からプラン名 (例 "Max") を取り出す。"""
    if usage_name == provider.name:
        return None
    if usage_name.startswith(provider.name + " "):
        return usage_name[len(provider.name) + 1:].strip() or None
    return usage_name
