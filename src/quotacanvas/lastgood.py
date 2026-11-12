"""最後に取得できた ProviderUsage の永続化 (last-good.json)。

エンジン再起動後も STALE_GRACE 内のデータを表示できるようにするため、
{provider: ProviderUsage} を JSON で保存する。既定の保存先は history.jsonl
と同じディレクトリ。読み込み・書き込みの失敗は log.warning で握り、
常駐エンジンを止めない。
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .atomic import atomic_write
from .platform_support.paths import data_dir
from .models import Balance, ProviderUsage, ResetCredit, Window

log = logging.getLogger("quotacanvas")

# history.py の DEFAULT_PATH と同じディレクトリ (history.jsonl の隣)。
DEFAULT_PATH = data_dir() / "last-good.json"

# 窓の長さとして受け入れる上限 (約 10 年)。これを超える値は壊れたファイル
# とみなして、その窓だけ捨てる (datetime の加減算が OverflowError になるため)。
MAX_WINDOW_LENGTH = timedelta(days=3660)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_iso(value: Optional[datetime]) -> Optional[str]:
    return _as_utc(value).isoformat() if value is not None else None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        return None
    return _as_utc(parsed)


def _encode_usage(usage: ProviderUsage) -> Dict[str, Any]:
    return {
        "name": usage.name,
        "error": usage.error,
        "fetched_at": _to_iso(usage.fetched_at),
        "retry_after": usage.retry_after,
        "windows": [
            {
                "key": w.key,
                "label": w.label,
                "used_percent": w.used_percent,
                "resets_at": _to_iso(w.resets_at),
                "length": w.length.total_seconds(),
                "part_of": w.part_of,
            }
            for w in usage.windows
        ],
        "balances": [{"currency": b.currency, "amount": b.amount, "label": b.label,
                      "text": b.text}
                     for b in usage.balances],
        "reset_credits": [
            {
                "title": c.title,
                "granted_at": _to_iso(c.granted_at),
                "expires_at": _to_iso(c.expires_at),
            }
            for c in usage.reset_credits
        ],
    }


def _safe_repr(value: Any, limit: int = 40) -> str:
    """ログ用。10**400 のような巨大整数でも安全に短く表す。"""
    try:
        text = repr(value)
    except (ValueError, OverflowError):
        return "<unprintable>"
    return text if len(text) <= limit else text[:limit] + "..."


def _decode_window(provider: str, entry: Any) -> Optional[Window]:
    if not isinstance(entry, dict):
        return None  # 壊れた要素 (文字列など) はその窓だけ飛ばす
    try:
        key = str(entry["key"])
        label = str(entry["label"])
        raw_used = entry["used_percent"]
        raw_length = entry["length"]
    except (KeyError, TypeError):
        return None
    try:
        used = float(raw_used)
        seconds = float(raw_length)
    except (TypeError, ValueError, OverflowError):
        # JSON の整数は Python では任意精度。10**400 のような値は float() が
        # OverflowError を出し、ここで捕まえないと provider 全体が消える。
        log.warning("last-good.json の窓 %s:%s の数値を解釈できないため無視します "
                    "(used_percent=%s, length=%s)", provider, key,
                    _safe_repr(raw_used), _safe_repr(raw_length))
        return None
    resets_at = _parse_iso(entry.get("resets_at"))
    part_of = entry.get("part_of")      # キーが無い古いファイルは None
    # NaN/inf をそのまま Window に入れると、表示や再保存で非標準 JSON になる。
    # length は timedelta の範囲外だと OverflowError でここより下が落ちるため、
    # 非有限・範囲外はその窓だけ警告付きで除外する (起動を止めない)。
    if not math.isfinite(used) or not math.isfinite(seconds):
        log.warning("last-good.json の窓 %s:%s の数値が非有限のため無視します "
                    "(used_percent=%s, length=%s)", provider, key,
                    _safe_repr(raw_used), _safe_repr(raw_length))
        return None
    if not (0 < seconds <= MAX_WINDOW_LENGTH.total_seconds()):
        log.warning("last-good.json の窓 %s:%s の長さが範囲外のため無視します: %s",
                    provider, key, _safe_repr(raw_length))
        return None
    try:
        length = timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        log.warning("last-good.json の窓 %s:%s の長さを解釈できないため無視します: %s",
                    provider, key, _safe_repr(raw_length))
        return None
    return Window(
        provider,
        key,
        label,
        used,
        resets_at,
        length,
        None if part_of is None else str(part_of),
    )


def _decode_credit(entry: Any) -> Optional[ResetCredit]:
    try:
        return ResetCredit(
            str(entry.get("title") or ""),
            _parse_iso(entry.get("granted_at")),
            _parse_iso(entry.get("expires_at")),
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _decode_balance(entry: Any) -> Optional[Balance]:
    """Skip malformed balance entries without discarding other last-good data."""
    if not isinstance(entry, dict):
        return None
    try:
        currency, amount, label = entry["currency"], entry["amount"], entry.get("label")
        text = entry.get("text")            # キーが無い古いファイルは None
        if not isinstance(currency, str) or not currency or isinstance(amount, bool):
            return None
        amount = float(amount)
        if not math.isfinite(amount) or (label is not None and not isinstance(label, str)) \
                or (text is not None and not isinstance(text, str)):
            return None
        return Balance(currency, amount, label, text)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _decode_usage(provider: str, entry: Any) -> Optional[ProviderUsage]:
    if not isinstance(entry, dict):
        return None
    try:
        retry_after = entry.get("retry_after")
        if retry_after is not None:
            try:
                retry_after = float(retry_after)
            except (TypeError, ValueError, OverflowError):
                log.warning("last-good.json の %s の retry_after を解釈できないため"
                            "無視します: %s", provider, _safe_repr(entry.get("retry_after")))
                retry_after = None
        if retry_after is not None and not math.isfinite(retry_after):
            # 429 の待ち時間が NaN/inf だとバックオフ計算がおかしくなる
            log.warning("last-good.json の %s の retry_after が非有限のため無視します: %s",
                        provider, _safe_repr(entry.get("retry_after")))
            retry_after = None
        return ProviderUsage(
            provider=provider,
            name=str(entry.get("name") or provider),
            windows=[w for w in (_decode_window(provider, e) for e in entry.get("windows") or [])
                     if w is not None],
            error=entry.get("error"),
            fetched_at=_parse_iso(entry.get("fetched_at")),
            reset_credits=[c for c in (_decode_credit(e) for e in entry.get("reset_credits") or [])
                           if c is not None],
            retry_after=retry_after,
            balances=[b for b in (_decode_balance(e) for e in entry.get("balances") or [])
                      if b is not None],
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        # 窓・残高・クレジット単位の防御で拾えなかった壊れ方の最後の砦。
        # 黙って provider を消さず、警告してから除外する。
        log.warning("last-good.json の %s を解釈できないため無視します: %s",
                    provider, exc)
        return None


class LastGood:
    """{provider: ProviderUsage} を 1 つの JSON ファイルに保存する。

    書き込みは一時ファイル -> os.replace で原子的に。読み込みは、ファイルが
    無い・壊れているときも空 dict を返して例外を出さない。
    """

    def __init__(self, path: Optional[os.PathLike] = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self.data: Dict[str, ProviderUsage] = {}
        self.load()

    # ------------------------------------------------------------ public

    def load(self) -> Dict[str, ProviderUsage]:
        """ディスクから読み込む (ファイル無し/壊れ => 空 dict)。"""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as exc:
            log.warning("last-good.json が壊れているため無視します: %s", exc)
            return {}
        except OSError as exc:
            log.warning("last-good.json を読めません: %s", exc)
            return {}
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            # JSONDecodeError のほか、桁数が多すぎる整数で json が ValueError を
            # 出すことがある (Python の int 変換上限)。起動を止めずに諦める。
            log.warning("last-good.json が壊れているため無視します: %s", exc)
            return {}
        if not isinstance(raw, dict):
            log.warning("last-good.json の形式が違うため無視します")
            return {}
        data: Dict[str, ProviderUsage] = {}
        for pid, entry in raw.items():
            usage = _decode_usage(str(pid), entry)
            if usage is not None:
                data[str(pid)] = usage
        self.data = data
        return data

    def set(self, usage: ProviderUsage) -> None:
        """1 サービスぶん更新してすぐディスクに書く (取得成功のたびに呼ぶ)。"""
        self.data[usage.provider] = usage
        self.save()

    def save(self) -> None:
        """現在の data を全体を書き込む。失敗しても例外を出さない。"""
        payload = {pid: _encode_usage(u) for pid, u in self.data.items()}
        try:
            data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
            atomic_write(self.path, lambda handle: handle.write(data))
        except (OSError, ValueError) as exc:
            log.warning("last-good.json に保存できません: %s", exc)
