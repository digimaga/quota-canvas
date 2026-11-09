"""detect-cache.json: `detect --json` の結果キャッシュ (docs/app-contract.md)。

status.json と同じディレクトリに {"cached_at": ISO8601, "services": [...]} の形で
原子的に書く。services は providers.detect_all と同じ形の配列。detect コマンド
(毎回) と常駐ループの全体取得周回が書き、`detect --json --cached` と
メニューバーアプリ (サービス/配置タブ) が読む。

"fetched_from" はその結果の元になった取得の開始時刻。書き込み時に既存
キャッシュの fetched_from より古い取得からの書き込みは捨てる (遅れて終わった
常駐取得が、資格情報変更後の再検出結果を巻き戻さないため)。読み→比較→
原子的置換は <name>.lock ファイルの flock で排他する。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .atomic import atomic_write
from .platform_support.paths import data_dir
from .providers._base import _parse_iso, _utc_now

try:
    import fcntl
except ImportError:  # POSIX 以外 (Windows) ではロック無しで書く
    fcntl = None

log = logging.getLogger("quotacanvas")

DETECT_CACHE_PATH = data_dir() / "detect-cache.json"

# サービス 1 エントリの必須項目と型 (壊れたキャッシュを有効扱いしないため)。
# 任意項目も、アプリの DetectService / DetectWindow (String?) と合わせて
# 存在するときは str か null に限定する。
_SERVICE_TYPES = {"provider": str, "detected": bool, "needs_key": bool,
                  "windows": list}
_SERVICE_OPTIONAL_STR = ("name", "source", "plan", "error")


def _iso(dt: datetime) -> str:
    """ISO 8601 (オフセット付きローカル時刻)。status.py と同じ形式。"""
    return dt.astimezone().isoformat(timespec="seconds")


def _iso_us(dt: datetime) -> str:
    """ISO 8601 マイクロ秒精度 (fetched_from の比較用: 同じ秒の取得でも順序を保つ)。"""
    return dt.astimezone().isoformat(timespec="microseconds")


def _valid_service(entry: Any) -> bool:
    """detect エントリ 1 件の必須項目と型を検査する。"""
    if not isinstance(entry, dict):
        return False
    for key, typ in _SERVICE_TYPES.items():
        if not isinstance(entry.get(key), typ):
            return False
    for key in _SERVICE_OPTIONAL_STR:
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            return False
    if (entry.get("fetched_from") is not None
            and _parse_iso(entry["fetched_from"]) is None):
        return False  # provider 単位の取得時刻も、あれば解釈できること
    for w in entry["windows"]:
        if not isinstance(w, dict) or not isinstance(w.get("key"), str):
            return False
        if w.get("label") is not None and not isinstance(w["label"], str):
            return False
    return True


def _valid_payload(data: Any) -> bool:
    """{"cached_at": ISO8601, "services": [...]} の形かどうか。1 件でも
    必須項目・型が合わない、cached_at が解釈できない、あれば fetched_from が
    解釈できないときは False (呼び出し側は全取得経路へフォールバックする)。"""
    if not isinstance(data, dict):
        return False
    if _parse_iso(data.get("cached_at")) is None:
        return False
    if not isinstance(data.get("services"), list):
        return False
    if not all(_valid_service(e) for e in data["services"]):
        return False
    if "fetched_from" in data and _parse_iso(data.get("fetched_from")) is None:
        return False
    return True


def payload_for(services: List[Dict[str, Any]], fetched_from: datetime,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """detect-cache.json と `detect --json --cached` の出力で共通の中身。
    fetched_from はこの結果の元になった取得の開始時刻 (巻き戻し比較に使う)。"""
    now = now if now is not None else _utc_now()
    return {"cached_at": _iso(now), "fetched_from": _iso_us(fetched_from),
            "services": services}


def read_cache(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """detect-cache.json を読む。無い・読めない・形や型が不正なときは None。"""
    path = path if path is not None else DETECT_CACHE_PATH
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if _valid_payload(data) else None


def _fetched_from(data: Dict[str, Any]) -> Optional[datetime]:
    """キャッシュが基づく取得の開始時刻。旧形式 (キー無し) は cached_at で代用。"""
    return _parse_iso(data.get("fetched_from") or data.get("cached_at"))


def _entry_time(entry: Dict[str, Any], fallback: Optional[datetime]) -> Optional[datetime]:
    """エントリの有効な取得時刻 (自分の fetched_from、無ければ fallback)。"""
    return _parse_iso(entry.get("fetched_from")) or fallback


def _timed_services(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """前回キャッシュの services に、エントリごとの有効 fetched_from を入れて
    返す (旧形式のエントリはエンベロープの fetched_from / cached_at で代用)。
    merge の材料と provider 単位の比較に使う。"""
    env_time = _fetched_from(payload)
    out: List[Dict[str, Any]] = []
    for e in payload.get("services") or []:
        if not isinstance(e, dict):
            continue
        if e.get("fetched_from") is None and env_time is not None:
            e = dict(e, fetched_from=_iso_us(env_time))
        out.append(e)
    return out


def _merge_services(old: List[Dict[str, Any]], new: List[Dict[str, Any]],
                    old_env: Optional[datetime], new_from: Optional[datetime],
                    stamp: str) -> Tuple[List[Dict[str, Any]], bool]:
    """provider 単位で「新しい方」(有効 fetched_from が大きい方) を採る。

    new のエントリで fetched_from が無いものには stamp (この書き込みの取得
    開始時刻) を入れる。old にだけある provider はそのまま残す (書き込み側が
    知らない provider の既存エントリを消さない)。戻り値は (services, 変化あり)。"""
    old_by = {e["provider"]: e for e in old}
    used: Set[str] = set()
    out: List[Dict[str, Any]] = []
    changed = False
    for entry in new:
        pid = entry.get("provider") if isinstance(entry, dict) else None
        if pid is None or pid in used:
            continue
        used.add(pid)
        if entry.get("fetched_from") is None:
            entry = dict(entry, fetched_from=stamp)
        old_e = old_by.get(pid)
        if old_e is None:
            out.append(entry)
            changed = True
            continue
        new_t = _entry_time(entry, new_from)
        old_t = _entry_time(old_e, old_env)
        if old_t is None or new_t is None or new_t >= old_t:
            out.append(entry)
            if entry != old_e:
                changed = True
        else:
            out.append(old_e)
    for e in old:
        if e["provider"] not in used:
            out.append(e)
    return out, changed


def _write_locked(path: Path, services: Optional[List[Dict[str, Any]]],
                  fetched_from: datetime, now: Optional[datetime],
                  merge: Optional[Callable[[Optional[List[Dict[str, Any]]]],
                                          List[Dict[str, Any]]]]) -> bool:
    """ロックの内側: 読み→(マージ)→provider ごとに新しい方→原子的置換。

    merge があれば、読み直したばかりの有効な前回キャッシュの services
    (エントリごとの fetched_from 入り) を渡して呼び、その戻り値を使う。
    どの provider も新しい方に更新されなければ何も書かず False。"""
    stamp = _iso_us(fetched_from)
    new_from = _parse_iso(stamp)
    old = read_cache(path)
    old_services = _timed_services(old) if old is not None else []
    old_env = _fetched_from(old) if old is not None else None
    if merge is not None:
        services = merge(old_services)
    final, changed = _merge_services(
        old_services, services or [], old_env, new_from, stamp)
    if old is not None and not changed:
        return False
    data = (json.dumps(payload_for(final, fetched_from, now=now),
                       ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write(path, lambda handle: handle.write(data))
    return True


def write_cache(services: Optional[List[Dict[str, Any]]], fetched_from: datetime,
                path: Optional[Path] = None,
                now: Optional[datetime] = None,
                merge: Optional[Callable[[Optional[List[Dict[str, Any]]]],
                                        List[Dict[str, Any]]]] = None) -> bool:
    """detect-cache.json を原子的に書く。実際に更新して書ければ True。

    provider ごとに「新しい方」(各エントリの fetched_from、無ければこの
    書き込みの fetched_from / 旧キャッシュのエンベロープ時刻) を採るので、
    遅れて終わった常駐取得が他の取得の新しいエントリを巻き戻さない。
    読み→(マージ)→比較→置換は detect-cache.json.lock の flock で排他する。
    merge があれば、ロックの中で読み直した有効な前回キャッシュの services を
    渡して呼び、その戻り値を書く (前回値に依存する更新がロック外の読みを
    踏まないようにするため)。"""
    path = path if path is not None else DETECT_CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        return _write_locked(path, services, fetched_from, now, merge)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _write_locked(path, services, fetched_from, now, merge)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
