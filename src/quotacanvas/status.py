"""status.json / frame.png の書き出し (docs/app-contract.md)。

メニューバーアプリ (app/) は、この 2 つのファイルと CLI だけを使って
エンジンとやりとりする。どちらも「一時ファイル → rename」で原子的に書く。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from .atomic import atomic_write as _atomic_write
from .platform_support.paths import data_dir
from .config import Config
from .gate import evaluate, format_line, format_model_lines
from .i18n import t
from .models import Pace, PaceStatus, ProviderUsage
from .providers import local as local_provider
from .render import _STATUS_BADGE, advice_text  # カードの advice / badge 文言は画面表示 (render) と共通

DATA_DIR = data_dir()
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


def _finite(value: Any) -> bool:
    """JSON に書ける有限の数値か (bool は数値として扱わない)。"""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _finite_percent(value: Any) -> Optional[float]:
    """丸めた使用率。NaN/inf は None (そのカード・窓を status.json から除く)。"""
    rounded = round(value, 1)
    return rounded if _finite(rounded) else None


def _card(pu: ProviderUsage, pace: Optional[Pace], now: datetime) -> Optional[Dict[str, Any]]:
    """カード 1 枚ぶんの dict。取得失敗 (pace=None) は None を返し errors に入れる。"""
    if pace is None:
        return None
    win = pace.window
    used = _finite_percent(win.used_percent)
    remaining = _finite_percent(win.remaining_percent)
    if used is None or remaining is None:
        # NaN/inf を書くと Swift 側の JSON 読み込みが丸ごと失敗する
        logging.getLogger(__name__).warning(
            "status: %s の使用率が非有限のためカードから除外します", win.id)
        return None
    item = {
        "id": win.id,
        "provider": pu.provider,
        "name": pu.name,
        "label": win.label,
        "used": used,
        "remaining": remaining,
        "status": pace.status.value,
        "status_label": t(_STATUS_LABEL[pace.status]),
        "advice": advice_text(pace, False, now),
        "resets_at": _iso(win.resets_at),
    }
    if win.reset_estimated:
        item["reset_estimated"] = True  # リセット済みとみなした窓。resets_at は推定値
    if pu.fetched_at is not None:
        # この値を実際にサービスから取得した時刻。last-good 表示中や Mac の
        # スリープ中 (取得を止めて描き直しだけ続ける) は進まない。不明なら省略。
        item["fetched_at"] = _iso(pu.fetched_at)
    if pu.stale_error:
        # 取得失敗中も最後の成功データを表示しているとき、その失敗理由。
        # 成功していればキーごと省略する。
        item["stale_error"] = pu.stale_error
    if _finite(pace.previous_used):
        # 前回の期間の最終使用率 (F3)。無ければ省略。
        item["previous_used"] = round(pace.previous_used, 1)
    if pace.exhausts_at is not None:
        # 通常の予測で尽きる時刻 (OVER のときだけ入る)。無ければ省略。
        item["exhausts_at"] = _iso(pace.exhausts_at)
    if pace.burst_exhausts_at is not None:
        # 直近 2 時間の加速が続いたときに尽きる時刻 (F2)。無ければ省略。
        item["burst_exhausts_at"] = _iso(pace.burst_exhausts_at)
    return item


def _window_item(pu: ProviderUsage, pace: Pace) -> Optional[Dict[str, Any]]:
    """status.json の "windows" 1 要素ぶんの dict。

    表示フィルタ前の全窓 (内訳窓 part_of 付きを含む)。判定に必要な項目だけを
    cards と同じ名前・意味で載せる (gate.py と app / 外部ツールが読む)。"""
    win = pace.window
    used = _finite_percent(win.used_percent)
    remaining = _finite_percent(win.remaining_percent)
    if used is None or remaining is None:
        logging.getLogger(__name__).warning(
            "status: %s の使用率が非有限のため windows から除外します", win.id)
        return None
    item: Dict[str, Any] = {
        "id": win.id,
        "provider": pu.provider,
        "key": win.key,
        "part_of": win.part_of,
        "used": used,
        "remaining": remaining,
        "status": pace.status.value,
        "resets_at": _iso(win.resets_at),
        "length": win.length.total_seconds(),
    }
    if win.reset_estimated:
        item["reset_estimated"] = True
    if (win.resets_at is not None and pace.status.value != "unknown"
            and _finite(pace.ideal_used_percent)):
        # 経過時間どおりに使ったときの使用率 (ペース線)。振り分けで boost にしてよいかの材料
        item["ideal_used"] = round(pace.ideal_used_percent, 1)
    if _finite(pace.projected_leftover) and _finite(pace.leftover_tolerance):
        item["projected_leftover"] = pace.projected_leftover
        item["leftover_tolerance"] = pace.leftover_tolerance
    if pace.exhausts_at is not None:
        item["exhausts_at"] = _iso(pace.exhausts_at)
    if pace.cautious_exhausts_at is not None:
        item["cautious_exhausts_at"] = _iso(pace.cautious_exhausts_at)
    if pu.fetched_at is not None:
        item["fetched_at"] = _iso(pu.fetched_at)
    if pu.stale_error:
        item["stale_error"] = pu.stale_error
    return item


def _spec_dict(spec: Any) -> Dict[str, Any]:
    """render.PanelSpec を {main, subs, size[, second_bar, second_window]} の dict にする。
    second_bar は設定で明示されたとき (true / false) だけ付ける (自動 = 省略)。"""
    out = {"main": spec.main, "subs": list(spec.subs), "size": spec.size}
    if getattr(spec, "second_bar", None) is not None:
        out["second_bar"] = spec.second_bar
    if getattr(spec, "second_window", None) is not None:
        out["second_window"] = spec.second_window
    return out


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
        specs = list(used)
    else:
        try:
            from .render import auto_layout
        except (ImportError, AttributeError):
            return None
        specs = auto_layout(cards)
    return _with_main_windows([_spec_dict(sp) for sp in specs], specs, cards)


def _with_main_windows(entries: List[Dict[str, Any]], specs: Sequence[Any],
                       cards: Sequence[Tuple[ProviderUsage, Optional[Pace]]]
                       ) -> List[Dict[str, Any]]:
    """main がサービス ID のパネルに、描画が実際に選んだメイン窓の ID を
    "main_window" として足す (アプリの 2 本目の窓の候補から外すため)。
    解決できなければ足さない。"""
    try:
        from .render import _lookups, _resolve_panel
    except (ImportError, AttributeError):
        return entries
    lookups = _lookups(cards)
    for entry, spec in zip(entries, specs):
        main = entry.get("main") or ""
        if not main or ":" in main:
            continue
        try:
            panel = _resolve_panel(lookups, spec)
        except Exception:  # 表示ヒントなので、解決の失敗で status を壊さない
            continue
        if panel and panel.get("main") is not None:
            entry["main_window"] = panel["main"][1].window.id
    return entries


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
    # ローカル LLM サーバーは台ごとの指定 ([providers.local.<名前>].color) を
    # "local:<名前>" キーで載せる (既定は BRAND_COLORS["local"] のシアン)。
    colors.update({"local:" + name: entry.color
                   for name, entry in cfg.providers.local.items() if entry.color})
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
                 previous: Optional[dict] = None, off: Optional[set] = None) -> Dict[str, Any]:
    """status.json 1 件ぶんの dict を作る。cards は cli.build_cards の返り値。

    layout は render に渡した PanelSpec のリスト (None = 自動配置)。
    recommendation は recommend.recommend の返り値 (None = キー省略)。
    all_windows は表示フィルタ前の全窓 (cli.all_window_paces の返り値)。
    None のときは cards から作る。burn は gate の使い切りモード
    ({ワーカー名: 期限}) で、[routing].enabled のとき status.json の
    "routing" の評価に使う。off は監視のみのワーカー名の集合。
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
    # ローカル LLM サーバー (providers.local) の並列枠も窓と同じ形で載せる
    # (残量 % は無く、running / waiting / capacity が入る)。gate の判定材料。
    local_items = [local_provider.window_entry(u) for u in usages
                   if u.provider.startswith("local:")]
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
            for item in (_window_item(pu, pace),) if item is not None
        ] + local_items,
        "balances": [dict(provider=u.provider, name=u.name, currency=b.currency,
                          amount=b.amount, label=b.label, text=b.text,
                          fetched_at=_iso(u.fetched_at),
                          stale_error=u.stale_error)
                     for u in usages for b in u.balances
                     if _finite(b.amount)],
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
            result = evaluate(payload, cfg.routing, burn or {}, now, previous=previous, off=off)
            payload["routing"] = dict(result.to_dict(), line=format_line(result, now))
            if cfg.routing.models:
                payload["routing"]["model_lines"] = format_model_lines(cfg.routing.models, result)
        except Exception as exc:  # routing の失敗で常駐 run は落とさない (キーを省くだけ)
            logging.getLogger(__name__).warning("routing: %s", exc)
    return payload


def write_status(payload: Dict[str, Any], path: Path = STATUS_PATH) -> None:
    """status.json を原子的に書く。

    NaN/Infinity は JSON の標準外で、Swift アプリの読み込みが丸ごと失敗する。
    allow_nan=False で ValueError にし、壊れたファイルは書かない。"""
    data = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n").encode("utf-8")
    _atomic_write(path, lambda handle: handle.write(data))


def write_frame(img: Any, path: Path = FRAME_PATH) -> None:
    """frame.png を原子的に書く (img は PIL Image)。"""
    _atomic_write(path, lambda handle: img.save(handle, "PNG"))
