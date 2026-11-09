"""Muse (Meta のチャットボット https://muse.ai): 週間利用枠と追加トークン残量を取得する。

公開 API が無いので、ブラウザが使う Next.js Server Action
(fetchSubscriptionAction) を直接呼ぶ:

1. Action ID の解決: GET https://muse.ai/ (Cookie 付き) の HTML が参照する
   /_next/static/chunks/*.js を順に取得し、本文中の
   createServerReference)("<40 桁以上の hex>", ... "fetchSubscriptionAction")
   に一致する ID を探す。1 段目で見つからなければ、それらの本文に出てくる
   "static/chunks/*.js" 文字列 (遅延読み込みの一覧) を _next 配下として取得し、
   同じ正規表現で探す (2 段目)。取得する JS は合計 600 本までで、見つかった
   時点で打ち切る。ID は ~/Library/Application Support/quota-canvas/
   muse-action.json にキャッシュし (秘密は含まない)、次回はそれを先に使う。
   解決直後は verified=false で保存し、Server Action が成功したら
   verified=true に更新する。再解決するのは verified=true の ID が
   HTTP 404 や想定外の応答で失敗したときだけ (verified=false の ID では
   再解決せず Cookie 期限切れエラー。ポーリングごとに再探索しない)。
   ID が見つからない失敗は時刻をキャッシュに記録し、6 時間は再解決しない。
   解決には全体 60 秒・JS 1 本 5 秒・本文 5MB の上限を設ける。
2. 呼び出し: POST https://muse.ai/ に Next-Action: <ID>・Accept:
   text/x-component・本文 [{"includeAgreement":false}]。応答は RSC 形式の
   複数行テキストで、行頭 "<数字>:" を外して JSON として読み、success:true
   と subscription を持つ行を使う。

認証はログイン Cookie (ブラウザの Cookie ヘッダーの文字列をそのまま)。
キーチェーン quota-canvas/muse (`set-key muse`) → 環境変数 MUSE_COOKIE の順に
探す。Cookie の値はログ・エラー・status.json に出さない。HTTP 401/403、
または success:true を持つ行が無い応答は「Cookie の期限切れ」エラーにする。
リダイレクトは _base の同一 origin 制限付き opener を使う (別 origin へ
Cookie を送らない)。静的 JS の取得には Cookie を付けない。
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import keychain  # noqa: F401 (tests patch muse.keychain)
from ..atomic import atomic_write
from ..i18n import t
from ..models import Balance, ProviderUsage, Window
from ..platform_support.paths import data_dir
from ._base import (
    TIMEOUT, USER_AGENT,
    CredentialError, Detection, Provider,
    _from_epoch, _open, _window_identity, detect_api_key, finite_number,
    locate_api_key,
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
# Server Action (POST) の読み取り期限 (解決用の RESOLVE_BUDGET とは別)。
POST_DEADLINE = 20.0

# Action ID は Next.js のビルドごとに変わるので、解決済みの ID をファイルに
# キャッシュする (秘密は含まない)。置き場は status.json と同じデータ
# ディレクトリ。
ACTION_CACHE_NAME = "muse-action.json"

# 実例: (0,t.createServerReference)("<40 桁の hex>",t.callServer,void 0,
#   t.findSourceMapURL,"fetchSubscriptionAction")
_ACTION_ID_RE = re.compile(
    r'createServerReference\)\s*\(\s*"([0-9a-f]{40,})"[^)]{0,300}?"fetchSubscriptionAction"')
_ACTION_ID_SHAPE = re.compile(r"^[0-9a-f]{40,}$")
_CHUNK_PATH_RE = re.compile(r"/_next/static/chunks/[^\"'\s\\]+?\.js")
_LAZY_CHUNK_RE = re.compile(r'"(static/chunks/[^"\\]+?\.js)"')
# RSC (React Flight) 応答の行頭 ID は 16 進数 ("0:", "1:", "a:", "1f:" など)。
_LINE_ID_RE = re.compile(r"[0-9a-fA-F]+")


class _NoSubscription(ValueError):
    """RSC 応答に success:true の subscription 行が無い。"""


# ----------------------------------------------------------- credentials

def locate_muse_cookie() -> Tuple[str, str]:
    """(Cookie ヘッダー文字列, 取得元の説明)。無ければ CredentialError。"""
    return locate_api_key("muse", "MUSE_COOKIE")


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """Cookie があるか。通信しない。"""
    return detect_api_key("muse", "Muse", "MUSE_COOKIE")


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
    ID で Server Action が一度成功した実績、resolved_at は解決した時刻
    (epoch 秒。古いキャッシュには無いので None)。"""
    data = _read_action_cache()
    action_id = data.get("action_id")
    if not (isinstance(action_id, str) and _ACTION_ID_SHAPE.fullmatch(action_id)):
        return None
    stamp = data.get("resolved_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        stamp = None
    return action_id, data.get("verified") is True, stamp


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


def _post_text(url: str, headers: Dict[str, str], body: str,
               timeout: float = TIMEOUT) -> str:
    data = body.encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=dict(headers),
                                     method="POST")
    with _open(request, timeout) as response:
        return _read_bounded(response,
                             deadline=time.monotonic() + POST_DEADLINE,
                             io_timeout=timeout).decode("utf-8", errors="replace")


