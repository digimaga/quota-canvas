"""ローカル LLM サーバー (OpenAI 互換) の並列枠の空き状況。

ローカルサーバーは Prometheus 形式の /metrics を出すので、標準ライブラリだけ
で解析して「処理中 n / 待ち n」を読む。並列数の上限はサーバーが返さないため
設定 ([providers.local.<名前>].capacity) で持つ。API キーが要るときは
キーチェーン account `local:<名前>` → 環境変数 `api_key_env` の順に探し、
無ければヘッダーを送らない。

窓 (Window) とは違い、残量 % やリセット時刻の概念は無い。成功時は
ProviderUsage.slots に LocalSlots を載せ、失敗時は error に短い理由
(秘密を含まない) を入れる。キーや例外本文を外に漏らさないのは他の
プロバイダー (_base._short_reason) と同じ方針。
"""

from __future__ import annotations

import math
import os
import re
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import keychain
from ..models import LocalSlots, ProviderUsage
from . import _base
from ._base import USER_AGENT, _short_reason

# エンジンごとの指標名の既定 (running, waiting)。
# tensorfold のみ実測例あり。ほかは各エンジンの公開指標名。
ENGINE_METRICS: Dict[str, Tuple[str, str]] = {
    "tensorfold": ("tensorfold:num_requests_running", "tensorfold:num_requests_waiting"),
    "vllm": ("vllm:num_requests_running", "vllm:num_requests_waiting"),
    "sglang": ("sglang:num_running_reqs", "sglang:num_queue_reqs"),
    "llamacpp": ("llamacpp:requests_processing", "llamacpp:requests_deferred"),
}
LOCAL_ENGINES = tuple(ENGINE_METRICS) + ("custom",)

DEFAULT_TIMEOUT = 3.0
LOCAL_TIMEOUT_MAX = 30.0

# 待ちが続いているかを確かめるための取り直し回数 (最初の 1 回を含む上限)。
QUEUE_CONFIRM_SAMPLES = 3
# 取り直しの間隔 (秒)。
QUEUE_CONFIRM_INTERVAL = 2.0
# 取り直し時の timeout の上限 (秒)。routing local-check がワーカー起動前に
# 同期で呼ぶので、待ちがあるときの遅延を小さく抑える。
QUEUE_CONFIRM_TIMEOUT = 2.0

# /metrics の本文の読み取り上限 (巨大な応答で固まらないための保険)。
MAX_METRICS_BYTES = 1024 * 1024

# Prometheus の 1 行: 名前 [ {ラベル} ] 値 [ タイムスタンプ ]。
# ラベル部は引用符を考慮して対応させる: ラベル値 (引用符内) には } や引用符
# (エスケープ付き)・空白も書けるので、単純な [^}]* や貪欲な .* だと
# OpenMetrics の exemplar (`m{a="x"} 1 # {trace_id="t"} 7`) まで飲み込んで
# しまう。上の位の } で閉じるようにしている。
_LABELS_RE = r"\{(?:[^\"}]|\"(?:[^\"\\]|\\.)*\")*\}"
_LINE_RE = re.compile(r"^([^\s{]+)(?:%s)?[ \t]+(.+)$" % _LABELS_RE)
# 正規表現に合わなかった行の指標名引き当て用 (ラベル部が壊れている行)。
_HEAD_RE = re.compile(r"^([^\s{]+)\{")

#: 解析失敗の短い理由 (i18n しない: 他のプロバイダーの error と同じく英語の短語)
INVALID_METRICS = "invalid metrics"
METRIC_MISSING = "metric not found"


class MetricsError(ValueError):
    """/metrics の解析失敗。メッセージは短い固定語 (秘密を含まない)。"""


