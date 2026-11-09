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
    _window_identity, http_get_json, http_post_json,
)
from . import claude, codex, gemini, glm, grok, openrouter, deepseek, kimi, opencode, devin, muse, local  # noqa: F401

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
# local (providers.local.<名前> のローカル LLM サーバー) は名前ごとに台数分
# 定義できるので REGISTRY に入れず、providers.local 経由で別途取得する。
_MODULES = (claude, codex, glm, grok, gemini, openrouter, deepseek, kimi, opencode, devin, muse)
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


def _detect_entry(provider: Provider, detection: Detection,
                  usage: Optional[ProviderUsage]) -> Dict[str, Any]:
    """detect --json の 1 サービスぶん。usage は provider.fetch / 常駐ループの
    周回の利用枠 (None = 未取得: 検出されたら plan 不明・窓無しのまま返す)。
    内訳窓 (part_of 付き) と重複 key は windows から除く。"""
    entry: Dict[str, Any] = {
        "provider": provider.id,
        "name": provider.shown_name,
        "detected": detection.detected,
        "source": detection.source,
        "needs_key": provider.needs_key,
        "plan": None,
        "windows": [],
        "error": None,
    }
    if not detection.detected:
        entry["error"] = detection.error or t("providers.no_credentials")
        return entry
    if usage is None:
        return entry
    if usage.error:
        entry["error"] = usage.error
        return entry
    entry["plan"] = _plan_name(provider, usage.name)
    seen = set()
    for window in usage.windows:
        if window.part_of is not None:
            continue  # 内訳窓: 窓ピッカー・窓チェックには出さない
        if window.key in seen:
            continue
        seen.add(window.key)
        entry["windows"].append({"key": window.key, "label": window.label})
    if usage.stale_error:
        # 取得失敗中の前回成功値 (Poller の last-good): plan/windows は残し、
        # 失敗理由だけ error に入れる (正常値としてキャッシュしない)
        entry["error"] = usage.stale_error
    return entry


def _detection(provider: Provider) -> Detection:
    """provider.detect() を呼び、例外も未検出の Detection に変換する。"""
    try:
        return provider.detect()
    except Exception as exc:  # noqa: BLE001
        return Detection(False, None, _short_reason(exc))


def detect_all(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """detect --json の中身: 各サービスの認証情報の有無と、使えるならプラン/窓。

    使えるサービスは利用枠を 1 回だけ取得して plan と windows を調べる
    (AI トークンは消費しない)。決して例外を漏らさない。
    """
    moment = now if now is not None else _utc_now()
    out: List[Dict[str, Any]] = []
    for pid, provider in REGISTRY.items():
        detection = _detection(provider)
        usage: Optional[ProviderUsage] = None
        if detection.detected:
            try:
                usage = provider.fetch(moment)
            except Exception as exc:  # noqa: BLE001
                usage = ProviderUsage(pid, provider.name, [],
                                      _short_reason(exc), moment)
        out.append(_detect_entry(provider, detection, usage))
    return out


# 常駐周回で取得していない provider の仮の取得時刻。どの実取得より古い扱いにして、
# 手動 detect などの実取得エントリが常駐側の未更新エントリに負けないようにする
# (detect_cache が provider 単位で「新しい方」を採る際の比較値)。
_NEVER_FETCHED = "1970-01-01T00:00:00+00:00"


def detect_merge(usages: List[ProviderUsage],
                 previous: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """常駐ループの周回の usages から detect --json と同じ形の配列を作る。

    サービスへの新たな取得はしない (provider.detect() による資格情報の有無の
    ローカル確認だけ)。usages に結果がある provider は plan / windows / error
    をその usage から作る (fetched_from は書き込み側が周回の開始時刻で刻む)。
    usages に無い provider (無効化中など) は前回キャッシュ previous の
    plan / windows / error / fetched_from を残す (無ければ detect() の結果だけ、
    時刻は _NEVER_FETCHED)。
    """
    by_pid = {u.provider: u for u in usages}
    cached = {e.get("provider"): e for e in previous or [] if isinstance(e, dict)}
    out: List[Dict[str, Any]] = []
    for pid, provider in REGISTRY.items():
        detection = _detection(provider)
        usage = by_pid.get(pid)
        entry = _detect_entry(provider, detection, usage)
        if usage is None and detection.detected:
            old = cached.get(pid)
            if old is not None:
                entry["plan"] = old.get("plan")
                entry["windows"] = list(old.get("windows") or [])
                entry["error"] = old.get("error")
            # この周回は取得していない: 時刻は前回値か「未取得」(最古) として、
            # 手動 detect などの新しい実取得エントリに負けるようにする
            entry["fetched_from"] = (old or {}).get("fetched_from") or _NEVER_FETCHED
        out.append(entry)
    return out


def _plan_name(provider: Provider, usage_name: str) -> Optional[str]:
    """usage 名 (例 "Claude Max") からプラン名 (例 "Max") を取り出す。"""
    if usage_name == provider.name:
        return None
    if usage_name.startswith(provider.name + " "):
        return usage_name[len(provider.name) + 1:].strip() or None
    return usage_name
