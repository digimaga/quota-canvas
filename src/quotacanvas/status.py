"""status.json / frame.png の書き出し (docs/app-contract.md)。

メニューバーアプリ (app/) は、この 2 つのファイルと CLI だけを使って
エンジンとやりとりする。どちらも「一時ファイル → rename」で原子的に書く。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from .atomic import atomic_write as _atomic_write
from .config import Config
from .gate import evaluate, format_line
from .i18n import t
from .models import Pace, PaceStatus, ProviderUsage
from .render import _STATUS_BADGE, advice_text  # カードの advice / badge 文言は画面表示 (render) と共通

DATA_DIR = Path.home() / "Library" / "Application Support" / "quota-canvas"
STATUS_PATH = DATA_DIR / "status.json"
FRAME_PATH = DATA_DIR / "frame.png"

# render.py と同じ文言キー (render.badge.*)。文言は出力時に t() で解決する
_STATUS_LABEL = dict(_STATUS_BADGE)
# render 側がまだ render.badge.fresh を持たない場合に備えたフォールバック (F5)
_STATUS_LABEL.setdefault(PaceStatus.FRESH, "render.badge.fresh")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO 8601 (オフセット付きローカル時刻)。無ければ None。"""
    if dt is None:
        return None
    return dt.astimezone().isoformat(timespec="seconds")


def _card(pu: ProviderUsage, pace: Optional[Pace], now: datetime) -> Optional[Dict[str, Any]]:
    """カード 1 枚ぶんの dict。取得失敗 (pace=None) は None を返し errors に入れる。"""
    if pace is None:
        return None
    win = pace.window
    item = {
        "id": win.id,
        "provider": pu.provider,
        "name": pu.name,
        "label": win.label,
        "used": round(win.used_percent, 1),
        "remaining": round(win.remaining_percent, 1),
        "status": pace.status.value,
        "status_label": t(_STATUS_LABEL[pace.status]),
        "advice": advice_text(pace, False, now),
        "resets_at": _iso(win.resets_at),
    }
    if pu.fetched_at is not None:
        # この値を実際にサービスから取得した時刻。last-good 表示中や Mac の
        # スリープ中 (取得を止めて描き直しだけ続ける) は進まない。不明なら省略。
        item["fetched_at"] = _iso(pu.fetched_at)
    if pu.stale_error:
        # 取得失敗中も最後の成功データを表示しているとき、その失敗理由。
        # 成功していればキーごと省略する。
        item["stale_error"] = pu.stale_error
    if pace.previous_used is not None:
        # 前回の期間の最終使用率 (F3)。無ければ省略。
        item["previous_used"] = round(pace.previous_used, 1)
    if pace.exhausts_at is not None:
        # 通常の予測で尽きる時刻 (OVER のときだけ入る)。無ければ省略。
        item["exhausts_at"] = _iso(pace.exhausts_at)
    if pace.burst_exhausts_at is not None:
        # 直近 2 時間の加速が続いたときに尽きる時刻 (F2)。無ければ省略。
        item["burst_exhausts_at"] = _iso(pace.burst_exhausts_at)
    return item


def _window_item(pu: ProviderUsage, pace: Pace) -> Dict[str, Any]:
    """status.json の "windows" 1 要素ぶんの dict。

    表示フィルタ前の全窓 (内訳窓 part_of 付きを含む)。判定に必要な項目だけを
    cards と同じ名前・意味で載せる (gate.py と app / 外部ツールが読む)。"""
    win = pace.window
    item: Dict[str, Any] = {
        "id": win.id,
        "provider": pu.provider,
        "key": win.key,
        "part_of": win.part_of,
        "used": round(win.used_percent, 1),
        "remaining": round(win.remaining_percent, 1),
        "status": pace.status.value,
        "resets_at": _iso(win.resets_at),
        "length": win.length.total_seconds(),
    }
    if pace.exhausts_at is not None:
        item["exhausts_at"] = _iso(pace.exhausts_at)
    if pu.fetched_at is not None:
        item["fetched_at"] = _iso(pu.fetched_at)
    if pu.stale_error:
        item["stale_error"] = pu.stale_error
    return item


def _spec_dict(spec: Any) -> Dict[str, Any]:
    """render.PanelSpec を {main, subs, size} の dict にする。"""
    return {"main": spec.main, "subs": list(spec.subs), "size": spec.size}


def _reset_credits_payload(usages: Sequence[ProviderUsage]) -> Optional[Dict[str, List[Dict[str, Optional[str]]]]]:
    """status.json の "reset_credits": {provider: [{title, granted_at, expires_at}]}。

    日時は ISO 8601 (オフセット付き)。1 つも権利が無ければ None (キー省略)。
    """
    out: Dict[str, List[Dict[str, Optional[str]]]] = {}
    for u in usages:
        credits = getattr(u, "reset_credits", None) or []
        if not credits:
            continue
        out[u.provider] = [
            {"title": c.title,
             "granted_at": _iso(c.granted_at),
             "expires_at": _iso(c.expires_at)}
            for c in credits
        ]
    return out or None


