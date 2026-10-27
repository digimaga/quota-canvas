"""設定ファイル (~/.config/quota-canvas/config.toml) の読み込み。

標準ライブラリの tomllib で読み、Config dataclass に変換する。

- ファイルが無くても動く（全部既定値。今までと同じ挙動）
- 未知のキーは警告ログを出して無視
- 型や値の範囲が違えば ConfigError（そのままユーザーに表示できるメッセージ）
"""

from __future__ import annotations

import json
import logging
import math
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# 列の組み立ては render と共通の規則 (1 列 = large 1 枚 or small 2 枚)。
from .render import MAX_COLUMNS as MAX_PANEL_COLUMNS, group_columns
from .i18n import LANGUAGE_CHOICES, t

log = logging.getLogger("quotacanvas")

DEFAULT_PATH = Path.home() / ".config" / "quota-canvas" / "config.toml"

PROVIDERS = ("claude", "codex", "glm", "grok", "gemini")

INTERVAL_MIN = 60  # poll.interval の最小値 (秒)
REDRAW_MIN = 10    # poll.redraw の最小値 (秒)

PathLike = Union[str, Path]


class ConfigError(Exception):
    """設定ファイルが不正。メッセージはそのままユーザーに表示される。"""


# --------------------------------------------------------------- dataclasses

@dataclass
class Night:
    """夜間減光。start > end なら日付をまたぐ（例: 23:00 → 07:00）。"""

    start: time = time(23, 0)
    end: time = time(7, 0)
    brightness: int = 20

    def is_night(self, at: datetime) -> bool:
        cur = at.time()
        if self.start == self.end:
            return False  # 長さ 0 は無効扱い
        if self.start < self.end:
            return self.start <= cur < self.end
        return cur >= self.start or cur < self.end  # start > end: 日付またぎ


GAUGES = ("remaining", "used")  # display.gauge の値

SEPARATOR_MAX = 5  # display.separator の最大文字数

SLEEP_PROVIDERS = PROVIDERS  # pacing.sleep.skip に入れられる provider id
SLEEP_ADVICES = ("reset", "bedtime")  # pacing.sleep.advice の値


@dataclass
class Sleep:
    """睡眠帯 (pacing.sleep)。start > end なら日付をまたぐ（例: 00:00 → 08:00）。

    ペース判定を「起きている時間」で行うための設定。skip に入れた provider
    には睡眠時間を適用しない（壁時計のまま判定する）。

    advice = "reset"（既定）は従来どおりアドバイス行もリセット基準。
    "bedtime" は、寝る時刻（start）がリセットより先に来るとき、アドバイス行の
    残り時間を「リセットまで」の代わりに「寝るまで」で出す。
    """

    start: time = time(0, 0)
    end: time = time(8, 0)
    skip: List[str] = field(default_factory=list)
    advice: str = "reset"


SIZES = ("large", "small")  # layout.panels[].size の値

TOKEN_REFRESH_MODES = ("cli", "off")  # providers.<p>.token_refresh の値
TOKEN_REFRESH_PROVIDERS = ("claude", "codex", "grok", "gemini")  # token_refresh キーを持てる provider


@dataclass
class Panel:
    """[[layout.panels]] の 1 要素: 画面のパネル 1 枚ぶんの配置。

    main はメインの窓 ID ("claude:weekly") かサービス ID ("gemini")。
    サービス ID ならそのサービスの代表窓が自動で選ばれる。main = "" は
    size = "small" のときだけ可: その段は空き枠になり、位置は詰められない。
    subs は下に並べるサブの窓 ID (large パネルのみ)。None (省略) なら自動:
    同じサービスの残りの窓のうち、ほかのパネルの main になっていないもの。
    """

    main: str
    subs: Optional[List[str]] = None
    size: str = "large"


@dataclass
class Layout:
    """画面の配置。panels が空（既定）なら描画側の自動配置に任せる。"""

    panels: List[Panel] = field(default_factory=list)


@dataclass
class Display:
    windows: List[str] = field(default_factory=list)  # 窓 ID の順番つき一覧。空なら全部
    brightness: int = 100                             # 0-100
    night: Optional[Night] = None                     # 省略可
    paused: bool = False                              # true の間は画面に送らない
    sleep_with_mac: bool = True                       # Mac スリープで画面も消灯し、復帰で戻す
    gauge: str = "remaining"                          # "remaining" (残り) | "used" (使用量)
    percent_colors: bool = True                       # % の数字を利用状況の色にする (false で全部白)
    separator: str = ""                               # 文の区切り。"" なら言語の既定 (日本語「／」)
    language: str = "auto"                            # "auto" (macOS の言語設定) | "ja" | "en"


