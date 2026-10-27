"""`quota-canvas once|run|config|detect|set-key|refresh|pick`: fetch usage, compute pacing, draw it on the TURZX screen."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import tomllib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from PIL import Image

from . import gate, keychain, macpower, providers
from .i18n import LANGUAGE_CHOICES, current_language, set_language, t, translations
from .config import (
    DEFAULT_PATH, PROVIDERS, Sleep, Config, ConfigError, brightness_for, config_from_dict,
    config_to_dict, format_toml, load_config, save_config, sleep_for, wants_bedtime,
)
from .history import History, _parse_iso
from .lastgood import LastGood
from .models import Pace, ProviderUsage
from . import refresh as refresh_mod
from .refresh import REFRESH_MIN_GAP, request_refresh, wait_for_wakeup
from .pacing import compute_pace
from .recommend import recommend
from .render import default_separator, render, render_error
from .status import STATUS_PATH, build_status, write_frame, write_status

log = logging.getLogger("quotacanvas")

# 取得に失敗している間は、経過時間に関係なく最後に取得できたデータ
# (last-good.json に永続化、エンジン再起動後も有効) を表示し続け、カードに
# 「取得失敗中」と出す (ProviderUsage.stale_error)。last_good が無いときだけ
# エラーパネルになる。
# After an HTTP 429, leave that provider alone for a while: the server's
# Retry-After clamped to [BACKOFF_MIN, BACKOFF_MAX], or doubling
# 5 -> 10 -> 20 -> 40 -> 60 minutes (capped) when it sends no header.
BACKOFF_MIN = timedelta(minutes=2)
BACKOFF_MAX = timedelta(hours=1)
BACKOFF_BASE = timedelta(minutes=5)
# Burn-in protection: shift the whole picture by a few pixels every few minutes.
SHIFT_EVERY = timedelta(minutes=3)
SHIFTS = [(0, 0), (4, 0), (4, 4), (0, 4), (-4, 4), (-4, 0), (-4, -4), (0, -4), (4, -4)]

PROVIDER_ORDER = {"claude": 0, "codex": 1, "glm": 2, "grok": 3, "gemini": 4}

Card = Tuple[ProviderUsage, Optional[Pace]]


def _window_pace(window, usage: ProviderUsage, history: History, now: datetime,
                 tight: float, on_track: float, sleep: Optional[Sleep]) -> Pace:
    """窓 1 枚ぶんのペース判定 (build_cards と status.json の windows で共有)。"""
    pace = compute_pace(window, history.samples(window.id), now, tight=tight, on_track=on_track,
                        sleep=sleep_for(sleep, usage.provider),
                        bedtime=wants_bedtime(sleep, usage.provider))
    periods = getattr(history, "periods", None)
    if periods is not None:
        prev_used = periods.previous_used(window.id)
        if prev_used is not None:
            # 前回の期間の最終使用率 (F3)。「前回 36% 余り」表示用
            pace.previous_used = prev_used
    return pace


def build_cards(usages: List[ProviderUsage], history: History, now: datetime,
                order: Optional[List[str]] = None,
                tight: float = 92.0, on_track: float = 70.0,
                provider_windows: Optional[Dict[str, Set[str]]] = None,
                sleep: Optional[Sleep] = None) -> List[Card]:
    """`order` (config display.windows / --only) があれば、その窓 ID の順番どおりに
    並べ、無い窓は出さない。無ければ長い窓を先に（大きいカード）、次に provider 順。
    `provider_windows` (config providers.<p>.windows) があれば、各サービスその窓
    key だけを表示する (空集合 = 全部)。両方あれば両方を満たす窓だけ出す。
    `sleep` (config pacing.sleep) があれば、skip に入っていないサービスの
    ペース判定を「起きている時間」で行う。advice = "bedtime" なら、寝る時刻が
    締切のときに備えて Pace.bedtime_at も載せる。
    内訳窓 (part_of 付き) は自分の key/id ではなく「親窓が残るかどうか」で
    残り、並び順も親窓の直後に合わせる。"""
    pos = {wid: i for i, wid in enumerate(order)} if order else None
    cards: List[Tuple[tuple, Card]] = []
    for u in usages:
        if u.error and not u.windows:
            if u.error in translations("providers.no_credentials"):
                continue  # the service isn't set up on this Mac; nothing to warn about
            cards.append(((1, PROVIDER_ORDER.get(u.provider, 9), ""), (u, None)))
            continue
        allowed = provider_windows.get(u.provider) if provider_windows else None
        for w in u.windows:
            parent = None
            if w.part_of is not None:
                # 親窓 (同じサービスの独立した窓で key が一致するもの)
                parent = next((p for p in u.windows
                               if p.part_of is None and p.key == w.part_of), None)
                if parent is None:
                    continue  # 親が無い内訳は出せない
            sort_w = parent if parent is not None else w
            if allowed and sort_w.key not in allowed:
                continue  # providers.<p>.windows のフィルタ (表示時に適用)
            if pos is not None and sort_w.id not in pos:
                continue
            pace = _window_pace(w, u, history, now, tight, on_track, sleep)
            after = 0 if parent is None else 1  # 内訳は親の直後
            if pos is not None:
                key = (0, pos[sort_w.id], after)
            else:
                long_first = 0 if sort_w.length >= timedelta(days=1) else 1
                key = (long_first, PROVIDER_ORDER.get(u.provider, 9), sort_w.key, after)
            cards.append((key, (u, pace)))
    cards.sort(key=lambda c: c[0])
    return [c for _, c in cards]


def all_window_paces(usages: List[ProviderUsage], history: History, now: datetime,
                     tight: float = 92.0, on_track: float = 70.0,
                     sleep: Optional[Sleep] = None) -> List[Card]:
    """表示フィルタ (display.windows / --only / providers.<p>.windows) をかけずに、
    取得できた全 provider の全窓 (内訳窓 part_of 付きを含む) のペース判定を返す。
    status.json のトップレベル "windows" の材料 (gate の判定はこれを読む)。"""
    return [(u, _window_pace(w, u, history, now, tight, on_track, sleep))
            for u in usages for w in u.windows]


class Poller:
    def __init__(self, history: History, enabled: Optional[Set[str]] = None,
                 last_good_path: Optional[Path] = None, record: bool = True):
        self.history = history
        self.enabled = enabled  # None なら全部、集合ならその provider だけ取得
        # record=False で読み取り専用にする (last-good.json / history.jsonl /
        # periods.jsonl に書かない。`pick` など、走行中の run と書き込みを
        # 重ねたくない 1 回きりの計算用)。
        self.record = record
        self.last_good: Dict[str, ProviderUsage] = {}
        # 最後に取得できた利用量を last-good.json に永続化する (規定は history
        # と同じディレクトリ)。再起動後も経過時間に関係なく表示に使う。
        path = Path(last_good_path) if last_good_path is not None \
            else history.path.with_name("last-good.json")
        self.store = LastGood(path)
        self.last_good.update(self.store.data)
        # HTTP 429 バックオフ: provider ごとの次回取得可能時刻と連続 429 回数、
        # 代わりに表示する直近の 429 エラー。
        self._next_attempt: Dict[str, datetime] = {}
        self._rate_limited: Dict[str, int] = {}
        self._last_429: Dict[str, ProviderUsage] = {}

    # ------------------------------------------------------- 429 backoff

    def _backing_off(self, pid: str, now: datetime) -> bool:
        until = self._next_attempt.get(pid)
        return until is not None and now < until

    def _clear_backoff(self, pid: str) -> None:
        self._next_attempt.pop(pid, None)
        self._rate_limited.pop(pid, None)
        self._last_429.pop(pid, None)

    def _start_backoff(self, usage: ProviderUsage, now: datetime) -> None:
        """429 を受けたら Retry-After (または倍々) で次回取得を延期する。"""
        pid = usage.provider
        count = self._rate_limited.get(pid, 0) + 1
        self._rate_limited[pid] = count
        max_seconds = BACKOFF_MAX.total_seconds()
        if usage.retry_after is not None and usage.retry_after > 0:
            delay_seconds = max(BACKOFF_MIN.total_seconds(),
                                min(usage.retry_after, max_seconds))
        else:
            delay_seconds = BACKOFF_BASE.total_seconds()
            remaining = count - 1
            while remaining and delay_seconds < max_seconds:
                delay_seconds = min(delay_seconds * 2, max_seconds)
                remaining -= 1
        delay = timedelta(seconds=delay_seconds)
        self._next_attempt[pid] = now + delay
        self._last_429[pid] = usage
        minutes = max(1, round(delay.total_seconds() / 60))
        log.warning("%s: HTTP 429、%d 分後に再取得します", pid, minutes)

    def _deferred_usage(self, pid: str, now: datetime) -> ProviderUsage:
        """バックオフ中の provider の代役: last_good があれば経過時間に関係なく
        それ (失敗中であることを stale_error に入れて返す。本体は汚さない)、
        無ければ直近の 429 エラー。"""
        good = self.last_good.get(pid)
        if good is not None:
            return replace(good, stale_error="HTTP 429")
        err = self._last_429.get(pid)
        if err is not None:
            return err
        return ProviderUsage(pid, providers.REGISTRY[pid].name, [], "HTTP 429", now)

    # -------------------------------------------------------------- fetch

    def fetch(self, now: datetime) -> List[ProviderUsage]:
        fetch_enabled: Optional[Set[str]] = \
            set(self.enabled) if self.enabled is not None else None
        # fetch_all と同じ順序 (REGISTRY 順) を保つため、枠を先に計算する。
        order = [pid for pid in providers.REGISTRY
                 if fetch_enabled is None or pid in fetch_enabled]
        deferred: Dict[str, ProviderUsage] = {}
        active: Set[str] = set()
        for pid in order:
            if self._backing_off(pid, now):
                deferred[pid] = self._deferred_usage(pid, now)
                log.debug("%s: HTTP 429 のバックオフ中のため取得をスキップ", pid)
            else:
                active.add(pid)
        if deferred:
            # バックオフ中のサービスは取りに行かない (enabled を絞る)。
            fetch_enabled = active
        fetched = providers.fetch_all(now, enabled=fetch_enabled)
        by_pid = {u.provider: u for u in fetched}
        result: List[ProviderUsage] = []
        for pid in order:
            if pid in deferred:
                result.append(deferred[pid])
                continue
            u = by_pid.get(pid)
            if u is None:
                continue
            if u.error:
                if u.error == "HTTP 429":
                    self._start_backoff(u, now)  # 警告は 1 回だけこちらで出す
                else:
                    self._clear_backoff(pid)
                    log.warning("%s: %s", u.provider, u.error)
                prev = self.last_good.get(u.provider)
                if prev is not None:
                    # 失敗中も最後の成功データを表示し続ける (経過時間は問わない)。
                    # 表示用に失敗理由だけ添える (last_good 本体・last-good.json
                    # は stale_error を含まないまま)。
                    u = replace(prev, stale_error=u.error)
            else:
                self._clear_backoff(pid)
                self.last_good[u.provider] = u
                if self.record:
                    self.store.set(u)
                    # 内訳窓 (part_of 付き) は親窓の表示の一部なので履歴は取らない
                    self.history.record([w for w in u.windows if w.part_of is None],
                                        now)
            result.append(u)
        return result


def _open_screen():
    from turzx import Screen
    try:
        return Screen()
    except RuntimeError as exc:
        log.warning("%s", exc)
        return None


def _send(img, screen) -> bool:
    try:
        if screen.show(img):
            return True
        log.warning("screen did not accept the frame")
    except Exception as exc:  # USB unplugged mid-run, etc.
        log.warning("screen error: %s", exc)
    return False


def _output(img, screen, out: Optional[Path]) -> object:
    """フレームを送り、使い続ける screen を返す (使えなければ None)。

    長いスリープのあとなどで USB 機器がつなぎ直されると、古いハンドルは
    ENODEV で失敗し続ける (turingscreencli は例外を出さず False を返す)。
    送れなかったら機器を開き直して 1 回だけ送り直す。新しく開いた screen を
    返すので、呼び出し側は screen の入れ替わりを見て明るさを設定し直すこと。
    """
    if out:
        img.save(out)
    if screen is None or _send(img, screen):
        return screen
    try:
        import usb.util
        usb.util.dispose_resources(screen.dev)
    except Exception:
        pass
    screen = _open_screen()
    if screen is None:
        return None
    log.info("画面を開き直しました")
    return screen if _send(img, screen) else None


def _write_app_files(cfg: Config, cards: List[Card], usages: List[ProviderUsage],
                     now: datetime, img, layout=None, recommendation=None,
                     all_windows: Optional[List[Card]] = None) -> None:
    """status.json と frame.png を書く (app 連携用、docs/app-contract.md)。
    書けなくても run は止めない。layout は render に渡した配置 (None = 自動配置)、
    recommendation は recommend.recommend の返り値 (None = 推奨なし)、
    all_windows は表示フィルタ前の全窓 (status.json の "windows" の材料)。"""
    burn = None
    previous = None
    if cfg.routing.enabled:
        try:
            burn = gate.load_burn(gate.BURN_PATH, now)
        except Exception as exc:  # routing の失敗で常駐 run は落とさない
            log.warning("routing: %s", exc)
            burn = None
        try:
            with STATUS_PATH.open(encoding="utf-8") as handle:
                old_status = json.load(handle)
            if isinstance(old_status, dict):
                previous = old_status.get("routing")
        except (OSError, ValueError):
            pass  # 初回・読めない既存ファイルは前回状態なし
    try:
        write_status(build_status(cfg, cards, usages, now, layout=layout,
                                  recommendation=recommendation, all_windows=all_windows,
                                  burn=burn, previous=previous))
    except OSError as exc:
        log.warning("status.json を書けません: %s", exc)
    if img is not None:
        try:
            write_frame(img)
        except OSError as exc:
            log.warning("frame.png を書けません: %s", exc)


def _render_frame(cards: List[Card], now: datetime, cfg: Config, layout,
                  recommendation: Optional[str] = None, offset=(0, 0)):
    """1 フレームを描く。recommendation はヘッダに描く推奨 1 行 (F1)。"""
    if not cards:
        return render_error(t("cli.no_usage"), now)
    return render(cards, now, offset=offset, gauge=cfg.display.gauge,
                  colors=_brand_colors(cfg), layout=layout,
                  recommendation=recommendation,
                  percent_colors=cfg.display.percent_colors,
                  separator=cfg.display.separator or default_separator())


def _render_layout(cfg: Config, cards: Optional[List[Card]] = None):
    """config の layout.panels を render.PanelSpec のリストに変換して返す。

    パネル未指定 (既定) なら None = render 側の自動配置。render 側がまだ
    PanelSpec を用意していない場合は None で描画する互換処理。
    subs を省略した large パネルには、自動配置 (render.auto_layout) と同じく
    同じサービスの表示中の窓を順に最大 3 件付ける。全パネルの main と明示
    subs の窓、先行パネルが自動サブに使った窓は重複しない。small パネルは
    描画側に合わせてサブなし。main = "" の small パネル (空き枠) はそのまま
    PanelSpec(main="", subs=[], size="small") になり、status.json でも同じ
    形で報告されて列構成を復元できる。
    """
    if not cfg.layout.panels:
        return None
    try:
        from .render import PanelSpec
    except (ImportError, AttributeError):
        log.warning("render に PanelSpec がないため、配置指定を無視して自動配置で描画します")
        return None
    used: Set[str] = set()  # main と明示 subs は自動サブの対象から外す
    for p in cfg.layout.panels:
        used.add(p.main)
        if p.subs is not None:
            used.update(p.subs)
    shown = [pace.window.id for _, pace in (cards or []) if pace is not None]
    specs = []
    for p in cfg.layout.panels:
        if not p.main:  # 空き枠 (small のみ可、config 検証済み)
            specs.append(PanelSpec(main="", subs=[], size=p.size))
            continue
        if p.subs is not None:
            subs = list(p.subs)[:3]  # 描画側も 3 件で切るので status.json もそろえる
        elif ":" not in p.main or p.size == "small":
            subs = []  # main がサービス全体 (エラーパネル) か small: 自動配置と同様サブなし
        else:
            provider = p.main.split(":", 1)[0]
            subs = []
            for wid in shown:
                if len(subs) >= 3:
                    break
                if wid.split(":", 1)[0] == provider and wid not in used:
                    subs.append(wid)
            subs.sort(key=lambda wid: not wid.endswith(":5h"))  # 5 時間枠を先頭に
            used.update(subs)
        specs.append(PanelSpec(main=p.main, subs=subs, size=p.size))
    return specs


def _brand_colors(cfg: Config) -> Dict[str, str]:
    """render に渡す色の上書き {provider: "#RRGGBB"}。指定のあるものだけ。"""
    return {name: color for name, color in cfg.providers.colors.items() if color}


# ------------------------------------------------------------ config helpers

def _refresh_config(cfg: Config) -> Tuple[Config, bool, str]:
    """run ループ用: 設定ファイルの更新時刻が変わっていたら読み直す。
    読み直しでエラーが出たら、前の設定のまま警告ログ。戻り値は
    (設定, 読み直したか, 表示言語)。言語が前回と違うときは呼び出し側が
    取得済みの文言 (窓ラベル・エラー文) を取り直す。"""
    if not cfg.is_stale():
        return cfg, False, current_language()
    try:
        new = load_config(cfg.path)
    except ConfigError as exc:
        log.warning("設定を再読み込みできなかったため、前の設定を使い続けます: %s", exc)
        cfg.mark_checked()
        return cfg, False, current_language()
    lang = set_language(new.display.language)  # 表示言語も設定に追従させる
    return new, True, lang


def _apply_brightness(screen, cfg: Config, applied: Optional[int]) -> Optional[int]:
    """必要なら Screen.brightness() を呼ぶ。戻り値は適用済みの明るさ。"""
    desired = brightness_for(cfg)
    if screen is None or desired == applied:
        return applied
    try:
        screen.brightness(desired)
    except Exception as exc:  # USB unplugged, etc.
        log.warning("明るさを設定できません: %s", exc)
        return applied
    log.info("画面の明るさを %d に設定しました", desired)
    return desired


class _ScreenGuard:
    """画面操作 (フレーム送信 / 明るさ) を run ループとシステムスリープ監視
    スレッド (macpower の on_sleep / on_wake) で直列化するロックと、
    スリープ消灯の状態。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.dimmed = False         # 黒フレーム + 明るさ 0 を送り済みか
        self.system_asleep = False  # システムスリープ報告中 (on_wake で解除)


