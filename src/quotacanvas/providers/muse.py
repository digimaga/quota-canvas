"""Muse (Meta のチャットボット https://muse.ai): 週間利用枠と追加トークン残量。

公開 API が無く、エンジン (urllib) からの Server Action POST はボット対策で
403 になるため、利用枠の取得はメニューバーアプリが担う:

1. アプリが専用 WKWebsiteDataStore の WebKit (muse.ai ログイン済み) で
   ページと同じ fetch を実行し、応答を `quota-canvas muse-ingest` に渡す。
2. `muse-ingest --status N` が本文を検証して、データディレクトリの
   muse-usage.json に {fetched_at, status, result} を原子的に書く
   (本文そのものや Cookie は残さない)。result は 200 で subscription を
   解釈できたとき {used_percent, resets_at, tier, topup_left_percent} の
   秘密なし dict、401/403 のとき "logged_out"、それ以外は "error"
   (+detail)。`--clear` はファイルを消す。200 で解析に成功したら
   Action ID キャッシュを verified=true にする。
3. fetch は muse-usage.json を読んで ProviderUsage を作るだけ (通信しない)。
   ファイル無し・古い (USAGE_MAX_AGE 秒超)・logged_out・error は取得失敗
   (last-good の stale 表示に乗る)。

Action ID (fetchSubscriptionAction) は `quota-canvas muse-action-id` が
解決する: GET https://muse.ai/ (Cookie 不要) の HTML が参照する
/_next/static/chunks/*.js を順に取得し、本文中の
createServerReference)("<40 桁以上の hex>", ... "fetchSubscriptionAction")
に一致する ID を探す。1 段目で見つからなければ、それらの本文に出てくる
"static/chunks/*.js" 文字列 (遅延読み込みの一覧) を _next 配下として取得し、
同じ正規表現で探す (2 段目)。取得する JS は合計 600 本までで、見つかった
時点で打ち切る。ID は muse-action.json にキャッシュし (秘密は含まない)、
キャッシュがあればそれを、`--refresh` なら解決し直す。解決直後は
verified=false で保存し、アプリからの応答 (muse-ingest) が解析成功したら
verified=true に更新する。ID が見つからない失敗は resolve_failed_at を
記録し、6 時間は再解決しない。解決には全体 60 秒・JS 1 本 5 秒・本文 5MB
の上限を設ける。静的 JS の取得にも Cookie は付けない。
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..atomic import atomic_write
from ..i18n import t
from ..models import Balance, ProviderUsage, Window
from ..platform_support.paths import data_dir
from ._base import (
    TIMEOUT, USER_AGENT, CredentialError, Detection, Provider,
    _from_epoch, _open, _window_identity, finite_number,
)

MUSE_ORIGIN = "https://muse.ai"
MUSE_PAGE_URL = MUSE_ORIGIN + "/"

# Action ID 解決で取得する JS の合計上限 (ビルドごとにチャンク数は変わる)。
MAX_JS_FETCHES = 600
# JS 1 本の取得タイムアウトと、解決全体 (HTML + 全 JS) の時間上限。
JS_TIMEOUT = 5.0
RESOLVE_BUDGET = 60.0
# 解決に失敗したとき (ID が見つからない)、この時間は再解決しない。
RESOLVE_BACKOFF = 6 * 3600.0
# 応答本文の読み取り上限と 1 回に読む塊の大きさ。
READ_LIMIT = 5 * 1024 * 1024
READ_CHUNK = 64 * 1024

# Action ID と利用枠データのキャッシュ。置き場は status.json と同じデータ
# ディレクトリ (どちらも秘密は含まない)。
ACTION_CACHE_NAME = "muse-action.json"
USAGE_FILE_NAME = "muse-usage.json"

# muse-usage.json がこの秒数より古いときは「アプリが更新していない」取得失敗。
# アプリのポーリング間隔 (5 分) より十分長い。
USAGE_MAX_AGE = 20 * 60.0

# muse-ingest が stdin から読む本文の上限 (1MB)。アプリ側も同じ長さで切る。
INGEST_BODY_LIMIT = 1024 * 1024

# 実例: (0,t.createServerReference)("<40 桁の hex>",t.callServer,void 0,
#   t.findSourceMapURL,"fetchSubscriptionAction")
_ACTION_ID_RE = re.compile(
    r'createServerReference\)\s*\(\s*"([0-9a-f]{40,})"[^)]{0,300}?"fetchSubscriptionAction"')
_ACTION_ID_SHAPE = re.compile(r"^[0-9a-f]{40,}$")
_CHUNK_PATH_RE = re.compile(r"/_next/static/chunks/[^\"'\s\\]+?\.js")
# --chunks-stdin で受け取るパス。muse.ai 同一 origin の /_next/static/chunks/ 配下だけ受理。
_CHUNK_PATH_SHAPE = re.compile(r"^/_next/static/chunks/[^\"'\s\\]+?\.js$")
_LAZY_CHUNK_RE = re.compile(r'"(static/chunks/[^"\\]+?\.js)"')
# RSC (React Flight) 応答の行頭 ID は 16 進数 ("0:", "1:", "a:", "1f:" など)。
_LINE_ID_RE = re.compile(r"[0-9a-fA-F]+")


class _NoSubscription(ValueError):
    """RSC 応答に success:true の subscription 行が無い。"""


# ------------------------------------------------------------- detection

def _usage_file() -> Path:
    """muse-usage.json の置き場。テストで差し替えられるよう関数にしている。"""
    return data_dir() / USAGE_FILE_NAME


def detect() -> Detection:
    """muse-usage.json があれば検出 (通信しない)。"""
    if _usage_file().exists():
        return Detection(True, t("providers.muse.source_app"))
    return Detection(False, None, t("providers.muse.login_required"))


# -------------------------------------------------------- action id cache

def _action_cache_file() -> Path:
    """Action ID のキャッシュ。テストで差し替えられるよう関数にしている。"""
    return data_dir() / ACTION_CACHE_NAME


def _read_action_cache() -> Dict[str, Any]:
    try:
        data = json.loads(_action_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_action_cache(data: Dict[str, Any]) -> None:
    try:
        payload = (json.dumps(data) + "\n").encode("utf-8")
        atomic_write(_action_cache_file(), lambda handle: handle.write(payload))
    except OSError:
        pass  # キャッシュを書けなくても取得自体は成功させる


def _cached_action() -> Optional[Tuple[str, bool, Optional[float]]]:
    """キャッシュの (action_id, verified, resolved_at)。verified はその
    ID で応答が一度解析できた実績、resolved_at は解決した時刻
    (epoch 秒。古いキャッシュには無いので None)。"""
    data = _read_action_cache()
    action_id = data.get("action_id")
    if not (isinstance(action_id, str) and _ACTION_ID_SHAPE.fullmatch(action_id)):
        return None
    stamp = data.get("resolved_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        stamp = None
    return action_id, data.get("verified") is True, stamp


def _mark_action_verified() -> None:
    """muse-ingest が 200 を解析できたら、キャッシュの ID を verified=true にする。"""
    data = _read_action_cache()
    if data.get("action_id") and data.get("verified") is not True:
        data["verified"] = True
        _write_action_cache(data)


# ------------------------------------------------------------------- HTTP

def _set_sock_timeout(response, seconds: float) -> None:
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


def _read_bounded(response, deadline: Optional[float] = None,
                  io_timeout: float = TIMEOUT) -> bytes:
    """本文を READ_CHUNK ずつ読む。READ_LIMIT を超えたら ValueError、
    deadline (time.monotonic の期限) を超えたら TimeoutError で打ち切る。

    response.read() は内部で受信を繰り返すので、1 recv で戻る read1()
    (無ければ read) を使い、各読み込みの前に残り時間を確認する。"""
    chunks: List[bytes] = []
    total = 0
    read = getattr(response, "read1", None) or response.read
    while True:
        sock_timeout = io_timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("muse response read deadline")
            sock_timeout = min(io_timeout, remaining)
        _set_sock_timeout(response, sock_timeout)
        chunk = read(READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > READ_LIMIT:
            raise ValueError("muse response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _get_text(url: str, headers: Dict[str, str], timeout: float = TIMEOUT,
              deadline: Optional[float] = None) -> str:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with _open(request, timeout) as response:
        return _read_bounded(response, deadline,
                             timeout).decode("utf-8", errors="replace")


# ------------------------------------------------------ action id resolve

def _chunk_paths(html: str) -> List[str]:
    """HTML 中の /_next/static/chunks/*.js のパスを重複なく並び順どおりに返す。"""
    seen = set()
    paths = []
    for match in _CHUNK_PATH_RE.finditer(html):
        path = match.group(0)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _find_action_id(js: str) -> Optional[str]:
    match = _ACTION_ID_RE.search(js)
    return match.group(1) if match else None


def _resolve_from_paths(paths, deadline: float) -> str:
    """muse.ai の絶対パス一覧 ("/_next/...") の JS を順に取って ID を探す。

    1 段目で見つからなければ、それらの本文に出てくる "static/chunks/<名前>.js"
    (遅延読み込みの一覧) を _next 配下として取得して探す (2 段目)。JS 取得の
    合計は MAX_JS_FETCHES まで、URL はすべて https://muse.ai の同一 origin。
    deadline (time.monotonic) まで。見つからなければ ValueError。"""
    fetched = 0
    deferred: List[str] = []
    seen_deferred = set()

    def fetch_js(url: str) -> str:
        nonlocal fetched
        fetched += 1
        return _get_text(url, {"User-Agent": USER_AGENT},
                         timeout=JS_TIMEOUT, deadline=deadline)

    for path in paths:
        if fetched >= MAX_JS_FETCHES or time.monotonic() >= deadline:
            break
        try:
            js = fetch_js(MUSE_ORIGIN + path)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise  # 429 は握りつぶさず Retry-After の処理へ返す
            continue
        except OSError:
            continue
        action_id = _find_action_id(js)
        if action_id is not None:
            return action_id
        for lazy in _LAZY_CHUNK_RE.findall(js):
            if lazy not in seen_deferred:
                seen_deferred.add(lazy)
                deferred.append(lazy)
    for lazy in deferred:
        if fetched >= MAX_JS_FETCHES or time.monotonic() >= deadline:
            break
        try:
            js = fetch_js(MUSE_ORIGIN + "/_next/" + lazy)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise
            continue
        except OSError:
            continue
        action_id = _find_action_id(js)
        if action_id is not None:
            return action_id
    raise ValueError("muse action id not found")


def _resolve_action_id() -> str:
    """トップページ HTML が参照する chunks JS から ID を解決する。

    解決全体 (HTML 取得 + 全 JS) の時間は RESOLVE_BUDGET まで。"""
    deadline = time.monotonic() + RESOLVE_BUDGET
    html = _get_text(MUSE_PAGE_URL, {"User-Agent": USER_AGENT},
                     deadline=deadline)
    return _resolve_from_paths(_chunk_paths(html), deadline)


def _resolve_and_cache() -> str:
    """抑制付きの Action ID 解決。

    直前の解決失敗 (ID が見つからない) から RESOLVE_BACKOFF 時間は再解決せず
    短いエラーにする (JS を何百本も取り直さない)。解決できた ID はキャッシュ
    する。"""
    stamp = _read_action_cache().get("resolve_failed_at")
    if (not isinstance(stamp, bool) and isinstance(stamp, (int, float))
            and time.time() - stamp < RESOLVE_BACKOFF):
        raise CredentialError(t("providers.muse_resolve_unavailable"))
    try:
        action_id = _resolve_action_id()
    except ValueError:
        _write_action_cache({"resolve_failed_at": int(time.time())})
        raise
    # まだ成功実績のない ID (verified=false) として保存する。
    _write_action_cache({"action_id": action_id, "verified": False,
                         "resolved_at": int(time.time())})
    return action_id


def _resolve_chunks(paths) -> str:
    """`--chunks-stdin` 用: ページが読み込んだ chunk のパス一覧から解決する。

    muse.ai 同一 origin の /_next/static/chunks/*.js の絶対パスだけを受理
    する。明示的な解決 (ignore_backoff と同じ扱い) なので 6 時間抑制を無視
    し、失敗も resolve_failed_at に記録しない。"""
    clean: List[str] = []
    seen = set()
    for raw in paths:
        path = raw.strip() if isinstance(raw, str) else ""
        if _CHUNK_PATH_SHAPE.fullmatch(path) and path not in seen:
            seen.add(path)
            clean.append(path)
    if not clean:
        raise ValueError("no muse chunks")
    action_id = _resolve_from_paths(clean, time.monotonic() + RESOLVE_BUDGET)
    # まだ成功実績のない ID (verified=false) として保存する。
    _write_action_cache({"action_id": action_id, "verified": False,
                         "resolved_at": int(time.time())})
    return action_id


def action_id(refresh: bool = False, chunks=None) -> str:
    """`muse-action-id` コマンド用: 解決済みの Action ID を返す。

    キャッシュがあればそれを使い、refresh=True なら解決し直す
    (POST が 404 = キャッシュの ID が古いときの再試行用)。
    chunks (ページが読み込んだ chunk のパス) が渡されたら HTML は取らず
    それから解決する。見つからなければ ValueError / CredentialError /
    HTTPError。"""
    if not refresh:
        cached = _cached_action()
        if cached is not None:
            return cached[0]
    if chunks is not None:
        return _resolve_chunks(chunks)
    return _resolve_and_cache()


# --------------------------------------------------------------- parser

def _extract_subscription(text: str) -> Dict[str, Any]:
    """RSC 応答 (行頭 "<数字>:" + JSON の複数行) から success:true の
    subscription を取り出す。無ければ _NoSubscription。"""
    for line in text.splitlines():
        num, sep, payload = line.partition(":")
        if not sep or not _LINE_ID_RE.fullmatch(num.strip()):
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("success") is True \
                and isinstance(data.get("subscription"), dict):
            return data["subscription"]
    raise _NoSubscription("muse: no subscription in response")


def _percent(value: Any) -> float:
    """使用 % を 0〜100 に丸める。bool・非数値・非有限は ValueError。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid percent")
    percent = float(value)
    if not math.isfinite(percent):
        raise ValueError("invalid percent")
    return min(100.0, max(0.0, percent))


def _opt_number(value: Any) -> Optional[float]:
    """数値化できるなら float、できなければ None (bool・非有限も None)。"""
    try:
        return finite_number(value)
    except ValueError:
        return None


def _parsed_result(subscription: Dict[str, Any]) -> Dict[str, Any]:
    """subscription dict → muse-usage.json に書く秘密なしの result dict。

    週間枠: usage.percentUsed を使用率にする (state == "UNLIMITED" なら 0、
    percentUsed が null なら枠を出さない)。追加トークン: topupTotal > 0 の
    とき残り % = balance / total を topup_left_percent にする (生のトークン
    数は表示文言と一致しないので使わない)。枠も残高も無ければ ValueError。"""
    result: Dict[str, Any] = {}
    usage = subscription.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    state = usage.get("state")
    percent_raw = usage.get("percentUsed")
    if state == "UNLIMITED" or percent_raw is not None:
        result["used_percent"] = \
            0.0 if state == "UNLIMITED" else _percent(percent_raw)
        resets = _from_epoch(usage.get("resetsAt"))
        result["resets_at"] = resets.timestamp() if resets is not None else None

    total = _opt_number(subscription.get("topupTotal"))
    balance = _opt_number(subscription.get("topupBalance"))
    if total is not None and total > 0 and balance is not None:
        result["topup_left_percent"] = round(
            min(100.0, max(0.0, balance / total * 100.0)), 1)

    tier = subscription.get("tier")
    tier_name = tier.get("name") if isinstance(tier, dict) else None
    if isinstance(tier_name, str) and tier_name.strip():
        result["tier"] = tier_name.strip()

    if "used_percent" not in result and "topup_left_percent" not in result:
        raise ValueError("no quota")
    return result


def _usage_from_parsed(parsed: Dict[str, Any], now: datetime) -> ProviderUsage:
    """muse-usage.json の result dict → ProviderUsage。

    週間枠の窓 (muse:weekly、7 日) と、追加トークンの残高行
    (text 行で「残り NN%」と表示、窓にはしない) を作る。"""
    windows: List[Window] = []
    used = parsed.get("used_percent")
    if used is not None:
        key, label, length = _window_identity(7 * 86400)
        windows.append(Window("muse", key, label, _percent(used),
                              _from_epoch(parsed.get("resets_at")), length))

    balances: List[Balance] = []
    left = _opt_number(parsed.get("topup_left_percent"))
    if left is not None:
        left = min(100.0, max(0.0, left))
        balances.append(Balance(
            "TOKENS", left, t("providers.muse.topup"),
            t("providers.muse.topup_left", pct=f"{left:g}")))

    if not windows and not balances:
        raise ValueError("no quota")

    tier = parsed.get("tier")
    name = "Muse " + tier.strip() \
        if isinstance(tier, str) and tier.strip() else "Muse"
    return ProviderUsage("muse", name, windows, None, now, balances=balances)


def parse_muse(text: Any, now: Optional[datetime] = None) \
        -> Tuple[str, List[Window], List[Balance]]:
    """(name, windows, balances) from the fetchSubscriptionAction RSC response."""
    if not isinstance(text, str):
        raise ValueError("invalid response")
    usage = _usage_from_parsed(_parsed_result(_extract_subscription(text)),
                               now or datetime.now(timezone.utc))
    return usage.name, usage.windows, usage.balances


# ---------------------------------------------------------------- ingest

def _read_usage_file() -> Dict[str, Any]:
    try:
        data = json.loads(_usage_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_usage_file(payload: Dict[str, Any]) -> None:
    payload_bytes = (json.dumps(payload) + "\n").encode("utf-8")
    atomic_write(_usage_file(), lambda handle: handle.write(payload_bytes))


def ingest_usage(status: int, text: str,
                 now: Optional[datetime] = None) -> str:
    """アプリの WebKit が取ってきた応答を muse-usage.json に保存する。

    戻り値は "parsed" | "logged_out" | "error"。本文そのものや Cookie は
    保存しない。200 で解析に成功したら Action ID キャッシュを
    verified=true にする。"""
    fetched_at = int((now or datetime.now(timezone.utc)).timestamp())
    payload: Dict[str, Any] = {"fetched_at": fetched_at, "status": int(status)}
    if status in (401, 403):
        payload["result"] = "logged_out"
        kind = "logged_out"
    elif status == 200:
        try:
            payload["result"] = _parsed_result(_extract_subscription(text))
            kind = "parsed"
        except ValueError as exc:
            payload["result"] = "error"
            payload["detail"] = str(exc)[:200]
            kind = "error"
    else:
        payload["result"] = "error"
        payload["detail"] = "HTTP %d" % status
        kind = "error"
    _write_usage_file(payload)
    if kind == "parsed":
        _mark_action_verified()
    return kind


def clear_usage_file() -> None:
    """`muse-ingest --clear`: muse-usage.json を消す。無ければ成功扱い。

    「無かった」以外の削除失敗 (OSError) は呼び出し側に伝える。"""
    try:
        _usage_file().unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- fetch

def fetch(now: datetime) -> ProviderUsage:
    """muse-usage.json を読んで ProviderUsage を作る (通信しない)。

    ファイル無し・logged_out・error・古い (USAGE_MAX_AGE 秒超) は取得失敗
    (last-good の stale 表示に乗る)。"""
    data = _read_usage_file()
    result = data.get("result") if data else None
    if result is None:
        raise CredentialError(t("providers.muse.login_required"))
    if result == "logged_out":
        raise CredentialError(t("providers.muse.logged_out"))
    if not isinstance(result, dict):
        detail = data.get("detail")
        raise CredentialError(t("providers.muse.ingest_error",
                                detail=detail if isinstance(detail, str)
                                else "?"))
    fetched_at = data.get("fetched_at")
    age = (now.timestamp() - fetched_at
           if isinstance(fetched_at, (int, float))
           and not isinstance(fetched_at, bool) else None)
    if age is None or age > USAGE_MAX_AGE:
        raise CredentialError(t("providers.muse.app_stale"))
    try:
        return _usage_from_parsed(result, now)
    except (TypeError, ValueError) as exc:
        raise CredentialError(
            t("providers.muse.ingest_error", detail=str(exc)))


PROVIDER = Provider(
    id="muse", name="Muse", needs_key=False, detect=detect, fetch=fetch,
    display_name="Muse (Meta)",
)