def parse_metrics(text: str) -> Tuple[Dict[str, float], Set[str]]:
    """Prometheus のテキスト形式を ({指標名: 値}, 壊れた行のあった指標名) にする。

    ラベル付きの行 (``name{model="x"} 1``) は同じ指標名の値を合計する。
    コメント行 (# ...) と空行は無視。壊れた行・非有限の値は例外にせず
    読み飛ばすが、指標名を引き当てられれば 2 つ目の戻り値に入れる
    (対象の指標で 1 系列でも壊れていたら合計を信用できないので、
    fetch_one が失敗にできるようにする。名前を取れない行は諦める)。
    """
    out: Dict[str, float] = {}
    broken: Set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        matched = _LINE_RE.match(line)
        if matched is None:
            # ラベル部が閉じていないなどの行。名前だけでも拾う。
            head = _HEAD_RE.match(line)
            if head is not None:
                broken.add(head.group(1))
            continue
        name = matched.group(1)
        fields = matched.group(2).split()
        try:
            value = float(fields[0])
        except (IndexError, ValueError):
            broken.add(name)
            continue
        if not math.isfinite(value):
            broken.add(name)
            continue
        out[name] = out.get(name, 0.0) + value
    return out, broken


def metric_names(entry: Any) -> Tuple[str, str]:
    """設定 1 件ぶんの (running 指標名, waiting 指標名)。

    running_metric / waiting_metric の指定を優先し、無ければエンジンの既定。
    custom エンジンは既定が無いので、設定の検証 (config) で必須にしている。
    """
    default = ENGINE_METRICS.get(entry.engine)
    running = entry.running_metric or (default[0] if default else None)
    waiting = entry.waiting_metric or (default[1] if default else None)
    if not running or not waiting:
        raise MetricsError(METRIC_MISSING)
    return running, waiting


def locate_local_key(name: str, variable: Optional[str]) -> Optional[str]:
    """API キーを探す (無くてもよい)。キーチェーン account `local:<名前>` →
    環境変数 `variable` の順。見つからなければ None = ヘッダーを送らない。
    キーチェーンが読めなくても (ロック中など) 取得自体は止めない。"""
    try:
        stored = keychain.read("local:" + name)
    except keychain.KeychainError:
        stored = None
    if stored:
        return stored
    if variable:
        value = os.environ.get(variable)
        if value and value.strip():
            return value.strip()
    return None


def metrics_url(entry: Any) -> str:
    """base_url と metrics_path をつなげた /metrics の URL。"""
    return entry.base_url.rstrip("/") + "/" + entry.metrics_path.lstrip("/")