def _layout_payload(cfg: Config, cards: Sequence[Tuple[ProviderUsage, Optional[Pace]]],
                    used: Optional[Sequence[Any]]) -> Optional[List[Dict[str, Any]]]:
    """status.json の "layout": 実際に使われた配置 ({main, subs, size} の配列)。

    used は render に渡した PanelSpec のリスト (None = 自動配置)。自動配置の
    ときは render.auto_layout(cards) の結果を使い、render 側がまだ
    auto_layout を持たなければ省略 (None)。
    """
    if used is not None:
        return [_spec_dict(sp) for sp in used]
    try:
        from .render import auto_layout
    except (ImportError, AttributeError):
        return None
    return [_spec_dict(sp) for sp in auto_layout(cards)]


def _colors_payload(cfg: Config) -> Optional[Dict[str, str]]:
    """status.json の "colors": 既定色込みの有効な色 {provider: "#RRGGBB"}。

    render.BRAND_COLORS を config の providers.<p>.color で上書きする。
    render 側がまだ BRAND_COLORS を持たなければ省略 (None)。
    """
    try:
        from .render import BRAND_COLORS
    except (ImportError, AttributeError):
        return None
    colors = dict(BRAND_COLORS)
    colors.update({name: c for name, c in cfg.providers.colors.items() if c})
    return colors


def _recommendation_payload(recommendation: Any) -> Optional[Dict[str, Any]]:
    """status.json の "recommendation"。recommend.Recommendation か、
    すでに payload 形の dict を受ける。無ければ None (キーごと省略)。"""
    if recommendation is None:
        return None
    if isinstance(recommendation, dict):
        return recommendation
    return recommendation.payload()


def build_status(cfg: Config, cards: Sequence[Tuple[ProviderUsage, Optional[Pace]]],
                 usages: Sequence[ProviderUsage], now: datetime,
                 layout: Optional[Sequence[Any]] = None,
                 recommendation: Optional[Any] = None,
                 all_windows: Optional[Sequence[Tuple[ProviderUsage, Optional[Pace]]]] = None,
                 burn: Optional[Dict[str, datetime]] = None,
                 previous: Optional[dict] = None) -> Dict[str, Any]:
    """status.json 1 件ぶんの dict を作る。cards は cli.build_cards の返り値。

    layout は render に渡した PanelSpec のリスト (None = 自動配置)。
    recommendation は recommend.recommend の返り値 (None = キー省略)。
    all_windows は表示フィルタ前の全窓 (cli.all_window_paces の返り値)。
    None のときは cards から作る。burn は gate の使い切りモード
    ({ワーカー名: 期限}) で、[routing].enabled のとき status.json の
    "routing" の評価に使う。
    """
    card_items = []
    for pu, pace in cards:
        if pace is not None and pace.window.part_of is not None:
            # 内訳窓 (part_of 付き) は親カードの一部なので、メニューバー
            # アプリが別カードとして表示しないよう cards から除く
            continue
        item = _card(pu, pace, now)
        if item is not None:
            card_items.append(item)
    payload = {
        "updated_at": _iso(now),
        "paused": bool(cfg.display.paused),
        "gauge": cfg.display.gauge,
        "cards": card_items,
        # 表示フィルタ前の全窓 (内訳窓を含む)。routing 判定や外部ツールの材料。
        # [routing].enabled に関係なく常に書く。
        "windows": [
            item for pu, pace in (cards if all_windows is None else all_windows)
            if pace is not None
            for item in (_window_item(pu, pace),)
        ],
        "errors": [
            {"provider": u.provider, "name": u.name, "error": u.error}
            for u in usages if u.error
        ],
    }
    layout_entries = _layout_payload(cfg, cards, layout)
    if layout_entries is not None:
        payload["layout"] = layout_entries
    colors = _colors_payload(cfg)
    if colors is not None:
        payload["colors"] = colors
    reset_credits = _reset_credits_payload(usages)
    if reset_credits is not None:
        payload["reset_credits"] = reset_credits
    rec_payload = _recommendation_payload(recommendation)
    if rec_payload is not None:
        payload["recommendation"] = rec_payload
    if cfg.routing.enabled:
        # アプリ用に判定結果も添える。CLI (routing line|check) はこの値を
        # 信用せず、読むたびに windows から evaluate し直す (burn の期限切れ・
        # 設定変更を即反映するため)。"line" は routing line と同じ 1 行で、
        # quota-canvas を入れていない Mac がコピーをそのまま表示するのに使う。
        try:
            result = evaluate(payload, cfg.routing, burn or {}, now, previous=previous)
            payload["routing"] = dict(result.to_dict(), line=format_line(result, now))
        except Exception as exc:  # routing の失敗で常駐 run は落とさない (キーを省くだけ)
            logging.getLogger(__name__).warning("routing: %s", exc)
    return payload


def write_status(payload: Dict[str, Any], path: Path = STATUS_PATH) -> None:
    """status.json を原子的に書く。"""
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(path, lambda handle: handle.write(data))


def write_frame(img: Any, path: Path = FRAME_PATH) -> None:
    """frame.png を原子的に書く (img は PIL Image)。"""
    _atomic_write(path, lambda handle: img.save(handle, "PNG"))