@dataclass
class Poll:
    interval: int = 900  # 秒、最小 INTERVAL_MIN
    redraw: int = 60     # 秒、最小 REDRAW_MIN


@dataclass
class Pacing:
    tight: float = 92.0     # リセット時予測がこれ以上なら使い切り
    on_track: float = 70.0  # これ以上なら順調、未満は余裕あり
    sleep: Optional[Sleep] = None  # 睡眠帯。省略 = ペース判定は壁時計のまま（従来どおり）


ROUTING_WORKER_PREFIX = "*"  # routing.workers の値の末尾に付けると前方一致になる


@dataclass
class Routing:
    """[routing]: 残量に応じた AI ワーカー振り分け判定 (既定オフ)。

    enabled = true のときだけ `quota-canvas routing line|check` が判定を出す
    (status.json への "windows" の書き出しは enabled に関係なく行う)。
    workers はワーカー名 → 判定に使う窓 id の配列 (順番が line の表示順)。
    末尾に "*" を付けると前方一致 (例 "grok:*")。空・未指定なら取得できている
    provider ごとに、その provider 名で独立窓 (part_of なし) すべて。
    """

    enabled: bool = False
    floor_percent: float = 5.0   # floor の適用中に残りがこれ未満なら stop
    stale_minutes: int = 20      # fetched_at がこれより古い窓を含むワーカーは stale
    workers: Dict[str, List[str]] = field(default_factory=dict)
    short_over_margin_minutes: Optional[float] = None
    floor_hours: float = 24.0   # 長い窓でリセットまでこれ以上なら floor を適用


@dataclass
class Providers:
    claude: bool = True
    codex: bool = True
    glm: bool = True
    grok: bool = True
    gemini: bool = True
    claude_token_refresh: str = "cli"  # "cli" = 期限切れ時に `claude -p` で更新 / "off" = しない
    codex_token_refresh: str = "cli"   # "cli" = 401/403 時に `codex exec` で更新 / "off" = しない
    grok_token_refresh: str = "cli"    # "cli" = 期限切れ時に `grok models` で更新 / "off" = しない
    gemini_token_refresh: str = "cli"  # (互換) 受け付けるが使わない。トークンの更新は agy 自身が行う
    windows: Dict[str, List[str]] = field(default_factory=dict)  # provider → 窓 key 一覧。空 = 全部
    colors: Dict[str, str] = field(default_factory=dict)  # provider → テーマ色 ("#RRGGBB")。省略可

    def enabled(self) -> Set[str]:
        """取得する provider 名の集合。"""
        return {name for name in PROVIDERS if getattr(self, name)}

    def windows_for(self, provider: str) -> Set[str]:
        """その provider で表示する窓 key の集合。空集合 = 制限なし（全部）。"""
        return set(self.windows.get(provider) or ())


@dataclass
class Config:
    display: Display = field(default_factory=Display)
    layout: Layout = field(default_factory=Layout)
    poll: Poll = field(default_factory=Poll)
    pacing: Pacing = field(default_factory=Pacing)
    providers: Providers = field(default_factory=Providers)
    routing: Routing = field(default_factory=Routing)
    path: Optional[Path] = None  # 読み込んだ（読もうとした）設定ファイル
    _state: Optional[Tuple[int, int]] = field(default=None, repr=False, compare=False)

    def is_stale(self) -> bool:
        """設定ファイルが読み込み時から変わっていれば True（run の再読み込み用）。"""
        return _file_state(self.path) != self._state

    def mark_checked(self) -> None:
        """現在の状態を「確認済み」にする（読み込み失敗時に警告の再表示を防ぐ）。"""
        self._state = _file_state(self.path)


# ------------------------------------------------------------------ loading

def _file_state(path: Optional[Path]) -> Optional[Tuple[int, int]]:
    """(mtime_ns, size)。ファイルが読めなければ None。"""
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load_config(path: Optional[PathLike] = None) -> Config:
    """設定ファイルを読み込む。無ければ全部既定値。不正なら ConfigError。"""
    cfg_path = Path(path).expanduser() if path is not None else DEFAULT_PATH
    data: Dict[str, Any] = {}
    try:
        raw = cfg_path.read_bytes()
    except FileNotFoundError:
        pass  # ファイルが無いのは正常系: 全部既定値
    except OSError as exc:
        raise ConfigError(t("config.read_failed", path=cfg_path,
                            detail=exc.strerror or exc))
    else:
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise ConfigError(t("config.not_utf8", path=cfg_path)) from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(t("config.syntax_error", path=cfg_path, detail=exc)) from None
    return _build(data, cfg_path)


