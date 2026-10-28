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

from .models import Balance, ProviderUsage, ResetCredit, Window

log = logging.getLogger("quotacanvas")

# history.py の DEFAULT_PATH と同じディレクトリ (history.jsonl の隣)。
DEFAULT_PATH = (Path.home() / "Library" / "Application Support" / "quota-canvas"
                / "last-good.json")


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
    except ValueError:
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
        "balances": [{"currency": b.currency, "amount": b.amount, "label": b.label}
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


def _decode_window(provider: str, entry: Any) -> Optional[Window]:
    if not isinstance(entry, dict):
        return None  # 壊れた要素 (文字列など) はその窓だけ飛ばす
    try:
        part_of = entry.get("part_of")      # キーが無い古いファイルは None
        return Window(
            provider,
            str(entry["key"]),
            str(entry["label"]),
            float(entry["used_percent"]),
            _parse_iso(entry.get("resets_at")),
            timedelta(seconds=float(entry["length"])),
            None if part_of is None else str(part_of),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _decode_credit(entry: Any) -> Optional[ResetCredit]:
    try:
        return ResetCredit(
            str(entry.get("title") or ""),
            _parse_iso(entry.get("granted_at")),
            _parse_iso(entry.get("expires_at")),
        )
    except (AttributeError, TypeError):
        return None


def _decode_balance(entry: Any) -> Optional[Balance]:
    """Skip malformed balance entries without discarding other last-good data."""
    if not isinstance(entry, dict):
        return None
    try:
        currency, amount, label = entry["currency"], entry["amount"], entry.get("label")
        if not isinstance(currency, str) or not currency or isinstance(amount, bool):
            return None
        amount = float(amount)
        if not math.isfinite(amount) or (label is not None and not isinstance(label, str)):
            return None
        return Balance(currency, amount, label)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _decode_usage(provider: str, entry: Any) -> Optional[ProviderUsage]:
    if not isinstance(entry, dict):
        return None
    try:
        retry_after = entry.get("retry_after")
        return ProviderUsage(
            provider=provider,
            name=str(entry.get("name") or provider),
            windows=[w for w in (_decode_window(provider, e) for e in entry.get("windows") or [])
                     if w is not None],
            error=entry.get("error"),
            fetched_at=_parse_iso(entry.get("fetched_at")),
            reset_credits=[c for c in (_decode_credit(e) for e in entry.get("reset_credits") or [])
                           if c is not None],
            retry_after=float(retry_after) if retry_after is not None else None,
            balances=[b for b in (_decode_balance(e) for e in entry.get("balances") or [])
                      if b is not None],
        )
    except (KeyError, TypeError, ValueError):
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
        except OSError as exc:
            log.warning("last-good.json を読めません: %s", exc)
            return {}
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("last-good.json に保存できません: %s", exc)
