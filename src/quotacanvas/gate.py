"""残量に応じた AI ワーカー振り分け判定 (旧 ~/.claude/bin/quota-gate のロジック)。

status.json のトップレベル "windows" (表示フィルタ前の全窓) を読み、
[routing] 設定のワーカーごとに stop / reserve / go / boost / unknown を出す。

- evaluate() は純粋関数: ファイル I/O もネットワークもしない
- 判定できないときは必ず verdict "unknown" (fail-open: 起動は止めない)
- 文言は locale (ja/en) から。ja は今の quota-gate の出力と一致する
- burn (使い切りモード) の状態は routing-burn.json に置く (このモジュール担当)
- Mac 専用 (Path.home() の Library 配下を使う)

このモジュールは表示系 (render / PIL / status) を import しない軽い構成にして
ある (SessionStart フックなど高頻度の CLI 呼び出しからは file 側で使うため)。
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from .i18n import t

if TYPE_CHECKING:  # 循環 import を避ける (実行時は duck typing)
    from .config import Routing

# 判定に使う canonical な provider 名 (config.PROVIDERS と同じ。単独で保つ)
KNOWN_PROVIDERS = ("claude", "codex", "glm", "grok", "gemini")

# ワーカー名の制約。既定ワーカーの名前は status.json 由来の windows[].provider の
# 生の文字列なので、この条件を満たす名前だけを判定・表示に使う (format_line の
# 1 行は SessionStart フックで AI の文脈に入るため、怪しい文字列を載せない)。
# [routing.workers] のキー検証 (config._worker_map) も同じ正規表現を使う。
WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def safe_worker_name(name: Any) -> bool:
    return isinstance(name, str) and WORKER_NAME_RE.match(name) is not None

# quota-gate と同じ: 判定は残り % ではなくペース判定 (status) で決める。
# 残り何 % より「このペースでリセットまで持つか」が大事なため。
PACE_VERDICT = {
    "exhausted": ("stop", "exhausted"),  # 使い切った
    "over": ("reserve", "over"),         # このペースだとリセット前に尽きる
    "tight": ("go", "tight"),            # ほぼ使い切れる。止めない
    "on_track": ("go", "on_track"),      # だいたい使い切る見込み
    "spare": ("boost", "spare"),         # 使い残しが出る見込み。積極的に使う
    "fresh": ("boost", "fresh"),         # リセット直後で枠が空いている
    "unknown": ("go", "unknown"),        # 判断材料が無い
}
SHORT_PACE_VERDICT = dict(PACE_VERDICT, on_track=("boost", "on_track"))
VERDICT_RANK = {"stop": 0, "reserve": 1, "go": 2, "boost": 3}
SHOW_STATUS = frozenset(("over", "tight", "spare", "exhausted", "fresh"))

DEFAULT_FLOOR_PERCENT = 5.0
DEFAULT_FLOOR_HOURS = 24.0
DEFAULT_STALE_MINUTES = 20

# 使い切りモード (burn) の状態ファイル。status.json と同じディレクトリ。
BURN_PATH = Path.home() / "Library" / "Application Support" / "quota-canvas" / "routing-burn.json"


# ---------------------------------------------------------------- parsing helpers

def now_local() -> datetime:
    return datetime.now().astimezone()


def to_local(dt: datetime) -> datetime:
    try:
        return dt.astimezone()
    except (ValueError, OSError):
        return dt


def parse_dt(value: Any) -> Optional[datetime]:
    """ISO8601 文字列を tz-aware datetime へ。壊れていれば None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = to_local(dt)  # naive はローカル時刻扱い
    return dt


def norm_status(value: Any) -> str:
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("over", "tight", "on_track", "spare", "exhausted", "fresh"):
            return s
    return "unknown"