def _http_get_text(url: str, headers: Dict[str, str], timeout: float) -> str:
    """GET して本文を UTF-8 で返す (テストの差し替えポイント)。

    _base._open (同一 origin へのリダイレクトしか許さない opener) を使うので、
    認証ヘッダーが別ホストに渡ることはない。本文は MAX_METRICS_BYTES を
    超えたら途中で黙って打ち切るのではなく失敗にする (壊れた値を表示しないため)。
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with _base._open(request, timeout) as response:
        body = response.read(MAX_METRICS_BYTES + 1)
    if len(body) > MAX_METRICS_BYTES:
        raise MetricsError(INVALID_METRICS)
    return body.decode("utf-8")


def _sleep(seconds: float) -> None:
    """指定秒だけ待つ (テストの差し替えポイント)。"""
    time.sleep(seconds)


def _reason(exc: BaseException) -> str:
    """失敗理由の短い表示。解析系は自前の固定語、ほかは _short_reason に任せる。"""
    if isinstance(exc, MetricsError):
        return str(exc)
    if isinstance(exc, UnicodeDecodeError):
        return INVALID_METRICS     # _short_reason は "invalid JSON" になるので
    return _short_reason(exc)


def fetch_one(name: str, entry: Any, now: datetime) -> ProviderUsage:
    """ローカルサーバー 1 台の /metrics を読んで ProviderUsage を返す。

    どんな失敗でも例外を外に漏らさず error に短い理由 (秘密を含まない) を
    入れる。成功時のみ slots が載る。ローカルサーバーの空きは分単位で変わる
    ので、失敗時に前回の値を使い回す (last-good) ことはしない。

    1 回目で待ち (waiting > 0) があれば、一瞬だけの待ちか確かめるため
    QUEUE_CONFIRM_INTERVAL 秒あけて最大 QUEUE_CONFIRM_SAMPLES 回まで
    取り直し、waiting が最小の回の値を採る (同値なら後の回)。
    """
    provider = "local:" + name
    usage_name = entry.label or name
    try:
        running_metric, waiting_metric = metric_names(entry)
        headers = {"User-Agent": USER_AGENT}
        key = locate_local_key(name, entry.api_key_env)
        if key:
            # サーバー実装によってどちらかしか見ないことがあるので両方送る。
            headers["x-api-key"] = key
            headers["Authorization"] = "Bearer " + key
        url = metrics_url(entry)
        timeout = float(entry.timeout)

        def read_slots(timeout: float) -> Tuple[int, int]:
            """1 回ぶん取得して (running, waiting) を返す。失敗は例外のまま。"""
            text = _http_get_text(url, headers, timeout)
            values, broken = parse_metrics(text)
            # 対象の 2 指標だけ厳しく: 1 系列でも壊れていたら合計を信用できない
            # ので失敗。無ければもちろん失敗 (対象外の指標のゴミは無視)。
            if running_metric in broken or waiting_metric in broken:
                raise MetricsError(INVALID_METRICS)
            if running_metric not in values or waiting_metric not in values:
                raise MetricsError(METRIC_MISSING)
            return (int(round(values[running_metric])),
                    int(round(values[waiting_metric])))

        # 1 回目の失敗は外側の except で error にする。1 回目は設定の
        # timeout のまま、取り直しは待ち時間を抑えるため上限を掛ける。
        best = read_slots(timeout)
        samples = 1
        while best[1] > 0 and samples < QUEUE_CONFIRM_SAMPLES:
            try:
                _sleep(QUEUE_CONFIRM_INTERVAL)
                latest = read_slots(min(timeout, QUEUE_CONFIRM_TIMEOUT))
            except Exception:  # noqa: BLE001 - 成功した回の値を使う
                break
            samples += 1
            if latest[1] <= best[1]:   # 最小値。同値なら後の回を採る
                best = latest
            if latest[1] == 0:
                break
        slots = LocalSlots(running=best[0], waiting=best[1],
                           capacity=int(entry.capacity))
        return ProviderUsage(provider, usage_name, [], None, now, slots=slots)
    except Exception as exc:  # noqa: BLE001 - 常に usage に変換する
        return ProviderUsage(provider, usage_name, [], _reason(exc), now)


def window_entry(usage: ProviderUsage) -> Dict[str, Any]:
    """status.json の "windows" 1 要素の形にする (gate の判定材料)。

    provider は "local:<名前>" (窓 id と同じ)。成功時は running / waiting /
    capacity、失敗時は error に入れる。残量 %・リセット時刻は無い。
    """
    item: Dict[str, Any] = {
        "id": usage.provider,
        "provider": usage.provider,
        "key": usage.provider.split(":", 1)[1],
        "part_of": None,
        "kind": "local",
    }
    if usage.slots is not None:
        item["running"] = usage.slots.running
        item["waiting"] = usage.slots.waiting
        item["capacity"] = usage.slots.capacity
    else:
        item["error"] = usage.error or INVALID_METRICS
    if usage.fetched_at is not None:
        item["fetched_at"] = usage.fetched_at.astimezone().isoformat(timespec="seconds")
    return item


def fetch_all(entries: Dict[str, Any], now: datetime) -> List[ProviderUsage]:
    """設定にある有効なローカルサーバー全部を取得する (設定順)。"""
    out: List[ProviderUsage] = []
    for name, entry in entries.items():
        if not getattr(entry, "enabled", True):
            continue
        out.append(fetch_one(name, entry, now))
    return out