def _build(data: Dict[str, Any], path: Path) -> Config:
    cfg = Config(path=path, _state=_file_state(path))
    for key in data:
        if key not in ("display", "layout", "poll", "pacing", "providers", "routing"):
            _unknown(path, key)

    display = data.get("display")
    if display is not None:
        _need_table(display, "display", path)
        for key, value in display.items():
            if key == "windows":
                cfg.display.windows = _windows_list(value, "display.windows", path)
            elif key == "brightness":
                cfg.display.brightness = _int_in(value, "display.brightness", path, 0, 100)
            elif key == "paused":
                cfg.display.paused = _bool(value, "display.paused", path)
            elif key == "sleep_with_mac":
                cfg.display.sleep_with_mac = _bool(value, "display.sleep_with_mac", path)
            elif key == "gauge":
                cfg.display.gauge = _gauge(value, "display.gauge", path)
            elif key == "percent_colors":
                cfg.display.percent_colors = _bool(value, "display.percent_colors", path)
            elif key == "separator":
                cfg.display.separator = _separator(value, "display.separator", path)
            elif key == "language":
                cfg.display.language = _language(value, "display.language", path)
            elif key == "night":
                _need_table(value, "display.night", path)
                cfg.display.night = _build_night(value, path)
            else:
                _unknown(path, "display." + key)

    layout = data.get("layout")
    if layout is not None:
        _need_table(layout, "layout", path)
        for key, value in layout.items():
            if key == "panels":
                cfg.layout.panels = _panels(value, "layout.panels", path)
            else:
                _unknown(path, "layout." + key)

    poll = data.get("poll")
    if poll is not None:
        _need_table(poll, "poll", path)
        for key, value in poll.items():
            if key == "interval":
                cfg.poll.interval = _int_in(value, "poll.interval", path, INTERVAL_MIN)
            elif key == "redraw":
                cfg.poll.redraw = _int_in(value, "poll.redraw", path, REDRAW_MIN)
            else:
                _unknown(path, "poll." + key)

    pacing = data.get("pacing")
    if pacing is not None:
        _need_table(pacing, "pacing", path)
        for key, value in pacing.items():
            if key == "tight":
                cfg.pacing.tight = _float_in(value, "pacing.tight", path, 0, 100)
            elif key == "on_track":
                cfg.pacing.on_track = _float_in(value, "pacing.on_track", path, 0, 100)
            elif key == "sleep":
                _need_table(value, "pacing.sleep", path)
                cfg.pacing.sleep = _build_sleep(value, path)
            else:
                _unknown(path, "pacing." + key)

    providers = data.get("providers")
    if providers is not None:
        _need_table(providers, "providers", path)
        for key, value in providers.items():
            if key not in PROVIDERS:
                _unknown(path, "providers." + key)
                continue
            sub_key = "providers." + key
            _need_table(value, sub_key, path)
            for sub, sub_value in value.items():
                if sub == "enabled":
                    setattr(cfg.providers, key, _bool(sub_value, sub_key + ".enabled", path))
                elif sub == "token_refresh":
                    if key in TOKEN_REFRESH_PROVIDERS:
                        setattr(cfg.providers, key + "_token_refresh", _token_refresh(
                            sub_value, sub_key + ".token_refresh", path))
                    else:
                        _unknown(path, "%s.%s" % (sub_key, sub))
                elif sub == "windows":
                    items = _windows_list(sub_value, sub_key + ".windows", path)
                    if items:  # 空リスト (= 全部) は「未指定」と同じ扱いにする
                        cfg.providers.windows[key] = items
                elif sub == "color":
                    cfg.providers.colors[key] = _color(sub_value, sub_key + ".color", path)
                else:
                    _unknown(path, "%s.%s" % (sub_key, sub))

    routing = data.get("routing")
    if routing is not None:
        _need_table(routing, "routing", path)
        for key, value in routing.items():
            if key == "enabled":
                cfg.routing.enabled = _bool(value, "routing.enabled", path)
            elif key == "floor_percent":
                cfg.routing.floor_percent = _float_in(value, "routing.floor_percent", path, 0, 100)
            elif key == "floor_hours":
                hours = _float_in(value, "routing.floor_hours", path, 0)
                if not math.isfinite(hours):
                    raise ConfigError(t("config.float_required", path=path,
                                        name="routing.floor_hours", range=_range_txt(0, None), value=value))
                cfg.routing.floor_hours = hours
            elif key == "stale_minutes":
                cfg.routing.stale_minutes = _int_in(value, "routing.stale_minutes", path, 1)
            elif key == "short_over_margin_minutes":
                if value is None:  # config --json の既定値
                    cfg.routing.short_over_margin_minutes = None
                    continue
                margin = _float_in(value, "routing.short_over_margin_minutes", path, 0)
                if not math.isfinite(margin) or margin <= 0:
                    raise ConfigError(t("config.float_required", path=path,
                                        name="routing.short_over_margin_minutes",
                                        range="> 0", value=value))
                cfg.routing.short_over_margin_minutes = margin
            elif key == "workers":
                cfg.routing.workers = _worker_map(value, path)
            else:
                _unknown(path, "routing." + key)
    return cfg