def _auth_headers(cookie: str) -> Dict[str, str]:
    return {"Cookie": cookie, "User-Agent": USER_AGENT}


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


def _resolve_action_id(cookie: str) -> str:
    """fetchSubscriptionAction の Action ID を探す。見つからなければ ValueError。

    1 段目: トップページ HTML が参照する chunks JS を順に取得して探す。
    2 段目: それらの本文に出てくる "static/chunks/<名前>.js" (遅延読み込みの
    一覧) を _next 配下として取得して探す。JS 取得の合計は MAX_JS_FETCHES
    まで。URL はすべて https://muse.ai の同一 origin に固定する。解決全体
    (HTML 取得 + 全 JS) の時間は RESOLVE_BUDGET まで。"""
    deadline = time.monotonic() + RESOLVE_BUDGET
    try:
        html = _get_text(MUSE_PAGE_URL, _auth_headers(cookie),
                         deadline=deadline)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CredentialError(t("providers.muse_cookie_expired")) from None
        raise
    fetched = 0
    deferred: List[str] = []
    seen_deferred = set()

    def fetch_js(url: str) -> str:
        nonlocal fetched
        fetched += 1
        return _get_text(url, {"User-Agent": USER_AGENT},
                         timeout=JS_TIMEOUT, deadline=deadline)

    for path in _chunk_paths(html):
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


def _resolve_and_cache(cookie: str) -> str:
    """抑制付きの Action ID 解決。

    直前の解決失敗 (ID が見つからない) から RESOLVE_BACKOFF 時間は再解決せず
    短いエラーにする (JS を何百本も取り直さない)。解決できた ID はキャッシュ
    する。"""
    stamp = _read_action_cache().get("resolve_failed_at")
    if (not isinstance(stamp, bool) and isinstance(stamp, (int, float))
            and time.time() - stamp < RESOLVE_BACKOFF):
        raise CredentialError(t("providers.muse_resolve_unavailable"))
    try:
        action_id = _resolve_action_id(cookie)
    except ValueError:
        _write_action_cache({"resolve_failed_at": int(time.time())})
        raise
    # まだ成功実績のない ID (verified=false) として保存する。
    _write_action_cache({"action_id": action_id, "verified": False,
                         "resolved_at": int(time.time())})
    return action_id


def _post_action(cookie: str, action_id: str) -> str:
    """Server Action を呼び、RSC 応答の生テキストを返す。"""
    return _post_text(MUSE_PAGE_URL, {
        "Cookie": cookie,
        "User-Agent": USER_AGENT,
        "Next-Action": action_id,
        "Accept": "text/x-component",
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": MUSE_ORIGIN,
    }, '[{"includeAgreement":false}]')


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