def _sleep_screen(screen) -> bool:
    """画面を消灯する: まず明るさを 0 にし、その後に黒一色の 1920x462 を送る。

    screen へのアクセスは呼び出し側 (_ScreenGuard.lock) で直列化すること。
    明るさを 0 にできなかったら False を返す (呼び出し側は dimmed を立てない
    ので、次の周回で再試行する)。黒フレームの送信に失敗しても (USB エラーは
    _output が警告だけ出す) 明るさ 0 が済んでいれば消灯済みとして True。
    """
    if screen is None:
        return True  # 画面が無い (--no-screen / 未接続) ときは消すものも無い
    try:
        screen.brightness(0)
    except Exception as exc:  # USB unplugged, etc.
        log.warning("明るさを 0 にできません: %s", exc)
        return False
    black = Image.new("RGB", (1920, 462), (0, 0, 0))
    _output(black, screen, None)
    return True


def _window_order(args, cfg: Config) -> Optional[List[str]]:
    """表示する窓の順番/絞り込み。--only が display.windows を上書きする。"""
    only = getattr(args, "only", None)
    if only:
        return list(only)
    return list(cfg.display.windows) or None


def _effective_poll(args, cfg: Config) -> Tuple[int, int]:
    """(interval, redraw)。CLI 引数が設定ファイル (poll) より優先。"""
    interval = getattr(args, "interval", None)
    redraw = getattr(args, "redraw", None)
    return (
        interval if interval is not None else cfg.poll.interval,
        redraw if redraw is not None else cfg.poll.redraw,
    )