def _panels(value: Any, key: str, path: Path) -> List[Panel]:
    """[[layout.panels]] の配列を検証して Panel のリストにする。

    上限: 列は最大 MAX_PANEL_COLUMNS (large 1 枚 / 連続する small 2 枚で 1 列)。
    main = "" の空き枠は size = "small" のときだけ可 (render.group_columns と
    同じ規則で列に組み立てて数える)。
    """
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise ConfigError(t("config.panels_not_array", path=path, name=key))
    panels = [_panel(item, "%s[%d]" % (key, i), path) for i, item in enumerate(value)]
    ncols = len(group_columns(panels))
    if ncols > MAX_PANEL_COLUMNS:
        raise ConfigError(t("config.too_many_columns", path=path, name=key,
                            max=MAX_PANEL_COLUMNS, actual=ncols))
    return panels


def _panel(table: Any, key: str, path: Path) -> Panel:
    """[[layout.panels]] の 1 要素を検証して Panel にする。main は必須
    (size = "small" の空き枠 main = "" を除く。size を読んでから判定する)。"""
    panel = Panel(main="", subs=None)
    for sub, value in table.items():
        if sub == "main":
            if not isinstance(value, str):
                raise ConfigError(t("config.panel_main_not_string", path=path,
                                    name=key + ".main", value=value))
            panel.main = value.strip()
        elif sub == "subs":
            if value is None:
                continue  # JSON null = automatic, same as leaving it out
            panel.subs = _windows_list(value, key + ".subs", path,
                                       example='subs = ["claude:5h"]')
        elif sub == "size":
            panel.size = _size(value, key + ".size", path)
        else:
            _unknown(path, key + "." + sub)
    if not panel.main:
        if panel.size == "small":
            return panel  # main = "" の small は空き枠: その段は空のまま位置を保つ
        raise ConfigError(t("config.panel_main_required", path=path, name=key))
    return panel


def _build_night(table: Dict[str, Any], path: Path) -> Night:
    night = Night()
    for key, value in table.items():
        if key == "start":
            night.start = _time(value, "display.night.start", path)
        elif key == "end":
            night.end = _time(value, "display.night.end", path)
        elif key == "brightness":
            night.brightness = _int_in(value, "display.night.brightness", path, 0, 100)
        else:
            _unknown(path, "display.night." + key)
    return night


def _build_sleep(table: Dict[str, Any], path: Path) -> Sleep:
    """[pacing.sleep]: 睡眠帯。start と end は必須、start == end は長さ 0
    なのでエラーにする。"""
    sleep = Sleep()
    seen: Set[str] = set()
    for key, value in table.items():
        if key == "start":
            sleep.start = _time(value, "pacing.sleep.start", path)
            seen.add(key)
        elif key == "end":
            sleep.end = _time(value, "pacing.sleep.end", path)
            seen.add(key)
        elif key == "skip":
            sleep.skip = _skip_list(value, path)
        elif key == "advice":
            if not isinstance(value, str) or value not in SLEEP_ADVICES:
                raise ConfigError(t("config.choice_required", path=path,
                                    name="pacing.sleep.advice",
                                    choices=" / ".join('"%s"' % a for a in SLEEP_ADVICES),
                                    value=value))
            sleep.advice = value
        else:
            _unknown(path, "pacing.sleep." + key)
    for required in ("start", "end"):
        if required not in seen:
            raise ConfigError(t("config.sleep_time_required", path=path,
                                name="pacing.sleep." + required))
    if sleep.start == sleep.end:
        raise ConfigError(t("config.sleep_same_time", path=path))
    return sleep


def _skip_list(value: Any, path: Path) -> List[str]:
    """pacing.sleep.skip: 睡眠を適用しない provider id の配列。"""
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(t("config.list_required", path=path, name="pacing.sleep.skip",
                            example='skip = ["glm"]'))
    items = [v.strip() for v in value]
    for item in items:
        if item not in SLEEP_PROVIDERS:
            raise ConfigError(t("config.choice_required", path=path,
                                name="pacing.sleep.skip",
                                choices=" / ".join('"%s"' % p for p in SLEEP_PROVIDERS),
                                value=item))
    return items


