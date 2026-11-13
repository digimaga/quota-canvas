"""Gemini: Antigravity CLI (agy) の /usage で Google AI Pro などの利用枠を取得する。

`agy -p "/usage" --output-format json` はモデルを呼ばず (トークン 0) 枠の残量を
1 つの JSON で返す (stdout。stderr にはログが出ることがある)。窓のバケットは
グループ名ではなく bucket の id で識別する (名前は将来変わりうる。未知の id
は無視):
- gemini-weekly / gemini-5h: Gemini 本体の週間 / 5 時間枠
- gemini-<モデル名>-wtu-weekly: Gemini 4 の各モデル (Argon / Carbon / Hydrogen ...)
  の週間枠。Carbon・Hydrogen は契約 (Ultra など) によって返らないことがある
- 3p-weekly / 3p-5h: Antigravity 経由で使える Claude / GPT の週間 / 5 時間枠
トークンの更新は agy 自身が行う (ここではリフレッシュしない)。

プラン名 (例 "Google AI Pro") だけは従来どおりキーチェーンのアクセストークン
(service "gemini" / account "antigravity") で Antigravity が使っている
`v1internal:loadCodeAssist` を叩いて取る。ここは補助なので、失敗しても
カード全体は失敗にしない (名前は "Gemini" になる)。

providers.gemini.token_refresh は過去の設定ファイルとの互換で受け付けるだけ
(トークンの更新は agy が行う)。"off" への切り替えと終了時 (atexit) には、
走っている agy を killpg で止め直す (claude / grok / codex と同じ形)。
"""

from __future__ import annotations

import atexit
import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .. import keychain
from ..platform_support import process
from ..platform_support.process import kill_group as _kill_process_group
from ..i18n import t
from ..models import ProviderUsage, Window
from ._base import (
    CredentialError, Detection, Provider, _parse_iso, finite_number,
    http_post_json,
)

log = logging.getLogger("quotacanvas.gemini")

# プラン名を取るだけの API (非公開なので形が変わりうる)。失敗しても構わない。
LOAD_CODE_ASSIST_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
KEYCHAIN_SERVICE = "gemini"
KEYCHAIN_ACCOUNT = "antigravity"
_KEYCHAIN_PREFIX = "go-keyring-base64:"
# loadCodeAssist は User-Agent が quota-canvas のままだと応答が欠けるので
# Antigravity 本体と同じ値を名乗る。
API_USER_AGENT = "antigravity"

USAGE_TIMEOUT = 30.0                 # agy /usage 1 回の上限 (秒)
_PLAN_TTL = 6 * 3600.0               # プラン名のキャッシュを取り直す間隔 (秒)

# 互換: providers.gemini.token_refresh は受け付けるが使わない (トークンの更新は
# agy 自身が行う)。既存の設定ファイルが読めなくならないよう値の検査だけする。
TOKEN_REFRESH_MODES = ("cli", "off")

# agy の場所は設定で変えられない (定数)。見つからないときだけ which("agy") を見る。
AGY_PATH = process.AGY_PATH

# (bucket id, 窓 key, ラベルの文言キー, 固定ラベル, 窓の長さ秒)。
# 文言キー (t() で解決) か固定ラベルのどちらかを使う。この並びがカードの
# 表示順で、メイン窓は weekly (最長・週間を優先する描画側の規則で自動選択)。
_BUCKETS_BEFORE_MODELS: Tuple[Tuple[str, str, Optional[str], Optional[str], int], ...] = (
    ("gemini-weekly", "weekly", "providers.window.weekly", None, 7 * 86400),
    ("gemini-5h", "5h", "providers.window.five_hours", None, 5 * 3600),
)
_BUCKETS_AFTER_MODELS: Tuple[Tuple[str, str, Optional[str], Optional[str], int], ...] = (
    ("3p-weekly", "claude", "providers.gemini.claude_window", None, 7 * 86400),
    ("3p-5h", "claude_5h", "providers.gemini.claude_5h_window", None, 5 * 3600),
)