# ----------------------------------------------------------- config command

def _find_example_file() -> Optional[Path]:
    here = Path(__file__).resolve()
    for base in (here.parents[2], Path.cwd()):  # リポジトリ直下 / 実行ディレクトリ
        candidate = base / "config.example.toml"
        if candidate.is_file():
            return candidate
    return None


def _init_config(target: Optional[Path]) -> int:
    """config.example.toml の内容で設定ファイルを作る。既にあれば上書きしない。"""
    path = Path(target).expanduser() if target else DEFAULT_PATH
    if path.exists():
        print(t("cli.init.exists", path=path))
        return 0
    example = _find_example_file()
    if example is None:
        print(t("cli.init.no_example"), file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print(t("cli.init.created", path=path, example=example))
    return 0


def _cmd_config(args, cfg: Config) -> int:
    """実際に使われる設定（既定値込み）と、現在取得できる窓 ID を表示する。"""
    if getattr(args, "json", False):
        _print_json(config_to_dict(cfg))
        return 0
    print(format_toml(cfg))
    print()
    print(t("cli.config.window_ids"))
    enabled = cfg.providers.enabled()
    usages = {u.provider: u for u in providers.fetch_all(datetime.now(timezone.utc), enabled=enabled)}
    for name in PROVIDERS:
        if name not in enabled:
            print("  %-7s %s" % (name, t("cli.config.disabled", name=name)))
            continue
        usage = usages.get(name)
        if usage is None:
            print("  %-7s %s" % (name, t("cli.config.not_fetched")))
        elif usage.error:
            print("  %-7s %s" % (name, t("cli.config.fetch_error", error=usage.error)))
        else:
            ids = [w.id for w in usage.windows]
            print("  %-7s %s" % (name, "  ".join(ids) if ids else t("cli.config.no_windows")))
    return 0


# ------------------------------------------------------- app 連携コマンド

def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(message: str) -> int:
    """app 連携コマンドの失敗: {"error": ...} を stdout に出して非 0 で終了する。"""
    print(json.dumps({"error": message}, ensure_ascii=False))
    return 1


def _cmd_detect(args) -> int:
    """detect: この Mac で使えるサービスと、そのプラン/窓の一覧。"""
    results = providers.detect_all(datetime.now(timezone.utc))
    if getattr(args, "json", False):
        _print_json(results)
        return 0
    for r in results:
        head = "%-7s %s" % (r["provider"], r["name"])
        if not r["detected"]:
            print("%s  %s" % (head, t("cli.detect.unusable", error=r["error"])))
            continue
        detail = "plan=%s" % r["plan"] if r["plan"] else t("cli.detect.plan_unknown")
        if r["windows"]:
            detail += "  windows: " + ", ".join(w["key"] for w in r["windows"])
        print(t("cli.detect.ok", head=head, source=r["source"], detail=detail))
    return 0


def _cmd_config_set_json(args) -> int:
    """config --set-json: stdin の JSON で設定を丸ごと置き換えて保存する。"""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail(t("cli.set_json.bad_json", detail=exc))
    try:
        cfg = config_from_dict(data)
    except ConfigError as exc:
        return _fail(str(exc))
    try:
        save_config(cfg, args.config)
    except ConfigError as exc:
        return _fail(str(exc))
    except OSError as exc:
        return _fail(t("cli.set_json.save_failed", path=args.config or DEFAULT_PATH,
                       detail=exc.strerror or exc))
    _print_json(config_to_dict(cfg))
    return 0


def _read_secret(prompt: str) -> str:
    """stdin から 1 行読む。端末のときはエコーしない。argv には絶対に載せない。"""
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass(prompt).strip()
    return sys.stdin.readline().strip()


def _cmd_refresh(args) -> int:
    """refresh: 反映要求ファイルを作るだけ (エンジンが動いていなくてもエラーにしない)。"""
    path = request_refresh()
    if args.json:
        _print_json({"requested": True})
    else:
        print(t("cli.refresh.created", path=path))
    return 0


# ------------------------------------------------------------------ pick

PICK_MAX_AGE = timedelta(minutes=20)   # status.json を「新しい」とみなす上限


def _status_recommendation(now: datetime, path: Path = STATUS_PATH) -> Tuple[bool, Optional[dict]]:
    """status.json の "recommendation" を dict で返す。

    ファイルが無い・壊れている・20 分以上古いときは (False, None) を返す
    (呼び出し側が 1 回だけ取得して計算し直す)。新しいが推奨なしなら (True, None)。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False, None
    if not isinstance(data, dict):
        return False, None
    updated = _parse_iso(data.get("updated_at"))
    if updated is None or (now - updated) >= PICK_MAX_AGE:
        return False, None
    rec = data.get("recommendation")
    return True, rec if isinstance(rec, dict) else None


def _cmd_pick(args, cfg: Config) -> int:
    """pick: 今使う先 (F1) を 1 行で出す。status.json が新しいときはその中の
    recommendation を使う。無い・古いときは取得を 1 回して計算する
    (画面には送らず、status.json / history.jsonl / periods.jsonl にも
    書かない読み取り専用の計算)。"""
    now = datetime.now(timezone.utc)
    fresh, rec = _status_recommendation(now, STATUS_PATH)
    if not fresh:
        history = History(args.history) if args.history else History()
        poller = Poller(history, enabled=cfg.providers.enabled(), record=False)
        usages = poller.fetch(now)
        found = recommend(usages, history, now, tight=cfg.pacing.tight,
                          on_track=cfg.pacing.on_track, sleep=cfg.pacing.sleep)
        rec = found.payload() if found is not None else None
    if args.json:
        _print_json(rec if rec is not None else {"recommendation": None})
    else:
        print(rec["text"] if rec is not None else t("cli.pick.none"))
    return 0


# ---------------------------------------------------------------- routing


def _routing_status_paths(args) -> Tuple[Path, Optional[Path]]:
    """(主, 予備) の status.json パス。--status / QUOTA_STATUS_PATH、
    --fallback-status / QUOTA_STATUS_FALLBACK で上書きできる。"""
    primary = getattr(args, "status", None) or os.environ.get("QUOTA_STATUS_PATH") \
        or STATUS_PATH
    fallback = getattr(args, "fallback_status", None) or os.environ.get("QUOTA_STATUS_FALLBACK")
    return Path(primary).expanduser(), Path(fallback).expanduser() if fallback else None


def _load_gate_status(args, cfg: Config) -> Tuple[str, Optional[dict], str, Path]:
    """routing 用に status.json を読む。主が無いか stale_minutes より古いときは
    予備 (--fallback-status / QUOTA_STATUS_FALLBACK) を使う (動いている別の Mac
    からのコピーに切り替えるため)。どちらもだめなら主の結果を返す。"""
    primary, fallback = _routing_status_paths(args)
    now = gate.now_local()
    state, data, detail = gate.load_status(primary, cfg.routing.stale_minutes, now)
    if state != "ok" and fallback is not None:
        f_state, f_data, f_detail = gate.load_status(fallback, cfg.routing.stale_minutes, now)
        if f_state == "ok":
            return f_state, f_data, f_detail, fallback
    return state, data, detail, primary


def _cmd_routing_line(args, cfg: Config) -> int:
    """routing line: 判定の 1 行サマリ。常に exit 0 (例外でも stderr 1 行)。
    サービスへの取得は絶対にしない (status.json を読むだけ)。"""
    if not cfg.routing.enabled:
        return 0
    try:
        lang = current_language()
        state, data, detail, path = _load_gate_status(args, cfg)
        now = gate.now_local()
        if args.json:
            if state != "ok":
                _print_json({"state": "stale", "detail": detail})
            else:
                result = gate.evaluate(data, cfg.routing,
                                       gate.load_burn(gate.BURN_PATH, now), now,
                                       previous=data.get("routing"))
                _print_json(result.to_dict())
            return 0
        if state != "ok":
            print(gate.format_stale_line(detail, path, data, now, lang))
            return 0
        result = gate.evaluate(data, cfg.routing, gate.load_burn(gate.BURN_PATH, now), now,
                               previous=data.get("routing"))
        line = gate.format_line(result, now, lang)
        if line:  # ワーカー 0 件のときは何も出さない (空白の崩れた行を出さない)
            print(line)
    except Exception as exc:  # 判定できないときも起動は止めない (fail-open)
        print("routing: %s" % exc, file=sys.stderr)
    return 0


def _cmd_routing_check(args, cfg: Config) -> int:
    """routing check <名前>: stop なら exit 3 (stderr 1 行)、reserve は注意 1 行、
    unknown は「判定できない」旨 1 行 (いずれも exit 0)、go / boost は無言。
    名前は KNOWN_PROVIDERS か [routing.workers] のキーだけ有効で、それ以外は
    status.json の中身に関係なく exit 2。status.json に窓が無い既知名は
    unknown (missing) で exit 0。QUOTA_GATE_FORCE=1 なら常に exit 0。"""
    if not cfg.routing.enabled:
        return 0
    if os.environ.get("QUOTA_GATE_FORCE") == "1":
        return 0
    try:
        return _routing_check_impl(args, cfg)
    except Exception as exc:  # fail-open, as with routing line
        print("routing: %s" % str(exc).replace("\n", " ").replace("\r", " "), file=sys.stderr)
        return 0


def _routing_check_impl(args, cfg: Config) -> int:
    name = args.name
    lang = current_language()
    known = set(cfg.routing.workers) | set(gate.KNOWN_PROVIDERS)
    if name not in known:  # 引数の妥当性は status.json の中身に依存させない
        return 2
    state, data, detail, _path = _load_gate_status(args, cfg)
    if state != "ok":  # 無し / 古い / 壊れ → unknown (fail-open、起動は止めない)
        worker = gate.WorkerResult(name=name, verdict="unknown",
                                   reason=gate.stale_reason(detail), stale=True)
        if args.json:
            _print_json(worker.to_dict())
            return 0
        message = gate.check_message(worker, gate.now_local(), lang)
        if message:
            print(message, file=sys.stderr)
        return 0
    now = gate.now_local()
    result = gate.evaluate(data, cfg.routing, gate.load_burn(gate.BURN_PATH, now), now,
                           previous=data.get("routing"))
    worker = result.worker(name)
    if worker is None:  # そのワーカーの窓もエラーも status に無い → unknown (missing)
        worker = gate.WorkerResult(name=name, verdict="unknown", reason="missing")
    if args.json:
        _print_json(worker.to_dict())
        return 0 if worker.verdict != "stop" else 3
    message = gate.check_message(worker, now, lang)
    if worker.verdict == "stop":
        print(message, file=sys.stderr)
        return 3
    if message:
        print(message, file=sys.stderr)
    return 0


def _print_hook_context(event: str, context: str) -> None:
    """SessionStart フックの additionalContext を 1 行の JSON で出す。"""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                             "additionalContext": context}},
                     ensure_ascii=False))


def _cmd_routing_hook(args, cfg: Config) -> int:
    """routing hook [--event NAME]: プラグインの SessionStart フック用。
    `routing line` と同じ 1 行に短いルール (locale の routing.hook.rules) を
    続けた additionalContext を、1 行の JSON で stdout に出す。
    status.json が無い・古い・壊れているときは line と同じ stale の 1 行だけ
    入れる (判定できない旨)。enabled=false・ワーカー 0 件・例外のときは
    何も出さない。どんなときも exit 0。サービスへの取得は絶対にしない
    (line と同じ読み取り経路。--status / --fallback-status / 環境変数も同じ)。"""
    if not cfg.routing.enabled:
        return 0
    try:
        lang = current_language()
        state, data, detail, path = _load_gate_status(args, cfg)
        now = gate.now_local()
        if state != "ok":
            _print_hook_context(args.event,
                                gate.format_stale_line(detail, path, data, now, lang))
            return 0
        result = gate.evaluate(data, cfg.routing, gate.load_burn(gate.BURN_PATH, now), now,
                               previous=data.get("routing"))
        line = gate.format_line(result, now, lang)
        if line:  # ワーカー 0 件のときは何も出さない (line と同じ)
            _print_hook_context(args.event, line + "\n\n" + t("routing.hook.rules"))
    except Exception:
        pass  # 判定できないときもセッション開始は止めない (fail-open、無出力)
    return 0


def _burn_payload(active: Dict[str, datetime]) -> Dict[str, Any]:
    """routing burn --json の応答: 有効な使い切りモードの一覧 (名前順)。"""
    return {"burn": [{"name": name, "until": active[name].isoformat()}
                     for name in sorted(active)]}


def _cmd_routing_burn(args, cfg: Config) -> int:
    """routing burn: 使い切りモード。期限内はそのワーカーの reserve / go / boost を
    boost とみなす (stop と unknown はそのまま)。--json のときは一覧も on / off の
    実行後も、有効なエントリを {"burn": [{"name", "until"}]} の形で出す
    (--json なしのテキスト出力は従来どおり)。"""
    lang = current_language()
    now = gate.now_local()
    if not args.name:
        active = gate.load_burn(gate.BURN_PATH, now)
        if args.json:
            _print_json(_burn_payload(active))
            return 0
        if not active:
            print(t("routing.burn.none"))
        for name in sorted(active):
            print(t("routing.burn.entry", name=name,
                    dt=active[name].strftime("%m/%d %H:%M")))
        return 0
    action = args.action
    known = set(cfg.routing.workers) if cfg.routing.workers else set(gate.KNOWN_PROVIDERS)
    # off は workers / KNOWN_PROVIDERS に無い名前でも、burn 状態にその名前があれば
    # 解除できる (設定から名前を消したあとに残ったエントリを掃除できるように。
    # on の既知チェックはそのまま)。
    active = gate.load_burn(gate.BURN_PATH, now)
    valid = action in ("on", "off") and (args.name in known
                                         or (action == "off" and args.name in active))
    if not valid:
        # 無効な引数は --json の有無に関係なく exit 2 (--json のときは {"error"} を出す)
        if args.json:
            print(json.dumps({"error": t("routing.usage")}, ensure_ascii=False))
        else:
            print(t("routing.usage"), file=sys.stderr)
        return 2
    until = gate.parse_until("23:59", now)
    if action == "on" and args.until:
        parsed = gate.parse_until(args.until, now)
        if parsed is None:
            if args.json:
                print(json.dumps({"error": t("routing.burn.bad_until")}, ensure_ascii=False))
            else:
                print(t("routing.burn.bad_until"), file=sys.stderr)
            return 2
        until = parsed
    if action == "on":
        gate.store_burn(gate.BURN_PATH, args.name, until, now)
        if not args.json:
            print(t("routing.burn.set", host=os.uname().nodename.split(".")[0],
                    name=args.name, dt=until.strftime("%m/%d %H:%M")))
    else:
        gate.clear_burn(gate.BURN_PATH, args.name, now)
        if not args.json:
            print(t("routing.burn.cleared", host=os.uname().nodename.split(".")[0],
                    name=args.name))
    if args.json:
        _print_json(_burn_payload(gate.load_burn(gate.BURN_PATH, now)))
    return 0


def _cmd_routing(args, cfg: Config) -> int:
    if args.routing_cmd == "line":
        return _cmd_routing_line(args, cfg)
    if args.routing_cmd == "check":
        return _cmd_routing_check(args, cfg)
    if args.routing_cmd == "burn":
        return _cmd_routing_burn(args, cfg)
    if args.routing_cmd == "hook":
        return _cmd_routing_hook(args, cfg)
    print(t("routing.usage"), file=sys.stderr)
    return 2


def _cmd_set_key(args) -> int:
    """set-key: API キーを login キーチェーンに保存 / --delete で削除する。"""
    provider = providers.REGISTRY.get(args.provider)
    if provider is None:
        return _fail(t("cli.set_key.unknown_provider", provider=args.provider,
                       choices=" / ".join(providers.REGISTRY)))
    if not provider.needs_key:
        return _fail(t("cli.set_key.no_key_needed", name=provider.shown_name))
    if args.delete:
        try:
            keychain.delete(provider.id)
        except keychain.KeychainError as exc:
            return _fail(str(exc))
        if args.json:
            _print_json({"provider": provider.id, "deleted": True})
        else:
            print(t("cli.set_key.deleted", name=provider.shown_name))
        return 0
    key = _read_secret(t("cli.set_key.prompt", name=provider.shown_name))
    if not key:
        return _fail(t("cli.set_key.empty_key"))
    try:
        keychain.write(provider.id, key)
    except keychain.KeychainError as exc:
        return _fail(str(exc))
    if args.json:
        _print_json({"provider": provider.id, "stored": True})
    else:
        print(t("cli.set_key.stored", name=provider.shown_name,
                service=keychain.SERVICE, account=provider.id))
    return 0


# --------------------------------------------------------------------- main

def _add_config_arg(parser: argparse.ArgumentParser, default=argparse.SUPPRESS) -> None:
    parser.add_argument("--config", type=Path, default=default, metavar="PATH",
                        help=t("cli.help.config", default=DEFAULT_PATH))


def _early_language(argv) -> str:
    """main 冒頭の表示言語。設定ファイル (既定パス、または argv の --config)
    を tomllib で生読みし、display.language が "auto"/"ja"/"en" ならそれを
    使う。読めない・無い・不正なときは "auto" (macOS の言語設定)。これで
    設定を読む前に return する detect / set-key / refresh と、設定の検証
    エラー文も設定の言語で出る。"""
    args = sys.argv[1:] if argv is None else list(argv)
    path = DEFAULT_PATH
    for i, arg in enumerate(args):
        if arg == "--config" and i + 1 < len(args):
            path = Path(args[i + 1])
            break
        if arg.startswith("--config="):
            path = Path(arg.split("=", 1)[1])
            break
    try:
        with path.expanduser().open("rb") as fh:
            display = tomllib.load(fh).get("display", {})
    except (OSError, tomllib.TOMLDecodeError):
        return "auto"
    lang = display.get("language") if isinstance(display, dict) else None
    return lang if lang in LANGUAGE_CHOICES else "auto"


def main(argv=None) -> int:
    set_language(_early_language(argv))  # 本読みの前に argv の --config だけ拾う
    parser = argparse.ArgumentParser(prog="quota-canvas", description="AI quota pacing on the TURZX screen")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--out", type=Path, help="also save each frame as this PNG")
    parser.add_argument("--no-screen", action="store_true", help="don't touch the USB screen")
    parser.add_argument("--history", type=Path, help="history file (default: ~/Library/Application Support/quota-canvas/history.jsonl)")
    parser.add_argument("--only", nargs="+", metavar="ID", help=t("cli.help.only"))
    _add_config_arg(parser, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    _add_config_arg(sub.add_parser("once", help="fetch, draw and exit"))
    p = sub.add_parser("run", help="keep the screen updated")
    p.add_argument("--interval", type=int, default=None,
                   help="seconds between usage fetches (default: config poll.interval, 900)")
    p.add_argument("--redraw", type=int, default=None,
                   help="seconds between redraws (default: config poll.redraw, 60)")
    _add_config_arg(p)
    p_config = sub.add_parser("config", help="show the effective config and available window ids")
    p_config.add_argument("--init", action="store_true",
                          help="create the config file from config.example.toml (never overwrites)")
    config_flags = p_config.add_mutually_exclusive_group()
    config_flags.add_argument("--json", action="store_true",
                              help=t("cli.help.json_config"))
    config_flags.add_argument("--set-json", action="store_true", dest="set_json",
                              help="read a full config object as JSON from stdin and save it")
    _add_config_arg(p_config)
    p_detect = sub.add_parser("detect", help=t("cli.help.detect"))
    p_detect.add_argument("--json", action="store_true", help="print the result as JSON")
    p_key = sub.add_parser("set-key", help=t("cli.help.set_key"))
    p_key.add_argument("provider", metavar="PROVIDER", help="service id, e.g. glm")
    p_key.add_argument("--delete", action="store_true", help="remove the stored key")
    p_key.add_argument("--json", action="store_true", help="print the result as JSON")
    p_refresh = sub.add_parser("refresh",
                               help=t("cli.help.refresh", path=refresh_mod.REFRESH_REQUEST_PATH))
    p_refresh.add_argument("--json", action="store_true", help='print {"requested": true} as JSON')
    p_pick = sub.add_parser("pick", help=t("cli.help.pick"))
    p_pick.add_argument("--json", action="store_true",
                        help="print the recommendation as JSON (no recommendation: {\"recommendation\": null})")
    # routing: status.json だけを読む軽いコマンド (サービスへの取得はしない)。
    # 重い準備 (Poller / 画面) をしないよう、サブコマンドは共通の親パーサで受ける。
    p_routing = sub.add_parser("routing", help=t("cli.help.routing"))
    routing_common = argparse.ArgumentParser(add_help=False)
    routing_common.add_argument("--status", type=Path, default=None, metavar="PATH",
                                help=t("cli.help.routing_status", default=STATUS_PATH))
    routing_common.add_argument("--fallback-status", type=Path, default=None, metavar="PATH",
                                help=t("cli.help.routing_fallback", var="QUOTA_STATUS_FALLBACK"))
    routing_subs = p_routing.add_subparsers(dest="routing_cmd", required=True)
    p_line = routing_subs.add_parser("line", parents=[routing_common],
                                     help=t("cli.help.routing_line"))
    p_line.add_argument("--json", action="store_true", help="print the result as JSON")
    p_check = routing_subs.add_parser("check", parents=[routing_common],
                                      help=t("cli.help.routing_check"))
    p_check.add_argument("name", metavar="NAME", help="worker name, e.g. glm")
    p_check.add_argument("--json", action="store_true", help="print the worker result as JSON")
    p_burn = routing_subs.add_parser("burn", help=t("cli.help.routing_burn"))
    p_burn.add_argument("name", nargs="?", default=None, metavar="NAME",
                        help="worker name, e.g. glm")
    p_burn.add_argument("action", nargs="?", default=None, metavar="ON|OFF",
                        help='"on" or "off"')
    p_burn.add_argument("--until", default=None, metavar="HH:MM|ISO",
                        help="burn until this time (default: today 23:59)")
    p_burn.add_argument("--json", action="store_true",
                        help='print the active entries as {"burn": [...]} as JSON')
    p_hook = routing_subs.add_parser("hook", parents=[routing_common],
                                     help=t("cli.help.routing_hook"))
    p_hook.add_argument("--event", default="SessionStart", metavar="NAME",
                        help="hookEventName to report (default: SessionStart)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    # 設定ファイル不要のコマンドを先に処理する
    if args.command == "detect":
        return _cmd_detect(args)
    if args.command == "set-key":
        return _cmd_set_key(args)
    if args.command == "refresh":
        return _cmd_refresh(args)
    if args.command == "config":
        if args.init:
            return _init_config(args.config)
        if args.set_json:
            return _cmd_config_set_json(args)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        if args.command == "routing" and getattr(args, "routing_cmd", None) == "hook":
            # 壊れた設定でもセッション開始は止めない (fail-open、無出力)
            return 0
        if args.command == "config" and args.json:
            return _fail(str(exc))
        print(t("cli.config_error", detail=exc), file=sys.stderr)
        return 2

    lang = set_language(cfg.display.language)  # ここから先の文言は display.language に従う
    if args.command == "config":
        return _cmd_config(args, cfg)
    if args.command == "pick":
        return _cmd_pick(args, cfg)  # 画面も status.json も触らないので先行して抜ける
    if args.command == "routing":
        # status.json を読むだけ (取得・描画なし) のため Poller / 画面の準備前に抜ける
        return _cmd_routing(args, cfg)

    history = History(args.history) if args.history else History()
    poller = Poller(history, enabled=cfg.providers.enabled())
    screen = None if args.no_screen else _open_screen()
    applied_brightness = _apply_brightness(screen, cfg, None)

    if args.command == "once":
        now = datetime.now(timezone.utc)
        usages = poller.fetch(now)
        cards = build_cards(usages, history, now, order=_window_order(args, cfg),
                            tight=cfg.pacing.tight, on_track=cfg.pacing.on_track,
                            provider_windows={name: cfg.providers.windows_for(name)
                                              for name in PROVIDERS},
                            sleep=cfg.pacing.sleep)
        rec = recommend(usages, history, now, tight=cfg.pacing.tight,
                        on_track=cfg.pacing.on_track, sleep=cfg.pacing.sleep)
        img = _render_frame(cards, now, cfg, _render_layout(cfg, cards),
                            recommendation=rec.text if rec else None)
        _output(img, screen, args.out)
        return 0 if cards else 1

    usages: List[ProviderUsage] = []
    next_fetch = 0.0
    last_fetch: Optional[float] = None  # 最後に利用量を取り直した monotonic 時刻
    deferred_fetch: Optional[float] = None  # 延期した反映要求で取り直す monotonic 時刻
    wake = ""  # 前回の待ちを打ち切った理由 ("refresh" / "config" / "display" / "" = 満了)
    started = time.monotonic()

    # --- Mac スリープ連携 (display.sleep_with_mac) ---------------------------
    # IOKit の on_sleep / on_wake は別スレッドから来るので、画面へのアクセスは
    # guard.lock で直列化する。再読み込み後の cfg は current 経由で共有する。
    guard = _ScreenGuard()
    current: Dict[str, Config] = {"cfg": cfg}

    def on_system_sleep() -> None:
        # システムスリープの直前 (監視スレッド): 明るさ 0 + 黒 1 枚で消灯する。
        # ここが戻るまで実際のスリープは始まらないので短く済ませる。ロックは
        # USB 操作のあいだだけ借りる。すぐ取れないときは消灯を諦める (スリープ
        # 自体は macpower 側が必ず IOAllowPowerChange で許可し、run ループが
        # 次の周回で再試行する)。
        try:
            cfg_now = current["cfg"]
            if not cfg_now.display.sleep_with_mac or cfg_now.display.paused:
                return
            if not guard.lock.acquire(timeout=5):
                log.warning("画面が使用中のためスリープ前の消灯を諦めます")
                return
            try:
                guard.system_asleep = True
                if not guard.dimmed:
                    guard.dimmed = _sleep_screen(screen)
            finally:
                guard.lock.release()
        except Exception:
            log.exception("スリープ前の画面消灯に失敗しました")

    def on_system_wake() -> None:
        # スリープからの復帰 (監視スレッド): 報告を解除するだけ。明るさの
        # 再設定と再描画は run ループ側が行う (guard.dimmed はループ側で解除)。
        try:
            with guard.lock:
                guard.system_asleep = False
        except Exception:
            log.exception("スリープ復帰の処理に失敗しました")

    def _sleep_mismatch() -> bool:
        # 待ち打ち切り用: 「眠っているか」と「消灯済みか」が食い違えば True。
        # ループを回した直後は必ず一致するので、ディスプレイのスリープ/復帰や
        # システムスリープからの復帰 (on_sleep で消灯済み → 起きた) で変化する。
        with guard.lock:
            system_asleep, dimmed = guard.system_asleep, guard.dimmed
        sleeping = current["cfg"].display.sleep_with_mac and (
            system_asleep or macpower.display_asleep())
        return bool(sleeping) != dimmed

    try:
        macpower.start_watch(on_system_sleep, on_system_wake)
    except Exception as exc:
        log.warning("Mac のシステムスリープ監視を開始できません: %s", exc)

    providers.claude.set_token_refresh(cfg.providers.claude_token_refresh)
    providers.codex.set_token_refresh(cfg.providers.codex_token_refresh)
    providers.grok.set_token_refresh(cfg.providers.grok_token_refresh)
    providers.gemini.set_token_refresh(cfg.providers.gemini_token_refresh)
    while True:
        cfg, reloaded, now_lang = _refresh_config(cfg)
        if reloaded:
            poller.enabled = cfg.providers.enabled()
            providers.claude.set_token_refresh(cfg.providers.claude_token_refresh)
            providers.codex.set_token_refresh(cfg.providers.codex_token_refresh)
            providers.grok.set_token_refresh(cfg.providers.grok_token_refresh)
            providers.gemini.set_token_refresh(cfg.providers.gemini_token_refresh)
        current["cfg"] = cfg
        now = datetime.now(timezone.utc)
        interval, redraw = _effective_poll(args, cfg)
        mono = time.monotonic()
        due = mono >= next_fetch
        if now_lang != lang:
            # 表示言語が変わった周回: 取得済みの窓ラベルやエラー文が古い言語
            # のままなので、描き直しだけでなく取得間隔を待たずに取り直す
            # (429 のバックオフ中のサービスは poller が従来どおり延期する)。
            due = True
        lang = now_lang
        # 反映要求: 前回の取得から 30 秒以上あいていれば取り直す (HTTP 429 対策)。
        # 30 秒未満なら捨てずに、前回の取得の 30 秒後まで延期する (トークン更新の
        # 直後の要求が、後ろのサービスの取得時間ぶん早く届いても取りこぼさない)。
        # 設定変更だけのときは取り直さず、手元の利用量で描き直す。
        requested = wake == "refresh" and (last_fetch is None or mono - last_fetch >= REFRESH_MIN_GAP)
        if wake == "refresh" and not requested:
            deferred_fetch = last_fetch + REFRESH_MIN_GAP
            log.info("前回の取得から 30 秒未満のため、取り直しを 30 秒後まで延期します")
        if deferred_fetch is not None and mono >= deferred_fetch:
            requested = True
        # Mac が眠っている間 (sleep_with_mac のとき) は利用量を取りに行かない (各社 API
        # への無駄な問い合わせを避ける)。next_fetch はそのまま残すので、起きた直後の
        # ループで期限切れなら取り直す。
        with guard.lock:
            system_asleep = guard.system_asleep
        sleeping = cfg.display.sleep_with_mac and (
            system_asleep or macpower.display_asleep())
        if requested and sleeping and deferred_fetch is None:
            deferred_fetch = mono  # 眠っていて取れなかった要求は、起きた周回で取り直す
        if (due or requested) and not sleeping:
            usages = poller.fetch(now)
            last_fetch = time.monotonic()
            next_fetch = last_fetch + interval  # 取り直したら定期取得もそこから interval 後へ
            deferred_fetch = None
        cards = build_cards(usages, history, now, order=_window_order(args, cfg),
                            tight=cfg.pacing.tight, on_track=cfg.pacing.on_track,
                            provider_windows={name: cfg.providers.windows_for(name)
                                              for name in PROVIDERS},
                            sleep=cfg.pacing.sleep)
        # status.json の "windows" (表示フィルタ前の全窓) 用。判定材料なので
        # [routing].enabled に関係なく毎周回作る (compute_pace は純粋計算だけ)。
        windows_all = all_window_paces(usages, history, now, tight=cfg.pacing.tight,
                                       on_track=cfg.pacing.on_track, sleep=cfg.pacing.sleep)
        rec = recommend(usages, history, now, tight=cfg.pacing.tight,
                        on_track=cfg.pacing.on_track, sleep=cfg.pacing.sleep)
        layout = _render_layout(cfg, cards)
        if cfg.display.paused:
            # 一時停止中: 画面には何も送らず status.json だけ更新する (frame.png はそのまま)
            _write_app_files(cfg, cards, usages, now, None, layout=layout,
                             recommendation=rec, all_windows=windows_all)
        elif sleeping:
            # Mac が眠っている: 黒 1 枚と明るさ 0 を送ってからは黙る
            # (frame.png はそのまま、status.json は更新し続ける)
            with guard.lock:
                if not guard.dimmed:
                    guard.dimmed = _sleep_screen(screen)
            _write_app_files(cfg, cards, usages, now, None, layout=layout,
                             recommendation=rec, all_windows=windows_all)
        else:
            # ロックは USB 操作 (_open_screen / 明るさ / 送画) の短い区間だけで
            # 使う。render と status.json への書き込みはロックの外で行う。
            shift = SHIFTS[int((time.monotonic() - started) // SHIFT_EVERY.total_seconds()) % len(SHIFTS)]
            img = _render_frame(cards, now, cfg, layout,
                                recommendation=rec.text if rec else None,
                                offset=shift)
            sent = img
            with guard.lock:
                # ロックを取る直前に眠り始めたかもしれない: もう一度取り直して
                # から送る。まだ眠っているなら dimmed を戻さず、通常フレームも
                # 送らない (黒で消したままにする)。
                if cfg.display.sleep_with_mac and (
                        guard.system_asleep or macpower.display_asleep()):
                    if not guard.dimmed:
                        guard.dimmed = _sleep_screen(screen)
                    sent = None
                else:
                    if guard.dimmed:
                        # スリープから復帰: 明るさを設定し直してすぐ描き直す
                        guard.dimmed = False
                        applied_brightness = None
                    if screen is None and not args.no_screen:
                        screen = _open_screen()
                        if screen is not None:
                            applied_brightness = None  # 新しく開いた画面には明るさを設定し直す
                    applied_brightness = _apply_brightness(screen, cfg, applied_brightness)
                    sent_to = _output(img, screen, args.out)
                    if sent_to is not screen:
                        # 開き直した (または失った) 画面には明るさを設定し直す
                        applied_brightness = _apply_brightness(sent_to, cfg, None)
                    screen = sent_to
            # 送ったフレームだけ frame.png に書く (黒で消したときはそのまま)
            _write_app_files(cfg, cards, usages, now, sent, layout=layout,
                             recommendation=rec, all_windows=windows_all)
        # 待ち時間を 1 秒刻みに分け、設定変更・反映要求・スリープ状態の変化を監視する。
        # 延期した取り直しがあれば、その時刻で待ちを打ち切る (眠っている間は縮めない)。
        wait = redraw
        if deferred_fetch is not None and not sleeping:
            wait = min(redraw, max(0.0, deferred_fetch - time.monotonic()))
        wake = wait_for_wakeup(cfg, wait, _sleep_mismatch)


if __name__ == "__main__":
    sys.exit(main())