def _worker_map(value: Any, path: Path) -> Dict[str, List[str]]:
    """routing.workers: ワーカー名 → 判定に使う窓 id の配列 (末尾 * は前方一致)。

    値に part_of 付きの内訳窓を書いても判定に入らない (gate 側で除外)。
    空リストは「どの窓も使わない」になってしまうのでエラーにする。
    """
    if not isinstance(value, dict) or any(not isinstance(k, str) or not k.strip()
                                          for k in value):
        raise ConfigError(t("config.workers_required", path=path, value=value))
    out: Dict[str, List[str]] = {}
    for name, ids in value.items():
        items = _windows_list(ids, "routing.workers.%s" % name.strip(), path,
                              example='["glm:5h", "glm:weekly"] または ["grok:*"]')
        if not items:
            raise ConfigError(t("config.workers_empty", path=path, name=name.strip()))
        out[name.strip()] = items
    return out


# -------------------------------------------------------- value validators

def _unknown(path: Path, key: str) -> None:
    log.warning("config: %s: 未知のキー '%s' を無視します", path, key)


def _need_table(value: Any, key: str, path: Path) -> None:
    if not isinstance(value, dict):
        raise ConfigError(t("config.need_table", path=path, name=key, value=value))


def _range_txt(lo: float, hi: Optional[float]) -> str:
    if hi is None:
        return t("config.range_min", lo=lo)
    return t("config.range_between", lo=lo, hi=hi)


def _int_in(value: Any, key: str, path: Path, lo: int, hi: Optional[int] = None) -> int:
    bad = isinstance(value, bool) or not isinstance(value, int) or value < lo \
        or (hi is not None and value > hi)
    if bad:
        raise ConfigError(t("config.int_required", path=path, name=key,
                            range=_range_txt(lo, hi), value=value))
    return value


def _float_in(value: Any, key: str, path: Path, lo: float, hi: Optional[float] = None) -> float:
    bad = isinstance(value, bool) or not isinstance(value, (int, float)) or value < lo \
        or (hi is not None and value > hi)
    if bad:
        raise ConfigError(t("config.float_required", path=path, name=key,
                            range=_range_txt(lo, hi), value=value))
    return float(value)


def _bool(value: Any, key: str, path: Path) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(t("config.bool_required", path=path, name=key, value=value))
    return value


