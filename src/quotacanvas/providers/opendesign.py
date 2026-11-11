"""OpenDesign: vela CLI のログインで 5 時間・7 日の利用枠と残高を取得する。

認証の探索順: 環境変数 VELA_CONTROL_KEY（このとき apiUrl は VELA_API_URL か
既定）→ OpenDesign 公式 CLI (`vela login`) が保存する
{AMR_HOME または ~/.amr}/config.json の profiles の controlKey。
プロファイル名は環境変数 AMR_PROFILE があればそれ、無ければ "prod"、
それも無ければ profiles の唯一の要素。controlKey が無ければ CredentialError
（vela login を案内する）。apiUrl は https のみ受け付ける。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..i18n import t
from ..models import Balance, ProviderUsage, Window
from ._base import (
    USER_AGENT, CredentialError, Detection, Provider,
    _parse_iso, _window_identity, finite_number, http_get_json,
)

DEFAULT_API_URL = "https://amr-api.open-design.ai"

# vela CLI が保存する認証情報。このファイルが無い・壊れている・controlKey が
# 無いときは「キー無し」として次の探索 ( CredentialError ) へ進む。
AMR_HOME = Path.home() / ".amr"

# ----------------------------------------------------------- credentials

def _config_file() -> Path:
    """vela CLI の config.json。AMR_HOME があればそこ ("~" 始まりは展開)、
    無ければ ~/.amr。テストで AMR_HOME を差し替えれば実ログインを読まない。"""
    home = os.environ.get("AMR_HOME")
    if home and home.strip():
        text = home.strip()
        if text.startswith("~"):
            text = os.path.expanduser(text)
        return Path(text) / "config.json"
    return AMR_HOME / "config.json"


def _api_url(value: Any) -> str:
    """apiUrl を検証して正規化する。値が無い (None・空) ときだけ既定 URL に
    戻す。https 以外・壊れた値は CredentialError (controlKey を平文や
    別ホストへ送らないため)。ポート・パスは保持し、末尾の / は除く。"""
    if value is None:
        return DEFAULT_API_URL
    if not isinstance(value, str):
        raise CredentialError(t("providers.opendesign_bad_api_url"))
    text = value.strip()
    if not text:
        return DEFAULT_API_URL
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        raise CredentialError(
            t("providers.opendesign_bad_api_url")) from None
    if (parts.scheme != "https" or not parts.hostname
            or parts.username is not None or parts.password is not None):
        raise CredentialError(t("providers.opendesign_bad_api_url"))
    try:
        parts.port  # 壊れたポート (https://host:abc) はここで ValueError
    except ValueError:
        raise CredentialError(
            t("providers.opendesign_bad_api_url")) from None
    # netloc をそのまま使う (IPv6 ホストを壊さない)。クエリ・fragment は捨てる
    return urllib.parse.urlunsplit(
        ("https", parts.netloc, parts.path, "", "")).rstrip("/")


def _profile(data: Any) -> Optional[Dict[str, Any]]:
    """config.json の profiles から使うプロファイルを選ぶ。

    AMR_PROFILE があればその名前、無ければ "prod"、それも無ければ profiles の
    唯一の要素。選べなければ None。"""
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        return None
    name = os.environ.get("AMR_PROFILE")
    if name and name.strip():
        entry = profiles.get(name.strip())
    elif "prod" in profiles:
        entry = profiles["prod"]
    elif len(profiles) == 1:
        entry = next(iter(profiles.values()))
    else:
        return None
    return entry if isinstance(entry, dict) else None


def _credentials_from_config_file() -> Optional[Tuple[str, str]]:
    """config.json から (controlKey, apiUrl)。読めなければ None。"""
    try:
        data = json.loads(_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    profile = _profile(data)
    if not isinstance(profile, dict):
        return None
    key = profile.get("controlKey")
    if not isinstance(key, str) or not key.strip():
        return None
    return key.strip(), _api_url(profile.get("apiUrl"))


def locate_opendesign_credentials() -> Tuple[str, str, str]:
    """(controlKey, apiUrl, 取得元の説明)。どこにも無ければ CredentialError。"""
    env_key = os.environ.get("VELA_CONTROL_KEY")
    if env_key and env_key.strip():
        return (
            env_key.strip(),
            _api_url(os.environ.get("VELA_API_URL")),
            t("providers.detect.env", variable="VELA_CONTROL_KEY"),
        )
    file_creds = _credentials_from_config_file()
    if file_creds:
        key, api_url = file_creds
        return key, api_url, t(
            "providers.detect.opendesign_config", path=str(_config_file()))
    raise CredentialError(t("providers.opendesign_login_required"))


# ------------------------------------------------------------- detection

def detect() -> Detection:
    """vela CLI のログイン (controlKey) があるか。通信しない。"""
    try:
        _, _, source = locate_opendesign_credentials()
    except CredentialError as exc:
        return Detection(False, None, str(exc) or
                         t("providers.opendesign_login_required"))
    return Detection(True, source)


# --------------------------------------------------------------- parser

# 応答の designPlanWindows[].kind → 窓の長さ (秒)。未知の kind は無視する。
_OPENDESIGN_WINDOWS = {
    "5h": 5 * 3600,
    "7d": 7 * 86400,
}


def parse_opendesign(data: Any, now: Optional[datetime] = None) \
        -> Tuple[str, List[Window], List[Balance]]:
    """(name, windows, balances) from the billing snapshot response.

    使用率 = (limitCredits - remainingCredits) / limitCredits * 100 を
    0〜100 に丸める。数値化できない・limit が 0 以下の窓はその窓だけ
    飛ばし、使える窓が 1 つも無いときだけ ValueError。同じ kind が重なったら
    最初の 1 つだけ採る。wallet.balanceUsd があれば残高 (USD) として出す
    (数値化できないときは残高だけ落とす)。plan.tier があればプラン名に使う
    ("go" → "Go"、"pro_plus" → "Pro Plus")。"""
    if not isinstance(data, dict):
        raise ValueError("invalid response")
    entries = data.get("designPlanWindows")
    windows: List[Window] = []
    seen: set = set()
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            seconds = _OPENDESIGN_WINDOWS.get(kind)
            if seconds is None or kind in seen:
                continue  # 未知の kind と、同じ kind の 2 個目以降は無視
            try:
                # クレジット数は文字列で来る
                limit = finite_number(entry.get("limitCredits"))
                remaining = finite_number(entry.get("remainingCredits"))
            except ValueError:
                continue  # 数値化できない窓はその窓だけ飛ばす
            if limit <= 0:
                continue
            seen.add(kind)
            value = min(100.0, max(0.0, (limit - remaining) / limit * 100.0))
            key, label, length = _window_identity(seconds)
            windows.append(Window(
                "opendesign", key, label, value,
                _parse_iso(entry.get("resetsAt")), length,
            ))
    if not windows:
        raise ValueError("no usage windows")

    balances: List[Balance] = []
    wallet = data.get("wallet")
    if isinstance(wallet, dict) and "balanceUsd" in wallet:
        try:
            amount = finite_number(wallet["balanceUsd"])
        except ValueError:
            pass  # 残高だけ落とす (窓はそのまま返す)
        else:
            balances.append(Balance("USD", amount))

    plan = data.get("plan")
    tier = plan.get("tier") if isinstance(plan, dict) else None
    name = "OpenDesign"
    if isinstance(tier, str) and tier.strip():
        words = tier.strip().replace("_", " ").replace("-", " ").split()
        if words:
            name = "OpenDesign " + " ".join(
                w[:1].upper() + w[1:] for w in words)
    return name, windows, balances


# ---------------------------------------------------------------- fetch

def _workspace_id(data: Any) -> str:
    """workspaces/current の応答から workspaceId を取り出す。"""
    if not isinstance(data, dict):
        raise ValueError("invalid response")
    workspace_id = data.get("workspaceId")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("invalid workspace")
    return workspace_id.strip()


def fetch(now: datetime) -> ProviderUsage:
    key, api_url, _source = locate_opendesign_credentials()
    headers = {
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,  # Python-urllib の既定 UA は Cloudflare に 403 される
    }
    try:
        current = http_get_json(api_url + "/api/v1/workspaces/current", headers)
        workspace_id = _workspace_id(current)
        snapshot_url = "%s/api/v1/workspaces/%s/billing/snapshot" % (
            api_url, urllib.parse.quote(workspace_id, safe=""))
        data = http_get_json(snapshot_url, headers)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CredentialError(
                t("providers.opendesign_login_expired")) from None
        raise
    name, windows, balances = parse_opendesign(data, now)
    return ProviderUsage("opendesign", name, windows, None, now,
                         balances=balances)


PROVIDER = Provider(
    id="opendesign", name="OpenDesign", needs_key=False, detect=detect,
    fetch=fetch,
)