def parse_muse(text: Any, now: Optional[datetime] = None) \
        -> Tuple[str, List[Window], List[Balance]]:
    """(name, windows, balances) from the fetchSubscriptionAction RSC response.

    週間枠: usage.percentUsed を使用率にする (state == "UNLIMITED" なら 0、
    percentUsed が null なら窓を出さない)。追加トークン: topupTotal > 0 の
    とき残り % = balance / total の Balance (text 行で「残り NN%」と表示、
    窓にはしない) を出す。生のトークン数は表示文言 (topupRowValueLabel)
    と一致しないので使わない。窓も残高も無ければ ValueError。"""
    if not isinstance(text, str):
        raise ValueError("invalid response")
    subscription = _extract_subscription(text)

    windows: List[Window] = []
    usage = subscription.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    state = usage.get("state")
    percent_raw = usage.get("percentUsed")
    resets_at = _from_epoch(usage.get("resetsAt"))
    if state == "UNLIMITED" or percent_raw is not None:
        used = 0.0 if state == "UNLIMITED" else _percent(percent_raw)
        key, label, length = _window_identity(7 * 86400)
        windows.append(Window("muse", key, label, used, resets_at, length))

    balances: List[Balance] = []
    total = _opt_number(subscription.get("topupTotal"))
    balance = _opt_number(subscription.get("topupBalance"))
    if total is not None and total > 0 and balance is not None:
        left = round(min(100.0, max(0.0, balance / total * 100.0)), 1)
        balances.append(Balance(
            "TOKENS", left, t("providers.muse.topup"),
            t("providers.muse.topup_left", pct=f"{left:g}")))

    if not windows and not balances:
        raise ValueError("no quota")

    tier = subscription.get("tier")
    tier_name = tier.get("name") if isinstance(tier, dict) else None
    if isinstance(tier_name, str) and tier_name.strip():
        name = "Muse " + tier_name.strip()
    else:
        name = "Muse"
    return name, windows, balances


# ---------------------------------------------------------------- fetch

def _resolve_retryable(resolved_at: Optional[float]) -> bool:
    """verified=false の ID でも失敗時に再解決してよいか。

    解決から RESOLVE_BACKOFF 時間過ぎていればビルド更新で古くなった可能性
    があるので 1 回だけ解決し直す。resolved_at が無い古いキャッシュは
    期限切れ扱い。"""
    return (resolved_at is None
            or time.time() - resolved_at >= RESOLVE_BACKOFF)


def _fetch_usage(cookie: str, now: datetime) \
        -> Tuple[str, List[Window], List[Balance]]:
    """Server Action を呼んで (name, windows, balances) を返す。

    キャッシュした Action ID を先に使う。再解決してよいのは成功実績の
    ある ID (verified=true)、または解決から十分経った verified=false の
    ID が HTTP 404 や success:true の無い応答で失敗したときだけ。直近で
    解決した (verified=false) ID で失敗したときは解決し直さず
    「Cookie の期限切れ」エラーにする (ポーリングごとに再探索しない)。"""
    cached = _cached_action()
    for _attempt in range(2):
        if cached is None:
            cached = (_resolve_and_cache(cookie), False, int(time.time()))
        action_id, verified, resolved_at = cached
        try:
            text = _post_action(cookie, action_id)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise CredentialError(t("providers.muse_cookie_expired")) from None
            if exc.code == 404:
                if verified or _resolve_retryable(resolved_at):
                    cached = None  # 1 回だけ解決し直す
                    continue
                raise CredentialError(t("providers.muse_cookie_expired")) \
                    from None
            raise
        try:
            result = parse_muse(text, now)
        except _NoSubscription:
            if verified or _resolve_retryable(resolved_at):
                cached = None  # 1 回だけ解決し直す
                continue
            raise CredentialError(t("providers.muse_cookie_expired")) from None
        if not verified:
            entry: Dict[str, Any] = {"action_id": action_id, "verified": True}
            if resolved_at is not None:
                entry["resolved_at"] = resolved_at
            _write_action_cache(entry)
        return result
    raise AssertionError("unreachable")  # pragma: no cover


def fetch(now: datetime) -> ProviderUsage:
    cookie, _source = locate_muse_cookie()
    name, windows, balances = _fetch_usage(cookie, now)
    return ProviderUsage("muse", name, windows, None, now,
                         balances=balances)


PROVIDER = Provider(
    id="muse", name="Muse", needs_key=True, detect=detect, fetch=fetch,
    display_name="Muse (Meta)",
)