def _windows_list(value: Any, key: str, path: Path,
                  example: str = 'windows = ["claude:weekly"]') -> List[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(t("config.list_required", path=path, name=key, example=example))
    items = [v.strip() for v in value]
    if any(not v for v in items):
        raise ConfigError(t("config.empty_string", path=path, name=key))
    return items


def _choice(value: Any, key: str, path: Path, choices) -> str:
    if value not in choices:
        raise ConfigError(t("config.choice_required", path=path, name=key,
                            choices=" / ".join('"%s"' % c for c in choices), value=value))
    return value


def _size(value: Any, key: str, path: Path) -> str:
    return _choice(value, key, path, SIZES)


def _color(value: Any, key: str, path: Path) -> str:
    if (not isinstance(value, str) or len(value) != 7 or value[0] != "#"
            or any(c not in "0123456789abcdefABCDEF" for c in value[1:])):
        raise ConfigError(t("config.color_required", path=path, name=key, value=value))
    return value


def _gauge(value: Any, key: str, path: Path) -> str:
    return _choice(value, key, path, GAUGES)


def _token_refresh(value: Any, key: str, path: Path) -> str:
    return _choice(value, key, path, TOKEN_REFRESH_MODES)


def _separator(value: Any, key: str, path: Path) -> str:
    if not isinstance(value, str) or len(value) > SEPARATOR_MAX:
        raise ConfigError(t("config.separator_required", path=path, name=key,
                            value=value, max=SEPARATOR_MAX))
    return value


def _language(value: Any, key: str, path: Path) -> str:
    return _choice(value, key, path, LANGUAGE_CHOICES)


def _time(value: Any, key: str, path: Path) -> time:
    if not isinstance(value, str):
        raise ConfigError(t("config.time_required", path=path, name=key, value=value))
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        raise ConfigError(t("config.time_required", path=path, name=key, value=value)) from None


# ------------------------------------------------------------------ helpers

def brightness_for(cfg: Config, now: Optional[datetime] = None) -> int:
    """その時刻に適用すべき明るさ（夜間帯なら night.brightness、それ以外は display.brightness）。"""
    if now is None:
        now = datetime.now().astimezone()
    night = cfg.display.night
    if night is not None and night.is_night(now):
        return night.brightness
    return cfg.display.brightness


def sleep_for(sleep: Optional[Sleep], provider: str) -> Optional[Tuple[time, time]]:
    """provider のペース判定に使う睡眠帯 (start, end)。

    `sleep` は cfg.pacing.sleep（None = 未設定）。provider が skip に入って
    いれば None（壁時計のままの判定 = 従来どおり）。
    """
    if sleep is None or provider in sleep.skip:
        return None
    return (sleep.start, sleep.end)


def wants_bedtime(sleep: Optional[Sleep], provider: str) -> bool:
    """provider の表示に advice = "bedtime" を効かせるか。

    `sleep` は cfg.pacing.sleep（None = 未設定）。advice が "bedtime" で、
    provider が skip に入っていないときだけ True（compute_pace の bedtime 引数）。
    """
    return sleep is not None and sleep.advice == "bedtime" \
        and provider not in sleep.skip


def format_toml(cfg: Config) -> str:
    """実際に使われる設定（既定値込み）を TOML 風の文字列にする。

    出力は load_config で読み戻せる（config --set-json の保存形式）。
    """
    d = cfg.display
    lines = [
        "# quota-canvas: 実際に使用中の設定（既定値込み）",
        "# この内容をそのまま保存して使えます: %s" % DEFAULT_PATH,
        "",
        "[display]",
        '# 表示する窓 ID を順番に書く。空 [] なら全部を既定の並びで表示。',
        'windows = %s' % json.dumps(d.windows, ensure_ascii=False),
        "brightness = %d" % d.brightness,
        '# ゲージの向き: "remaining" = 残りで表示 / "used" = 使用量で表示',
        'gauge = "%s"' % d.gauge,
        "# true なら % の数字を利用状況の色にする（超過・枯渇は赤、余裕ありは緑）。",
        "# false なら全部白",
        "percent_colors = %s" % ("true" if d.percent_colors else "false"),
        '# 文の区切り (例 "／", " · ")。"" なら表示言語の既定（日本語「／」、英語「 · 」）',
        'separator = %s' % json.dumps(d.separator, ensure_ascii=False),
        '# 表示言語: "auto" = macOS の言語設定に従う / "ja" = 日本語 / "en" = 英語',
        'language = "%s"' % d.language,
        "# true の間は画面への描画を止める（メニューバーアプリの「一時停止」）",
        "paused = %s" % ("true" if d.paused else "false"),
        "# true なら Mac のスリープ（ディスプレイ/システム）に合わせて画面を黒 1 枚と",
        "# 明るさ 0 で消灯し、復帰したら明るさと表示を戻す",
        "sleep_with_mac = %s" % ("true" if d.sleep_with_mac else "false"),
    ]
    if d.night is not None:
        lines += [
            "",
            "[display.night]",
            'start = "%s"' % d.night.start.strftime("%H:%M"),
            'end = "%s"' % d.night.end.strftime("%H:%M"),
            "brightness = %d" % d.night.brightness,
        ]
    else:
        lines += [
            "",
            "# [display.night] は未設定（夜間減光は無効）。使うなら次の 4 行の # を外す:",
            '# [display.night]',
            '# start = "23:00"',
            '# end = "07:00"',
            '# brightness = 20',
        ]
    lines += [
        "",
        "# 画面の配置 ([[layout.panels]])。未指定なら取得できたサービスを自動配置します。",
        '# main = メインの窓 ID ("claude:weekly") かサービス ID ("gemini")。'
        'main = "" は size = "small" のときだけ可（その段は空き枠になる）',
        '# subs = 大きいパネルの下に並べるサブの窓 ID (large のみ)',
        '# size = "large" (大きいパネル、1 枚で 1 列) | "small" (小さいパネル、'
        '連続する 2 枚で 1 列の上下 2 段)',
        '# 上限: 列は最大 4 つ（全部 small なら最大 8 枚）。',
    ]
    if cfg.layout.panels:
        for p in cfg.layout.panels:
            lines += [
                "[[layout.panels]]",
                'main = "%s"' % p.main,
            ]
            if p.subs is None:
                lines.append("# subs を省略: 同じサービスの残りの窓を自動でサブに並べる")
            else:
                lines += [
                    "# サブに並べる窓 ID。空 [] ならサブなし",
                    "subs = %s" % json.dumps(p.subs, ensure_ascii=False),
                ]
            lines.append('size = "%s"' % p.size)
    else:
        lines += [
            "# [[layout.panels]]",
            '# main = "claude:weekly"',
            '# subs = ["claude:5h"]',
            '# size = "large"',
        ]
    lines += [
        "",
        "[poll]",
        "interval = %d" % cfg.poll.interval,
        "redraw = %d" % cfg.poll.redraw,
        "",
        "[pacing]",
        "tight = %.1f" % cfg.pacing.tight,
        "on_track = %.1f" % cfg.pacing.on_track,
    ]
    if cfg.pacing.sleep is not None:
        s = cfg.pacing.sleep
        lines += [
            "# 睡眠帯: ペース判定（over / tight / on_track / spare）を「起きている時間」で",
            "# 判定する。寝る前（start）までに枠を使い切れるかの目安になる。",
            "# start > end なら日付をまたぐ。skip に入れた provider には適用しない。",
            '# advice = "bedtime" なら、寝る時刻がリセットより先のときアドバイス行の',
            "# 残り時間を「寝るまで」で出す（既定の \"reset\" はリセット基準のまま）。",
            "[pacing.sleep]",
            'start = "%s"' % s.start.strftime("%H:%M"),
            'end = "%s"' % s.end.strftime("%H:%M"),
            "skip = %s" % json.dumps(s.skip, ensure_ascii=False),
            'advice = "%s"' % s.advice,
        ]
    else:
        lines += [
            "",
            "# [pacing.sleep] は未設定（ペース判定は壁時計どおり）。使うなら次の 5 行の # を外す:",
            '# [pacing.sleep]',
            '# start = "00:00"',
            '# end = "08:00"',
            '# skip = ["glm"]',
            '# advice = "bedtime"',
        ]
    lines += [
        "",
        "[routing]",
        "# 残量に応じた AI ワーカー振り分け判定 (`quota-canvas routing line|check`)。",
        "# true にしない限り何も出しません (既定オフ)。status.json への全窓 (windows) の",
        "# 書き出しはこの設定に関係なく行われます。",
        "enabled = %s" % ("true" if cfg.routing.enabled else "false"),
        "# floor の適用中に残りがこの % 未満なら stop",
        "floor_percent = %g" % cfg.routing.floor_percent,
        "# 24 時間以上または長さ不明の窓でリセットまでこの時間以上なら floor を適用",
        "# 0 なら全窓で常に適用。リセット時刻不明も適用",
        "floor_hours = %r" % cfg.routing.floor_hours,
        "# fetched_at がこの分数より古い窓を含むワーカーは stale (判定できない扱い)",
        "stale_minutes = %d" % cfg.routing.stale_minutes,
        "# 24 時間未満の窓の over 猶予と floor を外す終盤の長さ (分)。未指定なら窓長の 10%",
        ("short_over_margin_minutes = %r" % cfg.routing.short_over_margin_minutes
         if cfg.routing.short_over_margin_minutes is not None else "# short_over_margin_minutes = 30"),
    ]
    if cfg.routing.workers:
        lines += [
            "",
            "# ワーカー名 → 判定に使う窓 id の配列。順番が routing line の表示順。",
            '# 末尾に "*" を付けると前方一致 (例 "grok:*")。内訳窓 (part_of 付き) は',
            "# どの指定でも判定に入らない。未指定なら取得できている provider ごとに",
            "# その provider 名で独立窓 (part_of なし) すべて。",
            "[routing.workers]",
        ]
        for name, ids in cfg.routing.workers.items():
            lines.append("%s = %s" % (name, json.dumps(ids, ensure_ascii=False)))
    else:
        lines += [
            "",
            "# ワーカーごとに使う窓を決めるなら例のように書く (未指定なら provider 名で自動):",
            '# [routing.workers]',
            '# glm = ["glm:5h", "glm:weekly"]',
            '# argon = ["gemini:argon"]',
        ]
    for name in PROVIDERS:
        color = cfg.providers.colors.get(name)
        lines += [
            "",
            "[providers.%s]" % name,
            "enabled = %s" % ("true" if getattr(cfg.providers, name) else "false"),
        ]
        if name == "claude":
            lines += [
                "# OAuth トークンが期限切れのときの更新方法:",
                '#   "cli" = `claude -p` を軽く 1 回実行して Claude Code CLI 自身に更新させる'
                "（30 分に 1 回まで）",
                '#   "off" = 自動更新しない（401/403 は「claude を一度起動してください」のエラーに）',
                'token_refresh = "%s"' % cfg.providers.claude_token_refresh,
            ]
        elif name == "codex":
            lines += [
                "# OAuth トークンが期限切れのときの更新方法:",
                '#   "cli" = `codex exec` を最小のプロンプトで 1 回実行して Codex CLI 自身に'
                "更新させる（30 分に 1 回まで）",
                '#   "off" = 自動更新しない（401/403 は従来どおり HTTP エラーのまま）',
                'token_refresh = "%s"' % cfg.providers.codex_token_refresh,
            ]
        elif name == "grok":
            lines += [
                "# ログイン トークンが期限切れのときの更新方法:",
                '#   "cli" = `grok models`（モデルを呼ばない）を 1 回実行して Grok CLI 自身に'
                "更新させる（30 分に 1 回まで）",
                '#   "off" = 自動更新しない（期限切れは従来どおりのエラー、401 は HTTP エラーのまま）',
                'token_refresh = "%s"' % cfg.providers.grok_token_refresh,
            ]
        elif name == "gemini":
            lines += [
                "# 利用枠の取得は Antigravity CLI の `agy -p \"/usage\"`（モデルを呼ばない）で、",
                "# トークンの更新も agy 自身が行うため、この設定は現在使われません。",
                '# 過去の設定ファイルとの互換で "cli" / "off" を受け付けるだけです。',
                'token_refresh = "%s"' % cfg.providers.gemini_token_refresh,
            ]
        lines += [
            "# このサービスで表示する窓 key（detect --json の windows[].key）。空 [] なら全部",
            "windows = %s" % json.dumps(cfg.providers.windows.get(name) or [], ensure_ascii=False),
        ]
        if color:
            lines += [
                "# このサービスのテーマ色（バーとドットの色）",
                'color = "%s"' % color,
            ]
        else:
            lines += ["# テーマ色を既定から変えるなら例のように書く: color = \"#D97757\""]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------- dict ⇄ Config / 保存

JSON_SOURCE = "config --set-json"  # JSON 検証エラーのメッセージに使うラベル


def config_to_dict(cfg: Config) -> Dict[str, Any]:
    """実際に使う設定を、TOML と同じ形の dict にする (config --json 用)。

    night と pacing.sleep は未設定なら absent。
    """
    display: Dict[str, Any] = {
        "paused": cfg.display.paused,
        "sleep_with_mac": cfg.display.sleep_with_mac,
        "gauge": cfg.display.gauge,
        "percent_colors": cfg.display.percent_colors,
        "separator": cfg.display.separator,
        "language": cfg.display.language,
        "windows": list(cfg.display.windows),
        "brightness": cfg.display.brightness,
    }
    if cfg.display.night is not None:
        display["night"] = {
            "start": cfg.display.night.start.strftime("%H:%M"),
            "end": cfg.display.night.end.strftime("%H:%M"),
            "brightness": cfg.display.night.brightness,
        }
    pacing: Dict[str, Any] = {"tight": cfg.pacing.tight, "on_track": cfg.pacing.on_track}
    if cfg.pacing.sleep is not None:
        pacing["sleep"] = {  # 未設定なら absent (night と同じ流儀)
            "start": cfg.pacing.sleep.start.strftime("%H:%M"),
            "end": cfg.pacing.sleep.end.strftime("%H:%M"),
            "skip": list(cfg.pacing.sleep.skip),
            "advice": cfg.pacing.sleep.advice,
        }
    return {
        "display": display,
        "layout": {
            "panels": [
                {"main": p.main,
                 "subs": None if p.subs is None else list(p.subs),
                 "size": p.size}
                for p in cfg.layout.panels
            ],
        },
        "poll": {"interval": cfg.poll.interval, "redraw": cfg.poll.redraw},
        "pacing": pacing,
        "providers": {
            name: _provider_entry(cfg, name) for name in PROVIDERS
        },
        "routing": {
            "enabled": cfg.routing.enabled,
            "floor_percent": cfg.routing.floor_percent,
            "floor_hours": cfg.routing.floor_hours,
            "stale_minutes": cfg.routing.stale_minutes,
            "short_over_margin_minutes": cfg.routing.short_over_margin_minutes,
            "workers": {name: list(ids) for name, ids in cfg.routing.workers.items()},
        },
    }


def _provider_entry(cfg: Config, name: str) -> Dict[str, Any]:
    """providers.<p> 1 件ぶんの dict。token_refresh は claude / codex / grok /
    gemini、color は未指定なら absent。"""
    entry: Dict[str, Any] = {
        "enabled": getattr(cfg.providers, name),
        "windows": list(cfg.providers.windows.get(name) or []),
    }
    if name in TOKEN_REFRESH_PROVIDERS:
        entry["token_refresh"] = getattr(cfg.providers, name + "_token_refresh")
    color = cfg.providers.colors.get(name)
    if color:
        entry["color"] = color
    return entry


def config_from_dict(data: Any, source: str = JSON_SOURCE) -> Config:
    """app から送られた設定 JSON (TOML と同じ形) を検証して Config にする。

    不正なら ConfigError（メッセージはそのまま app に返せる）。未知のキーは
    TOML と同じく警告して無視する。
    """
    if not isinstance(data, dict):
        raise ConfigError(t("config.not_object", source=source, value=data))
    return _build(data, Path(source))


def save_config(cfg: Config, path: Optional[PathLike] = None) -> Path:
    """format_toml の内容を設定ファイルに原子的に書き込む（一時ファイル → rename）。

    書き込んだパスを返す。
    """
    target = Path(path).expanduser() if path is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(format_toml(cfg), encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target