# モデル別の週間枠 (Gemini 4 Argon / Carbon / Hydrogen ...)。bucket id は
# "gemini-<モデル名>-wtu-weekly" の形 (Argon の実応答で確認済み)。モデルが
# 増えても id が同じ形なら、コードを変えずに窓として拾えるよう、名前は決め打ちに
# しない。並びは下の既知のモデル順、未知のモデルはその後ろ (応答に出た順)。
# 窓 key = モデル名 (小文字)、ラベル = 先頭だけ大文字 ("Carbon" など)。
# カードの表示順は 週間・5 時間より後・Claude / GPT より前 (サブ窓は 3 行までなので、
# 5 時間枠を押し出さないようにする)。
_MODEL_BUCKET_RE = re.compile(r"^gemini-([a-z][a-z0-9]*)-wtu-weekly$")
_MODEL_ORDER: Tuple[str, ...] = ("argon", "carbon", "hydrogen")
# 窓 key が他の窓と衝突しないための予約 (モデル名がこれと同じなら無視する)。
_RESERVED_KEYS = frozenset(
    key for _bid, key, _m, _l, _s in _BUCKETS_BEFORE_MODELS + _BUCKETS_AFTER_MODELS)


# ------------------------------------------------------------- compat 設定

def set_token_refresh(mode: str) -> None:
    """(互換) providers.gemini.token_refresh の値の検査だけする。

    トークンの更新は agy 自身が行うため、この設定で quota-canvas 側はトークン
    を扱わない。既存の設定ファイルや cli 側の呼び出しが壊れないように、値の
    組み合わせの確認だけ残す。"off" への切り替えのときは、走っている (または
    前回止められなかった) agy がいれば killpg する。
    """
    if mode not in TOKEN_REFRESH_MODES:
        raise ValueError("token_refresh は %s のいずれか" % (TOKEN_REFRESH_MODES,))
    if mode == "off":
        _kill_running_child()


# ----------------------------------------------------------- credentials
# (プラン名の取得専用。枠の取得には使わない)