def norm_remaining(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def fmt_hhmm(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return to_local(dt).strftime("%H:%M")


def _fmt_dt(dt: Optional[datetime], now: datetime) -> Optional[str]:
    """リセット時刻などの表示。今日なら HH:MM、別の日なら MM/DD HH:MM。"""
    if dt is None:
        return None
    loc = to_local(dt)
    if loc.date() == to_local(now).date():
        return loc.strftime("%H:%M")
    return loc.strftime("%m/%d %H:%M")


def fmt_reset(value: Any, now: datetime) -> Optional[str]:
    """ISO8601 文字列 (または datetime) の表示用時刻。無ければ None。"""
    if isinstance(value, datetime):
        return _fmt_dt(value, now)
    return _fmt_dt(parse_dt(value), now)


def pct_str(remaining: Optional[float]) -> str:
    if remaining is None:
        return "?%"
    return "%d%%" % round(remaining)


def parse_until(value: str, now: datetime) -> Optional[datetime]:
    """burn --until の値 (HH:MM か ISO8601) を解釈する。無効なら None。

    HH:MM は今日の時刻で、すでに過ぎていれば明日にする。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) == 5 and text[2] == ":":
        try:
            hh, mm = int(text[:2]), int(text[3:])
        except ValueError:
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        until = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if until <= now:
            until += timedelta(days=1)
        return until
    dt = parse_dt(text)
    return to_local(dt) if dt is not None else None


# ------------------------------------------------------------------ status 入力

def status_windows(status: Optional[dict]) -> List[dict]:
    """status.json の "windows" (表示フィルタ前の全窓) を dict のリストで返す。"""
    if not isinstance(status, dict):
        return []
    windows = status.get("windows")
    if windows is None:
        # windows を書く前のエンジン (別の Mac からのコピーなど) は表示中の cards で代用する
        windows = status.get("cards")
    if not isinstance(windows, list):
        return []
    return [w for w in windows if isinstance(w, dict)]


def error_providers(status: Optional[dict]) -> Set[str]:
    """errors 配列に出てくる provider 名の集合 (canonical 名に整理する)。"""
    out: Set[str] = set()
    if not isinstance(status, dict):
        return out
    errors = status.get("errors")
    if not isinstance(errors, list):
        return out
    for err in errors:
        name = err.get("provider") if isinstance(err, dict) else err
        if not isinstance(name, str):
            continue
        text = name.strip().lower()
        for known in KNOWN_PROVIDERS:
            if text.startswith(known):
                out.add(known)
                break
    return out


def _is_stale_window(entry: dict, stale_minutes: int, now: datetime) -> bool:
    """その窓の残量を信用しないなら True。stale_error キーに null/空以外が入っている、
    または fetched_at が stale_minutes より古い (壊れた fetched_at も信用しない)。
    fetched_at キーが無い窓は status.json 全体の新しさ判定 (load_status) に任せる。"""
    if entry.get("stale_error"):
        return True
    if "fetched_at" not in entry:
        return False
    fetched = parse_dt(entry.get("fetched_at"))
    if fetched is None:
        return True
    return (now - to_local(fetched)) > timedelta(minutes=stale_minutes)


def load_status(path: Path, stale_minutes: int = DEFAULT_STALE_MINUTES,
                now: Optional[datetime] = None) -> Tuple[str, Optional[dict], str]:
    """status.json を読む。(state, data, detail) を返す。state は "ok" / "stale"。
    detail: missing / unreadable / broken / old / "" (paused は stale 扱いにしない)。
    ファイル I/O はここだけ (evaluate は純粋関数のまま)。"""
    if now is None:
        now = now_local()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return "stale", None, "missing"
    except (OSError, ValueError):
        return "stale", None, "unreadable"
    try:
        data = json.loads(raw)
    except ValueError:
        return "stale", None, "broken"
    if not isinstance(data, dict):
        return "stale", None, "broken"
    updated = parse_dt(data.get("updated_at"))
    if updated is None:
        # updated_at が無い・壊れている → 新しさを検証できないので stale
        return "stale", data, "broken"
    if (now - to_local(updated)) > timedelta(minutes=stale_minutes):
        return "stale", data, "old"
    return "ok", data, ""


def stale_time_str(path: Path, data: Optional[dict]) -> str:
    """stale 行の「何時時点」。updated_at から、無ければファイルの mtime。"""
    ts = fmt_hhmm(parse_dt(data.get("updated_at"))) if isinstance(data, dict) else None
    if ts is None:
        try:
            ts = datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%H:%M")
        except OSError:
            ts = "--:--"
    return ts


# ------------------------------------------------------------------ burn 状態

def load_burn(path: Path, now: Optional[datetime] = None) -> Dict[str, datetime]:
    """有効な使い切りモード {ワーカー名: 期限}。壊れたファイル・期限切れは無視。"""
    if now is None:
        now = now_local()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: Dict[str, datetime] = {}
    for name, until in (raw.items() if isinstance(raw, dict) else []):
        dt = parse_dt(until)
        if dt is not None and to_local(dt) > now:
            out[str(name)] = to_local(dt)
    return out


def _write_burn(path: Path, data: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def store_burn(path: Path, name: str, until: datetime, now: Optional[datetime] = None) -> None:
    """使い切りモードを 1 件設定する (書き込み時に期限切れのエントリを掃除)。"""
    if now is None:
        now = now_local()
    active = load_burn(path, now)
    active[name] = to_local(until)
    _write_burn(path, {k: v.isoformat() for k, v in sorted(active.items())})


def clear_burn(path: Path, name: str, now: Optional[datetime] = None) -> None:
    """使い切りモードを 1 件解除する (書き込み時に期限切れのエントリを掃除)。"""
    if now is None:
        now = now_local()
    active = load_burn(path, now)
    active.pop(name, None)
    _write_burn(path, {k: v.isoformat() for k, v in sorted(active.items())})


# ------------------------------------------------------------------ 判定

@dataclass
class WorkerResult:
    """ワーカー 1 件ぶんの判定。"""

    name: str
    verdict: str                     # stop | reserve | go | boost | unknown
    reason: str                      # exhausted / below_floor / over / tight / on_track /
                                     # spare / fresh / unknown / burn / stale / missing / error
    windows: List[str] = field(default_factory=list)  # 判定に使った窓 id
    remaining: Optional[float] = None   # 代表 (一番悪い) 窓の残り %
    status: Optional[str] = None        # 代表窓のペース判定
    resets_at: Optional[str] = None     # 代表窓のリセット時刻 (ISO8601)
    exhausts_at: Optional[str] = None   # 代表窓の枯渇見込み時刻 (ISO8601, over のとき)
    burn_until: Optional[str] = None    # 使い切りモードの期限 (ISO8601, 有効なときだけ)
    stale: bool = False
    provider: str = ""                  # リセット権表示用の provider 名 (一意でないとき "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "verdict": self.verdict, "reason": self.reason,
            "windows": list(self.windows), "remaining": self.remaining,
            "status": self.status, "resets_at": self.resets_at,
            "exhausts_at": self.exhausts_at, "burn_until": self.burn_until,
            "stale": self.stale, "provider": self.provider,
        }


@dataclass
class RoutingResult:
    """evaluate() の結果 (line / check / status.json の "routing" の材料)。"""

    workers: List[WorkerResult] = field(default_factory=list)
    at: Optional[str] = None  # status.json の updated_at (ISO8601)
    reset_credits: Dict[str, int] = field(default_factory=dict)  # provider → 期限内の権数
    holds: Dict[str, str] = field(default_factory=dict)  # reserve(over) の窓 id → resets_at

    def worker(self, name: str) -> Optional[WorkerResult]:
        for w in self.workers:
            if w.name == name:
                return w
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "reset_credits": dict(self.reset_credits),
            "holds": dict(self.holds),
            "workers": [w.to_dict() for w in self.workers],
        }


def _status_reset_credits(status: Optional[dict], now: datetime) -> Dict[str, int]:
    """期限内のリセット権の数 {provider: 件数}。表示だけに使う (判定は変えない)。"""
    out: Dict[str, int] = {}
    rc = status.get("reset_credits") if isinstance(status, dict) else None
    if not isinstance(rc, dict):
        return out
    for prov, items in rc.items():
        n = 0
        for it in items if isinstance(items, list) else []:
            exp = parse_dt(it.get("expires_at")) if isinstance(it, dict) else None
            if exp is None or to_local(exp) > now:
                n += 1
        if n:
            out[str(prov)] = n
    return out


def _judge(entry: dict, floor_percent: float, floor_hours: float, now: datetime, holds: dict,
           margin_minutes: Optional[float]) -> Tuple[str, str]:
    """窓 1 枚の判定。短い窓の over は予測時刻と前回状態を使う。"""
    st = norm_status(entry.get("status"))
    if st == "exhausted":
        return "stop", "exhausted"
    length = entry.get("length")
    short = (not isinstance(length, bool) and isinstance(length, (int, float))
             and math.isfinite(length) and 0 < length < 24 * 3600)
    margin = (margin_minutes * 60 if margin_minutes is not None else length * 0.1) if short else None
    rem = norm_remaining(entry.get("remaining"))
    if rem is not None and rem < floor_percent:
        reset = parse_dt(entry.get("resets_at"))
        floor_seconds = margin if short else floor_hours * 3600
        if floor_hours == 0 or reset is None or (reset - now).total_seconds() >= floor_seconds:
            return "stop", "below_floor"
    if not short:
        return PACE_VERDICT.get(st, ("go", "unknown"))
    if st == "over":
        reset = parse_dt(entry.get("resets_at"))
        exhaust = parse_dt(entry.get("exhausts_at"))
        if reset is None or exhaust is None:
            return "reserve", "over"
        old_reset = parse_dt(holds.get(entry.get("id")))
        threshold = margin * 2 / 3 if old_reset == reset else margin
        if (reset - exhaust).total_seconds() < threshold:
            return "go", "over_near_reset"
        return "reserve", "over"
    return SHORT_PACE_VERDICT.get(st, ("go", "unknown"))


def _match_windows(patterns: List[str], windows: List[dict]) -> List[dict]:
    """ワーカーの窓指定 (id 完全一致 or 末尾 * の前方一致) に合う独立窓だけ返す。
    part_of 付きの内訳窓はどの指定でも判定に入れない。"""
    out: List[dict] = []
    for w in windows:
        if w.get("part_of"):
            continue
        wid = w.get("id")
        if not isinstance(wid, str):
            continue
        for p in patterns:
            if not isinstance(p, str) or not p:
                continue
            if p.endswith("*"):
                if wid.startswith(p[:-1]):
                    out.append(w)
                    break
            elif wid == p:
                out.append(w)
                break
    return out


def _worker_providers(name: str, patterns: List[str]) -> Set[str]:
    """ワーカーのデータ元 provider 名の集合 (窓 id の ':' 前で判断する)。
    gemini:argon は gemini provider のデータなので、gemini の取得失敗で
    一緒に unknown になる (quota-gate の argon/agclaude 扱いと同じ)。"""
    if not patterns:
        return {name}
    out = {p.split(":", 1)[0] for p in patterns if isinstance(p, str) and p}
    return out or {name}


def _one_provider(providers: Set[str]) -> str:
    return next(iter(providers)) if len(providers) == 1 else ""


def default_workers(status: Optional[dict]) -> Dict[str, List[str]]:
    """[routing.workers] 未指定の既定: 取得できている provider ごとに、その
    provider 名で「その provider の独立窓 (part_of なし) すべて」。
    名前は status.json 由来の生の文字列なので、安全な形のものだけ採用する。"""
    windows = status_windows(status)
    present: List[str] = []
    for w in windows:
        p = w.get("provider")
        if isinstance(p, str) and p and p not in present and safe_worker_name(p):
            present.append(p)
    for p in error_providers(status):
        if p not in present:
            present.append(p)
    ordered = [p for p in KNOWN_PROVIDERS if p in present] \
        + [p for p in present if p not in KNOWN_PROVIDERS]
    return {
        p: [w["id"] for w in windows
            if w.get("provider") == p and not w.get("part_of") and "id" in w]
        for p in ordered
    }


def _worst(entries: List[dict], floor_percent: float, floor_hours: float, now: datetime, previous_holds: dict,
           margin_minutes: Optional[float], holds: Dict[str, str]) -> Optional[Tuple[dict, str, str]]:
    """代表窓 = 一番悪い判定の窓 (stop < reserve < go < boost、同順なら残量が少ない方)。"""
    best: Optional[Tuple[Tuple[int, float], dict, str, str]] = None
    for entry in entries:
        verdict, reason = _judge(entry, floor_percent, floor_hours, now, previous_holds, margin_minutes)
        wid, reset = entry.get("id"), entry.get("resets_at")
        if isinstance(wid, str):
            holds.pop(wid, None)
            reset_dt = parse_dt(reset)
            if verdict == "reserve" and reason == "over" and reset_dt is not None and reset_dt > now:
                holds[wid] = reset
        rem = norm_remaining(entry.get("remaining"))
        rank = (VERDICT_RANK[verdict], rem if rem is not None else float("inf"))
        if best is None or rank < best[0]:
            best = (rank, entry, verdict, reason)
    if best is None:
        return None
    return best[1], best[2], best[3]


def evaluate(status: Optional[dict], routing_cfg: "Routing",
             burn: Dict[str, datetime], now: datetime,
             previous: Optional[dict] = None) -> RoutingResult:
    """status.json の windows からワーカーごとの判定を作る (純粋関数)。

    routing_cfg は config.Routing (workers / floor_percent / stale_minutes を
    属性で持つオブジェクト)。burn は {ワーカー名: 期限}。期限切れは無視する。
    窓が見つからない / provider がエラー / データが古いときは verdict "unknown"
    を返し、起動を止めない (fail-open)。
    """
    windows = status_windows(status)
    errors = error_providers(status)
    floor = float(getattr(routing_cfg, "floor_percent", DEFAULT_FLOOR_PERCENT))
    floor_hours = float(getattr(routing_cfg, "floor_hours", DEFAULT_FLOOR_HOURS))
    stale_min = int(getattr(routing_cfg, "stale_minutes", DEFAULT_STALE_MINUTES))
    specs: Dict[str, List[str]] = getattr(routing_cfg, "workers", None) or default_workers(status)
    previous_holds = previous.get("holds") if isinstance(previous, dict) else None
    if not isinstance(previous_holds, dict):
        previous_holds = {}
    # 判定できなかった窓は、まだ有効な前回の hold を維持する。
    holds: Dict[str, str] = {}
    for wid, reset in previous_holds.items():
        reset_dt = parse_dt(reset)
        if isinstance(wid, str) and reset_dt is not None and reset_dt > now:
            holds[wid] = reset
    previous_holds = dict(holds)
    margin_minutes = getattr(routing_cfg, "short_over_margin_minutes", None)

    workers: List[WorkerResult] = []
    for name, patterns in specs.items():
        providers = _worker_providers(name, patterns)
        mine = [w for w in windows if w.get("provider") in providers]
        if any(_is_stale_window(w, stale_min, now) for w in mine):
            workers.append(WorkerResult(name=name, verdict="unknown", reason="stale",
                                        stale=True, provider=_one_provider(providers)))
            continue
        if providers & errors:
            workers.append(WorkerResult(name=name, verdict="unknown", reason="error",
                                        provider=_one_provider(providers)))
            continue
        matched = _match_windows(list(patterns or []), windows)
        if not matched:
            workers.append(WorkerResult(name=name, verdict="unknown", reason="missing",
                                        provider=_one_provider(providers)))
            continue
        picked = _worst(matched, floor, floor_hours, now, previous_holds, margin_minutes, holds)
        assert picked is not None
        entry, verdict, reason = picked
        until = burn.get(name)
        active = until is not None and to_local(until) > now
        if active and verdict in ("reserve", "go", "boost"):
            if verdict != "boost":
                reason = "burn"
            verdict = "boost"
        workers.append(WorkerResult(
            name=name, verdict=verdict, reason=reason,
            windows=[w.get("id") for w in matched if isinstance(w.get("id"), str)],
            remaining=norm_remaining(entry.get("remaining")),
            status=norm_status(entry.get("status")),
            resets_at=entry.get("resets_at") if isinstance(entry.get("resets_at"), str) else None,
            exhausts_at=entry.get("exhausts_at") if isinstance(entry.get("exhausts_at"), str) else None,
            burn_until=to_local(until).isoformat() if active else None,
            provider=_one_provider(providers),
        ))
    return RoutingResult(
        workers=workers,
        at=status.get("updated_at") if isinstance(status, dict) and isinstance(status.get("updated_at"), str) else None,
        reset_credits=_status_reset_credits(status, now),
        holds=holds,
    )


# ------------------------------------------------------------------ 表示 (line / check)

def _set_lang(lang: Optional[str]) -> None:
    from .i18n import set_language
    if lang:
        set_language(lang)


def stale_reason(detail: str) -> str:
    """status.json 自体が読めないときの unknown の reason。detail で区別する:
    missing → "missing_status"、old → "stale"、壊れ (broken / unreadable) → "broken"。"""
    if detail == "missing":
        return "missing_status"
    if detail == "old":
        return "stale"
    return "broken"


def format_line(result: RoutingResult, now: datetime, lang: Optional[str] = None) -> str:
    """quota-gate line と同じ形の 1 行。

    `quota: glm=boost(58%,~17:00) grok=reserve(85%,ペース超過·~10/28 21:11枯渇見込み,
    ~11/02 10:45) ... (17:29時点)`。判定できないワーカーは `=?`、古いデータは
    `=stale` (quota-gate と同じ)。
    """
    _set_lang(lang)
    if not result.workers:
        return ""  # ワーカーが 1 つも無いときは崩れた行ではなく何も出さない
    parts: List[str] = []
    for w in result.workers:
        if not safe_worker_name(w.name):
            continue  # status.json 由来の怪しい名前は行に載せない
        if w.verdict == "unknown":
            parts.append("%s=%s" % (w.name, "stale" if w.reason == "stale" else "?"))
            continue
        text = "%s=%s(%s" % (w.name, w.verdict, pct_str(w.remaining))
        if w.status == "over":
            text += "," + t("routing.gate.over")
            ex = fmt_reset(w.exhausts_at, now)
            if ex:
                text += t("routing.gate.exhausts", dt=ex)
        n = result.reset_credits.get(w.provider) if w.provider else None
        if n:
            text += "," + t("routing.gate.credits", n=n)
        if w.burn_until:
            b = fmt_reset(w.burn_until, now)
            if b:
                text += "," + t("routing.gate.burn", dt=b)
        reset = fmt_reset(w.resets_at, now)
        if reset:
            text += ",~" + reset
        elif w.status in SHOW_STATUS and w.status != "over":
            # リセット時刻が分からないときだけ生の status を添える (quota-gate と同じ)
            text += "," + w.status
        parts.append(text + ")")
    at = fmt_hhmm(parse_dt(result.at)) or "--:--"
    return t("routing.gate.line", body=" ".join(parts), at=at)


def format_stale_line(detail: str, path: Path, data: Optional[dict],
                      now: datetime, lang: Optional[str] = None) -> str:
    """status.json が無い・古い・読めないときの 1 行 (quota-gate と同じ)。"""
    _set_lang(lang)
    if detail == "missing":
        return t("routing.gate.stale_missing")
    reason = t("routing.gate.stale_reason_old") if detail == "old" \
        else t("routing.gate.stale_reason_broken")
    return t("routing.gate.stale_old", time=stale_time_str(path, data), reason=reason)


def check_message(worker: WorkerResult, now: datetime, lang: Optional[str] = None) -> Optional[str]:
    """`routing check` の stderr 1 行。go / boost は None (無言)。"""
    _set_lang(lang)
    if worker.verdict == "stop":
        if worker.remaining is not None:
            detail = t("routing.check.remains", pct=pct_str(worker.remaining))
        else:
            detail = worker.status or "unknown"
        return t("routing.check.stop", name=worker.name, detail=detail)
    if worker.verdict == "reserve":
        reset = fmt_reset(worker.resets_at, now)
        if worker.remaining is not None:
            detail = t("routing.check.remains_reset", pct=pct_str(worker.remaining),
                       dt=reset or "")
        else:
            detail = worker.status or "unknown"
        return t("routing.check.reserve", name=worker.name, detail=detail)
    if worker.verdict == "unknown":
        return t("routing.check.unknown", name=worker.name, reason=worker.reason)
    return None