def _read_entry(now: Optional[datetime] = None) -> Tuple[str, Optional[datetime]]:
    """(access token, expiry) from the keychain item gemini/antigravity.

    項目が無い・読めない・形が違うときは CredentialError。
    """
    try:
        raw = keychain.read(KEYCHAIN_ACCOUNT, service=KEYCHAIN_SERVICE)
    except keychain.KeychainError as exc:
        raise CredentialError(str(exc)) from None
    if raw is None:
        raise CredentialError(t("providers.no_credentials"))
    if not raw.startswith(_KEYCHAIN_PREFIX):
        raise CredentialError(t("providers.malformed"))
    try:
        payload = json.loads(base64.b64decode(raw[len(_KEYCHAIN_PREFIX):]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CredentialError(t("providers.malformed")) from None
    token_obj = payload.get("token") if isinstance(payload, dict) else None
    token = token_obj.get("access_token") if isinstance(token_obj, dict) else None
    if not isinstance(token, str) or not token:
        raise CredentialError(t("providers.malformed"))
    return token, _parse_iso(token_obj.get("expiry"))


def _api_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": API_USER_AGENT,
    }


# --------------------------------------------------------------- agy 実行

def _find_agy(search_path: Optional[str] = None) -> Optional[str]:
    """agy 実行ファイルのパス。定数の場所になければ which("agy") で探す。"""
    return process.find_cli("agy", search_path, (AGY_PATH,),
                            candidates_first=True, which=shutil.which)


# 時間切れで止められなかった agy (次回起動前に止め直す)。fetch は常駐ループから
# 直列に呼ばれるので、同時に起動する agy は 1 つだけ。止められなかった子を
# 手放さず追跡することで、次の取得で 2 つ目を並走させない。
_child: Optional[subprocess.Popen] = None


def _stop_previous_child() -> bool:
    """前回の取得で止められなかった agy がまだ生きていれば、もう一度止める。

    終了を確認できたら True。生き残っていれば警告を出して False を返すので、
    呼び出し側は新しい agy を起動しない (並走させない)。
    """
    global _child
    proc = _child
    if proc is None:
        return True
    if proc.poll() is not None:
        _child = None
        return True
    if _kill_process_group(proc):
        _child = None
        return True
    log.warning("前回の agy (pid=%s) がまだ終了していない。"
                "並走させないため今回の取得を見送る", proc.pid)
    return False


def _kill_running_child() -> None:
    """起動中 (または前回止められなかった) agy がいれば killpg する。

    終了を確認できたときだけ _child を手放す。確認できないときは残して、
    atexit の次の呼び出しや、次回取得前の _stop_previous_child で再試行する。
    """
    global _child
    proc = _child
    if proc is not None and proc.poll() is None and _kill_process_group(proc):
        _child = None


atexit.register(_kill_running_child)  # 終了時にもみ消し (実行中の agy を残さない)


def _agy_usage(timeout: float = USAGE_TIMEOUT) -> Dict[str, Any]:
    """`agy -p "/usage" --output-format json` を実行し、command.data を返す。

    agy はトークンの更新を自分で行う (ここではリフレッシュしない)。子は独自の
    プロセスグループ (start_new_session) で起動し、timeout を過ぎたら
    os.killpg でグループごと SIGKILL してから時間切れのエラーにする (claude.py
    の CLI 実行と同じ形)。SIGKILL 後も終了を確認できない子は _child に残し、
    次の取得で起動前に止め直す (止められないあいだは取得を見送る)。
    stderr にはログが出ることがあるので無視する。
    agy 無し・status が SUCCESS 以外・形が違う応答は CredentialError
    (メッセージは秘密を含まない)。
    """
    global _child
    if not _stop_previous_child():  # 前回の子と並走させない
        raise CredentialError(t("providers.gemini.agy_timeout"))
    env = process.limited_env()
    agy = _find_agy(env["PATH"])
    if not agy:
        raise CredentialError(t("providers.gemini.agy_missing"))
    try:
        proc = process.start(subprocess.Popen,
            [agy, "-p", "/usage", "--output-format", "json"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),  # agy にカレントディレクトリを触らせない
            env=env,
        )
    except OSError:
        raise CredentialError(t("providers.gemini.agy_failed")) from None
    _child = proc
    try:
        stdout, _stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if _kill_process_group(proc):
            _child = None
        # 終了を確認できない子は _child に残す (次の取得の前に止め直す)
        raise CredentialError(t("providers.gemini.agy_timeout")) from None
    _child = None
    try:
        data = json.loads((stdout or b"").decode("utf-8", "replace"))
    except ValueError:
        raise CredentialError(t("providers.gemini.agy_invalid")) from None
    if not isinstance(data, dict) or data.get("status") != "SUCCESS":
        log.info("agy /usage が成功しなかった (status=%r)",
                 data.get("status") if isinstance(data, dict) else type(data).__name__)
        raise CredentialError(t("providers.gemini.agy_failed")) from None
    command = data.get("command")
    payload = command.get("data") if isinstance(command, dict) else None
    if not isinstance(payload, dict):
        raise CredentialError(t("providers.gemini.agy_invalid"))
    return payload


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """Antigravity CLI (agy) があるか。/usage は実行しない (利用枠の取得は
    fetch の 1 回に任せる)。"""
    if _find_agy() is None:
        return Detection(False, None, t("providers.gemini.agy_missing"))
    return Detection(True, t("providers.detect.gemini"))


# --------------------------------------------------------------- parser

def _tier_name(assist: Dict[str, Any], key: str) -> Optional[str]:
    """assist 応答の <key> (paidTier / currentTier) からプラン名を取る。"""
    tier = assist.get(key)
    if not isinstance(tier, dict):
        return None
    name = tier.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def parse_usage(payload: Dict[str, Any]) -> List[Window]:
    """窓のリスト (指定順) from the agy /usage command.data。

    バケットは id で識別する (グループ名・バケット名は将来変わりうる)。
    未知の id、remaining_fraction が数値でないバケットは無視する。数値でも
    NaN / inf は壊れた応答として ValueError。使用率 =
    (1 - remaining_fraction) * 100、リセット時刻 = reset_time。
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    groups = payload.get("groups") if isinstance(payload, dict) else None
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            bid = bucket.get("id")
            if isinstance(bid, str) and bid not in buckets:
                buckets[bid] = bucket

    # モデル別の週間枠 (id が gemini-<名前>-wtu-weekly の形のもの) を集める。
    models: List[Tuple[str, str]] = []  # (モデル名, bucket id)
    for bid in buckets:
        match = _MODEL_BUCKET_RE.match(bid)
        if match and match.group(1) not in _RESERVED_KEYS:
            models.append((match.group(1), bid))
    models.sort(key=lambda m: (_MODEL_ORDER.index(m[0])
                               if m[0] in _MODEL_ORDER else len(_MODEL_ORDER)))
    model_buckets = tuple((bid, name, None, name.capitalize(), 7 * 86400)
                          for name, bid in models)

    windows: List[Window] = []
    for bid, key, msg_key, literal, seconds in (
            _BUCKETS_BEFORE_MODELS + model_buckets + _BUCKETS_AFTER_MODELS):
        bucket = buckets.get(bid)
        if bucket is None:
            continue  # 未知の id ではなく、この環境では出てこないバケット
        remaining = bucket.get("remaining_fraction")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            continue
        remaining = finite_number(remaining)  # NaN / inf はクランプの前に弾く
        used = max(0.0, min(100.0, (1.0 - remaining) * 100.0))
        label = t(msg_key) if msg_key else (literal or key)
        windows.append(Window("gemini", key, label, used,
                              _parse_iso(bucket.get("reset_time")),
                              timedelta(seconds=seconds)))
    return windows


# ---------------------------------------------------------------- fetch

# プラン名のキャッシュ (成功したときだけ)。常駐ループの毎回の取得で
# loadCodeAssist を叩かないように、_PLAN_TTL (6 時間) 経過まで使い回す。
_plan_cache: Optional[str] = None
_plan_fetched_at: Optional[float] = None  # time.monotonic()。None = まだ試していない


def _plan_name(now: datetime) -> Optional[str]:
    """loadCodeAssist のプラン名 (例 "Google AI Pro")。取れなければ None。

    キーチェーンのトークンが読めない・期限切れ・HTTP エラーのいずれでも
    例外を出さず None (名前は "Gemini" にフォールバック)。枠の取得は agy の
    /usage で完結するので、ここでの失敗はカード全体の失敗にしない。
    成功した結果だけをキャッシュし、_PLAN_TTL 経過までは取り直さない
    (失敗はキャッシュしないので、次の取得で再試行する)。
    """
    global _plan_cache, _plan_fetched_at
    mono = time.monotonic()
    if _plan_cache is not None and _plan_fetched_at is not None \
            and mono - _plan_fetched_at < _PLAN_TTL:
        return _plan_cache
    try:
        token, expires = _read_entry(now)
    except CredentialError:
        return None
    if expires is not None and expires <= now:
        return None  # 名前のためだけに agy に更新を促したりはしない
    try:
        assist = http_post_json(LOAD_CODE_ASSIST_URL, _api_headers(token),
                                {"metadata": {"ideType": "ANTIGRAVITY"}})
    except Exception as exc:  # noqa: BLE001 - 名前のための通信は失敗しても構わない
        log.info("プラン名の取得 (loadCodeAssist) に失敗: %s", type(exc).__name__)
        return None
    if not isinstance(assist, dict):
        return None
    plan = _tier_name(assist, "paidTier") or _tier_name(assist, "currentTier")
    if plan:
        _plan_cache = plan
        _plan_fetched_at = mono
    return plan


def fetch(now: datetime) -> ProviderUsage:
    """agy の /usage から枠 (週間・5 時間・モデル別・Claude / GPT) を取る。プラン名の失敗は名前のフォールバックだけ。"""
    payload = _agy_usage()
    windows = parse_usage(payload)
    plan = _plan_name(now)
    name = ("Gemini " + plan) if plan else "Gemini"
    if not windows:
        # 識別できるバケットが 1 つも無い: 取得失敗として扱う
        return ProviderUsage("gemini", name, [], t("providers.gemini.no_quota"), now)
    return ProviderUsage("gemini", name, windows, None, now)


PROVIDER = Provider(
    id="gemini", name="Gemini", needs_key=False, detect=detect, fetch=fetch,
    display_name="Gemini (Antigravity)",
)
