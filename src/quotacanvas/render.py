"""PIL renderer: AI-service quota + pace panels for the 1920x462 TURZX canvas.

Depends only on Pillow and the shared dataclasses in ``quotacanvas.models``.
Pacing lives in ``pacing``; the caller computes one ``Pace`` per window and
passes ``(ProviderUsage, Pace)`` pairs already sorted in display order.
A provider whose fetch failed is passed with ``pace=None`` and is drawn as a
compact gray error panel.

Layout is slot based (``PanelSpec``): panels are assembled into at most 4
equal-width columns (``group_columns`` -- the same rule the config validator
uses). A large panel fills one column; a small panel goes into the bottom
row of the previous column when that column is a half-filled small column,
otherwise it starts a new column as its top row. A small slot with
``main=""`` (or an id that cannot be resolved) stays empty and keeps its
position. A panel is one main frame (a Window + its Pace) plus up to 3 sub
frames (large panels only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from .platform_support import appearance
from .i18n import t
from .models import Balance, Pace, PaceStatus, ProviderUsage, Window

__all__ = ["render", "render_error", "advice_text", "auto_layout", "group_columns",
           "PanelSpec", "BRAND_COLORS", "CANVAS_W", "CANVAS_H",
           "MAX_COLUMNS", "MAX_SERVICES"]


@dataclass
class PanelSpec:
    """Placement of one panel, as coming from the config."""

    main: str               # window id ("claude:weekly") or provider id
                            # ("gemini"; error display / auto main pick)
    subs: List[str] = field(default_factory=list)   # window ids, large only
    size: str = "large"     # "large" | "small"

CANVAS_W = 1920
CANVAS_H = 462

# --------------------------------------------------------------- palette ----

BG = (11, 13, 18)          # #0B0D12
CARD_BG = (21, 24, 33)     # #151821
TRACK = (38, 43, 56)       # empty bar track
DIVIDER = (37, 42, 55)
TEXT = (233, 236, 244)
TEXT_DIM = (154, 160, 172)
TEXT_FAINT = (108, 114, 126)
WHITE = (246, 248, 252)
BLACK = (0, 0, 0)

RED = (240, 97, 109)       # OVER / EXHAUSTED
YELLOW = (250, 178, 25)    # burst (直近の加速) の警告 / 取得失敗中表示
PURPLE = (116, 102, 224)   # ON_TRACK / TIGHT
GREEN = (163, 230, 53)     # SPARE  #A3E635 (lime; distinct from codex green)
GRAY = (138, 143, 152)     # UNKNOWN

# Per-service theme colors used for gauge fills and the name dot.
# State colors stay reserved for badge / advice / left band / prediction hatch
# (and red / green on the % numbers when over / spare,
# unless percent_colors is off).
BRAND_COLORS = {
    "claude": "#D97757",
    "codex": "#10A37F",
    "glm": "#3B82F6",
    "grok": "#E5E7EB",
    "gemini": "#8E75F0",
    "openrouter": "#8B5CF6",
    "deepseek": "#4D6BFE",
    "kimi": "#18A999",
}
_BRAND_DEFAULT = (154, 163, 175)  # #9CA3AF for unknown services


def _hex_color(h):
    # type: (str) -> tuple
    """'#RRGGBB' (or 'RRGGBB') -> (r, g, b)."""
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# 内訳 (Window.part_of 付きの窓) の固定パレット。parts の並び順で割り当て、
# 足りなくなったら先頭に戻って繰り返す。
_PART_COLORS = [_hex_color(h) for h in ("#A78BFA", "#F59E0B", "#22D3EE")]


def _brand_color(colors, provider):
    # type: (Optional[dict], str) -> tuple
    """Theme color for `provider`: caller override first, then BRAND_COLORS,
    then the neutral gray default."""
    c = (colors or {}).get(provider) or BRAND_COLORS.get(provider)
    if c is None:
        return _BRAND_DEFAULT
    if isinstance(c, str):
        return _hex_color(c)
    return tuple(c)

# 描画時に言語が決まる文字列はモジュール読み込み時に確定させない:
# 定数は文言キーだけ持ち、描画時に t() で解決する
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
           "sep", "oct", "nov", "dec")


def _weekday(dt):
    # type: (datetime) -> str
    """ローカル曜日名 (ja: 月..日 / en: Mon..Sun)。"""
    return t("render.weekday." + _WEEKDAYS[dt.weekday()])


_STATUS_COLOR = {
    PaceStatus.OVER: RED,
    PaceStatus.EXHAUSTED: RED,
    PaceStatus.TIGHT: PURPLE,  # 予測 92-100%: 余った枠をほぼ使い切れる。警告ではないので on_track と同じ紫
    PaceStatus.ON_TRACK: PURPLE,
    PaceStatus.SPARE: GREEN,
    PaceStatus.FRESH: GREEN,   # リセット直後で枠が開いている: spare と同じ緑系
    PaceStatus.UNKNOWN: GRAY,
}
# 値は文言キー (render.badge.*)。status.py も同じキーを使う
_STATUS_BADGE = {
    PaceStatus.OVER: "render.badge.over",
    PaceStatus.EXHAUSTED: "render.badge.exhausted",
    PaceStatus.TIGHT: "render.badge.tight",
    PaceStatus.ON_TRACK: "render.badge.on_track",
    PaceStatus.SPARE: "render.badge.spare",
    PaceStatus.FRESH: "render.badge.fresh",
    PaceStatus.UNKNOWN: "render.badge.unknown",
}


# 残り% (使用%) の数字の色: 超過・枯渇は赤、余裕ありは緑、
# それ以外 (tight / on_track / fresh / unknown) は白。
# render(percent_colors=False) なら全部白
_NUM_COLOR = {
    PaceStatus.OVER: RED,
    PaceStatus.EXHAUSTED: RED,
    PaceStatus.SPARE: GREEN,
}


def _num_color(s, st, default=WHITE):
    # type: (_S, PaceStatus, tuple) -> tuple
    if not s.percent_colors:
        return default
    return _NUM_COLOR.get(st, default)


def _badge(st):
    # type: (PaceStatus) -> str
    """ステータスバッジの文言 (現在の言語)。"""
    return t(_STATUS_BADGE[st])


def _badge_short(st):
    # type: (PaceStatus) -> str
    """サブ行の状態の短い文言 (全角 2 文字)。"""
    return t(_STATUS_BADGE[st].replace("badge.", "badge_short.", 1))


def _sub_status(st):
    # type: (PaceStatus) -> PaceStatus
    """サブ行ではリセット直後を余裕ありとして扱う (文言・色とも)。"""
    return PaceStatus.SPARE if st == PaceStatus.FRESH else st

# ----------------------------------------------------------------- fonts ----

_font_cache = {}  # type: dict


def F(weight, size):
    # type: (str, int) -> ImageFont.FreeTypeFont
    """Load a Hiragino Kaku Gothic face, cached."""
    key = (weight, size)
    font = _font_cache.get(key)
    if font is not None:
        return font
    font = None
    for path in appearance.font_paths(weight):
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# ---------------------------------------------------------------- painter ---

#: 文言の中の区切り。描くときに render(separator=...) へ置き換える。
SEP = " · "


def default_separator():
    # type: () -> str
    """表示言語の既定の区切り (日本語「／」、英語「 · 」)。"""
    return t("render.separator")


class _S(object):
    """ImageDraw wrapper applying a global pixel offset (burn-in shift)."""

    def __init__(self, img, offset, scale=1.0):
        # type: (Image.Image, Tuple[int, int], float) -> None
        self.d = ImageDraw.Draw(img)
        self.percent_colors = True  # % の数字を状態色にするか (render が設定)
        self.sep = None  # 文の区切り。None なら文言の " · " のまま (render が設定)
        self.ox = int(offset[0])
        self.oy = int(offset[1])
        self.scale = scale
        self.scaled_fonts = {}

    def _font(self, font):
        if self.scale == 1.0 or not hasattr(font, "font_variant"):
            return font
        if font not in self.scaled_fonts:
            self.scaled_fonts[font] = font.font_variant(size=max(1, round(font.size * self.scale)))
        return self.scaled_fonts[font]

    def _p(self, xy):
        n = len(xy)
        if n == 2:
            return ((xy[0] + self.ox) * self.scale, (xy[1] + self.oy) * self.scale)
        return tuple((v + (self.ox if i % 2 == 0 else self.oy)) * self.scale
                     for i, v in enumerate(xy))

    def _b(self, box):
        return self._p(box)

    def _t(self, txt):
        """文言の区切り " · " (先頭の "· " も) を self.sep に置き換える。"""
        if self.sep is None or self.sep == SEP:
            return txt
        if txt.startswith(SEP.lstrip()):
            return self.sep.lstrip() + txt[len(SEP.lstrip()):].replace(SEP, self.sep)
        return txt.replace(SEP, self.sep)

    def lead_gap(self, gap):
        """"· 週間" のように区切りで始まる文字列の前の余白。区切りが空白で
        始まらない (全角「／」など) ときは文字自体に余白があるので詰める。"""
        if self.sep is None or self.sep.startswith(" "):
            return gap
        return 2

    def text(self, xy, txt, font, fill, anchor=None):
        self.d.text(self._p(xy), self._t(txt), font=self._font(font), fill=fill,
                    anchor=anchor)

    def textlength(self, txt, font):
        return self.d.textlength(self._t(txt), font=self._font(font)) / self.scale

    def line(self, box, fill, width=1):
        self.d.line(self._p(box), fill=fill, width=max(1, round(width * self.scale)))

    def rectangle(self, box, fill=None, outline=None, width=1):
        self.d.rectangle(self._b(box), fill=fill, outline=outline, width=max(1, round(width * self.scale)))

    def polygon(self, pts, fill=None, outline=None):
        self.d.polygon([self._p(p) for p in pts], fill=fill, outline=outline)

    def rrect(self, box, radius=0, fill=None, outline=None, width=1, corners=None):
        try:
            self.d.rounded_rectangle(self._b(box), radius=radius * self.scale, fill=fill,
                                     outline=outline, width=max(1, round(width * self.scale)), corners=corners)
        except TypeError:  # Pillow < 9.1 has no `corners`
            self.d.rounded_rectangle(self._b(box), radius=radius * self.scale, fill=fill,
                                     outline=outline, width=max(1, round(width * self.scale)))


def _text_bbox(s, xy, txt, font, anchor):
    # type: (_S, Tuple[float, float], str, ImageFont.FreeTypeFont, str) -> Tuple[float, float, float, float]
    """bbox of `txt` as s.text would draw it (global offset removed, so the
    result is in the caller's local coordinates)."""
    bb = s.d.textbbox(s._p(xy), s._t(txt), font=s._font(font),
                      anchor=anchor)
    return (bb[0] / s.scale - s.ox, bb[1] / s.scale - s.oy,
            bb[2] / s.scale - s.ox, bb[3] / s.scale - s.oy)


# ------------------------------------------------------------- primitives ---

def _mix(c1, c2, t):
    return tuple(int(round(c1[k] * (1.0 - t) + c2[k] * t)) for k in range(3))


def _dim(color, t=0.55):
    """Blend a state color toward the background."""
    return _mix(color, BG, t)


def _on(color):
    """Readable text color on top of `color`."""
    luma = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    return (20, 21, 26) if luma > 150 else WHITE


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _fit_font(s, text, weight, size, max_w, min_size=12):
    # type: (_S, str, str, int, float, int) -> ImageFont.FreeTypeFont
    """Largest font <= `size` whose rendering of `text` fits `max_w`."""
    size = int(size)
    while size > min_size:
        font = F(weight, size)
        if s.textlength(text, font=font) <= max_w:
            return font
        size -= 1
    return F(weight, min_size)


def _fit_text(s, text, weight, size, max_w, min_size=11):
    # type: (_S, str, str, int, float, int) -> Tuple[ImageFont.FreeTypeFont, str]
    """Shrink, then ellipsize, so `text` fits `max_w`."""
    font = _fit_font(s, text, weight, size, max_w, min_size)
    if s.textlength(text, font=font) <= max_w:
        return font, text
    while len(text) > 1 and s.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return font, text + "…"


# ------------------------------------------------------------ text format ---

def _local(dt):
    return dt.astimezone() if dt is not None else None


def _fmt_clock(dt):
    """Header date and time in the display language."""
    return _fmt_dt(dt)


def _fmt_dt(dt):
    l = _local(dt)
    return t("render.date.full", weekday=_weekday(l),
             month=t("render.month." + _MONTHS[l.month - 1]), day=l.day,
             hour=l.hour, minute=l.minute)


def _fmt_span(td):
    """5日 2時間 / 2時間13分 / 45分"""
    if td is None:
        return "--"
    sec = max(0, int(td.total_seconds()))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return t("render.span.dh", d=d, h=h) if h > 0 \
            else t("render.span.d", d=d)
    if h > 0:
        return t("render.span.hm", h=h, m=m) if m > 0 \
            else t("render.span.h", h=h)
    return t("render.span.m", m=m)


def _fmt_num(x):
    s = "%.1f" % float(x)
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _round(x):
    return int(math.floor(float(x) + 0.5))


# --------------------------------------------------------- reset credits ---
# Codex の「リセット権」(週間枠を即リセットできる権利) の表示。閲覧のみで、
# 権利を使う (消費する) 操作はこのアプリには存在しない。

_CREDIT_MAX_ROWS = 2    # 詳しく並べるのは期限の近い 2 件まで (残りは「ほか N 回」、総数は見出し)


def _tz_label(now):
    # type: (datetime) -> str
    """見出しの括弧内に入れるローカルタイムゾーン略称 (例 "JST")。
    略称が取れないときは UTC±HH:MM。"""
    l = _local(now)
    try:
        name = l.tzname()
    except Exception:
        name = None
    if name:
        return str(name)
    off = l.utcoffset() or timedelta()
    minutes = int(round(off.total_seconds() / 60.0))
    return "UTC%+03d:%02d" % (minutes // 60, abs(minutes) % 60)


def _fmt_dt_short(dt):
    """10/30 03:45 (曜日なしの短い形、コンパクト表示用)"""
    l = _local(dt)
    return t("render.date.short", month=t("render.month." + _MONTHS[l.month - 1]),
             day=l.day, hour=l.hour, minute=l.minute)


def _fmt_time_short(dt, now=None):
    # type: (datetime, Optional[datetime]) -> str
    """_fmt_dt_short の日付省略形: `dt` が `now` と同じ日 (ローカル) なら
    "21:52" だけ、違う日なら "10/26 03:45"。burst の尽きる時刻など、今から
    数時間以内の見込み時刻の表示に使う (リセット権など既存の表示は変更しない)。"""
    l = _local(dt)
    if now is not None:
        ln = _local(now)
        if (l.year, l.month, l.day) == (ln.year, ln.month, ln.day):
            return "%02d:%02d" % (l.hour, l.minute)
    return _fmt_dt_short(dt)


def _credit_row(credit, now):
    # type: (object, datetime) -> Tuple[str, tuple]
    """1 権利ぶんの行 "(10/30 (木) 03:45 まで · あと 5日5時間", 色)。
    色: 残り 24 時間未満は赤、2 日未満は黄。"""
    if credit.expires_at is None:
        return t("render.credit.no_expiry"), TEXT_FAINT
    rem = credit.expires_at - now
    if rem < timedelta(hours=24):
        col = RED
    elif rem < timedelta(days=2):
        col = YELLOW
    else:
        col = TEXT
    return t("render.credit.row", dt=_fmt_dt(credit.expires_at),
             span=_fmt_span(rem)), col


def _credit_compact(credits):
    # type: (Sequence[object]) -> str
    """1 行に縮めた形 "リセット権 2回 · 最短 10/30 03:45"。"""
    txt = t("render.credit.count", n=len(credits))
    first = credits[0]
    if first.expires_at is not None:
        txt += " " + t("render.credit.next", dt=_fmt_dt_short(first.expires_at))
    return txt


def _credit_rows(credits, now):
    # type: (Sequence[object], datetime) -> List[Tuple[str, tuple]]
    """見出しの下に並べる行 (期限の近いものから最大 _CREDIT_MAX_ROWS 行 + 残り数)。"""
    rows = [_credit_row(c, now) for c in credits[:_CREDIT_MAX_ROWS]]
    rest = len(credits) - len(rows)
    if rest > 0:
        rows.append((t("render.credit.more", n=rest), TEXT_FAINT))
    return rows


def _draw_reset_credits(s, tx0, cw, gap_top, limit, credits, now):
    # type: (_S, float, float, float, float, Sequence[object], datetime) -> None
    """ゲージの下〜アドバイス行 (またはサブ行) の上の空き帯 [gap_top, limit]
    にリセット権を描く。見出し + 最大 2 行 + 「ほか N 回」が入らないときは
    1 行のコンパクト表示 ("リセット権 2回 · 最短 10/30 03:45") に縮める。"""
    rows = _credit_rows(credits, now)
    n_lines = 1 + len(rows)  # + 見出し
    avail = limit - gap_top
    lh = avail / float(n_lines)
    if lh < 19.0 or avail < 24.0:
        # 入りきらない: 見出し 1 行だけに縮める
        if avail >= 22.0:
            cf, shown = _fit_text(s, _credit_compact(credits), "w6", 15, cw,
                                  min_size=10)
            s.text((tx0, gap_top + 11.0), shown, font=cf,
                   fill=TEXT_DIM, anchor="lm")
        return
    lh = min(lh, 26.0)
    cy = gap_top + lh / 2.0   # 上詰め (サブ行と同じくゲージの直下から)
    head = t("render.credit.head", n=len(credits), tz=_tz_label(now))
    hf, head = _fit_text(s, head, "w6", 15, cw, min_size=11)
    s.text((tx0, cy), head, font=hf, fill=TEXT, anchor="lm")
    for txt, col in rows:
        cy += lh
        f, shown = _fit_text(s, txt, "w4", 14, cw, min_size=10)
        s.text((tx0, cy), shown, font=f, fill=col, anchor="lm")


# ------------------------------------------------------------ pace helpers --

def _elapsed_line(mp, now, total=True):
    """経過の表示 "1時間経過 / 7日" (total=False なら "1時間経過")。"0日" の
    ような空の単位は出さない (_fmt_span)。経過は壁時計 (pacing.sleep 設定時の
    elapsed_fraction は起きている時間ベースなので表示には使わない)。"""
    win = mp.window
    if win.resets_at is not None:
        elapsed = min(max(now - win.starts_at, timedelta(0)), win.length)
    else:
        elapsed = win.length * max(0.0, mp.elapsed_fraction)  # リセット不明: 従来どおり
    e = _fmt_span(elapsed)
    if not total:
        return t("render.elapsed.short", e=e)
    return t("render.elapsed.of", e=e, total=_fmt_span(win.length))

def _prev_line(mp, gauge):
    # type: (Pace, str) -> Optional[str]
    """F3: 前回の期間の結果の短い文言 ("前回 36% 余り" / gauge="used" なら
    "前回 64% 使用")。previous_used が無ければ None。"""
    prev = getattr(mp, "previous_used", None)
    if prev is None:
        return None
    if gauge == "used":
        return t("render.card.prev_used", pct=_round(prev))
    return t("render.card.prev_left", pct=_round(100.0 - prev))


def _sub_note(mp, subs, now=None):
    # type: (Pace, Sequence[dict], Optional[datetime]) -> Optional[str]
    """F2 後半: 同じサービスの独立サブ窓 (5時間など) が主窓より先に尽きる
    ときの注記 ("5時間枠が 15:30 に尽きる見込み")。複数あるときは最も早く
    尽きる (尽きている) ずつを 1 つだけ。内訳窓 (part_of 付き) と別サービス
    のサブは対象外。時刻は当日なら "21:52" 形式。"""
    win = mp.window
    best = None   # (尽きる時刻の目安, 文言)
    for sub in subs:
        sp = sub["pace"]
        sw = sp.window
        if sw.provider != win.provider or sw.part_of is not None:
            continue
        critical = sp.status in (PaceStatus.OVER, PaceStatus.EXHAUSTED)
        earlier = (sp.exhausts_at is not None
                   and (mp.exhausts_at is None or sp.exhausts_at < mp.exhausts_at))
        if not (critical or earlier):
            continue
        at = sp.exhausts_at or sw.resets_at
        if best is not None and at is not None and best[0] is not None \
                and at >= best[0]:
            continue
        if sp.exhausts_at is not None:
            txt = t("render.card.sub_exhaust_at", label=sub["label"],
                    dt=_fmt_time_short(sp.exhausts_at, now))
        else:
            txt = t("render.card.sub_exhausted", label=sub["label"])
        best = (at, txt)
    return best[1] if best is not None else None


def advice_color(pace):
    # type: (Pace) -> tuple
    """アドバイス行の色。F2 の「このペースだと … に尽きる見込み」
    (burst_exhausts_at、OVER 以外で出る) のときは spare / on_track / fresh
    でも黄 (YELLOW) で警告する。バッジ・帯・ゲージは status 色のまま。"""
    if pace.burst_exhausts_at is not None and pace.status not in (
            PaceStatus.OVER, PaceStatus.EXHAUSTED, PaceStatus.UNKNOWN):
        return YELLOW
    return _STATUS_COLOR[pace.status]


def _daily_allowance(pace):
    # type: (Pace) -> Optional[float]
    """1日以上の窓の表示用「1日あたり」の許容量。Pace.allowance_per_day
    (暦の 1 日あたり。pacing.sleep 設定のときは起きている時間基準の
    sustainable_per_day より小さくなる) を使う。未計算の Pace では
    sustainable_per_day に落ちる (sleep 未設定なら両者は同じ値)。"""
    if pace.allowance_per_day is not None:
        return pace.allowance_per_day
    return pace.sustainable_per_day


def _short_left_advice(head, pct, left_in, bedtime_in):
    # type: (str, int, Optional[timedelta], Optional[timedelta]) -> str
    """1日未満の窓 (5時間枠など) のアドバイス行: 「{head} · 残り91% · あと4時間」。
    1時間あたりの許容量ではなく残量と締切までの時間をそのまま出す。締切は
    advice = "bedtime" のとき「寝るまで{span}」、それ以外はリセット基準の
    「あと{span}」。"""
    key = "render.advice.short_left_bedtime" if bedtime_in is not None \
        else "render.advice.short_left"
    return t(key, head=head, pct=pct, span=_fmt_span(left_in))


def advice_text(pace, compact=False, now=None):
    # type: (Pace, bool, Optional[datetime]) -> str
    """One-line advice for a card. All wording lives here: compact=False is
    the big-card sentence, compact=True the short small-card one."""
    if now is None:
        now = datetime.now().astimezone()
    st = pace.status
    win = pace.window
    reset_in = (win.resets_at - now) if win.resets_at is not None else None
    # advice = "bedtime" で寝る時刻が締切 (Pace.bedtime_at) のときは、アドバイス
    # 行の残り時間を「寝るまで」に置き換える。EXHAUSTED の「リセットまで」と
    # カード上部のリセット表示はそのまま (ここでは last_unit / compact のみ)。
    bedtime_in = None
    if pace.bedtime_at is not None:
        bedtime_in = max(timedelta(0), pace.bedtime_at - now)
    left_in = bedtime_in if bedtime_in is not None else reset_in

    if st is PaceStatus.UNKNOWN:
        return t("render.advice.no_data")
    if st is PaceStatus.EXHAUSTED:
        if reset_in is not None:
            return t("render.advice.exhausted_reset", span=_fmt_span(reset_in))
        return _badge(st)

    # F2: 直近 2 時間の加速でリセット前に尽きる見込み。OVER のときは通常の
    # 「ペースを落とす」advice のほうが強いのでそちらを優先する。FRESH でも出す
    if pace.burst_exhausts_at is not None and st is not PaceStatus.OVER:
        dt = _fmt_time_short(pace.burst_exhausts_at, now)
        return t("render.advice.burst_compact" if compact
                 else "render.advice.burst", dt=dt)

    if st is PaceStatus.FRESH:
        # F5: リセット直後で使用もまだ少ない。枠は開いている旨だけ伝える
        return t("render.advice.fresh_compact") if compact \
            else t("render.advice.fresh")

    # Less than one unit (1h / 1 day) to the deadline: a per-unit allowance would
    # exceed what is left (75% in 18 min = "244%/h"), so talk about the rest.
    # 締切は advice = "bedtime" のとき「寝るまで」、それ以外はリセット。
    hourly = win.length < timedelta(days=1)
    unit_len = timedelta(hours=1) if hourly else timedelta(days=1)
    last_unit = left_in is not None and left_in < unit_len
    left = max(0, _round(100.0 - win.used_percent))

    if compact:  # narrow card: status word + tail
        if hourly and not last_unit:
            # 1日未満の窓 (5時間枠など) は許容量ではなく残量と締切までの時間を
            # そのまま出す。リセット時刻不明 (実データでは UNKNOWN で先に返る)
            # は状態語だけ。
            if left_in is not None:
                return _short_left_advice(_badge(st), left, left_in, bedtime_in)
            return _badge(st)
        sust = _daily_allowance(pace)
        if last_unit:
            key = "render.advice.bedtime_compact_reset" if bedtime_in is not None \
                else "render.advice.compact_reset"
            tail = t(key, span=_fmt_span(left_in))
        elif sust is not None:
            tail = t("render.advice.compact_per_day", pct=_round(sust))
        else:
            tail = None
        label = _badge(st)
        return "%s · %s" % (label, tail) if tail else label

    if reset_in is None:
        return t("render.advice.no_reset", label=_badge(st))
    if last_unit:
        span = _fmt_span(left_in)
        # bedtime のときだけ文言を「寝るまで」版に差し替える (数値の考えかたは同じ)
        pre = "bedtime_" if bedtime_in is not None else ""
        if st is PaceStatus.OVER:
            return t("render.advice.%sover_last_unit" % pre, span=span, pct=left)
        if st is PaceStatus.TIGHT:
            return t("render.advice.%stight_last_unit" % pre, span=span)
        if st is PaceStatus.ON_TRACK:
            return t("render.advice.%son_track_last_unit" % pre, span=span, pct=left)
        if st is PaceStatus.SPARE:
            return t("render.advice.%sspare_last_unit" % pre, span=span, pct=left)
        return _badge(st)
    if hourly:
        # 1日未満の窓 (5時間枠など) も compact と同じ形で残量と締切までの時間を
        # そのまま出す。頭は OVER なら「ペースを約◯%落とす」、TIGHT は
        # 「ほぼ使い切れる見込み」、それ以外はバッジの状態語 (順調 / 余裕あり)。
        if st is PaceStatus.OVER:
            slow = _round(-pace.adjust_percent) if pace.adjust_percent is not None else None
            head = t("render.advice.slow_down_pct", pct=slow) if slow \
                else t("render.advice.slow_down")
        elif st is PaceStatus.TIGHT:
            head = t("render.advice.tight")
        else:
            head = _badge(st)
        return _short_left_advice(head, left, left_in, bedtime_in)
    # Windows of a day or longer (weekly etc.) talk in the per-day allowance.
    # 表示は暦の 1 日あたり (pacing.sleep 設定時は sustainable_per_day より
    # 小さくなる)。
    sust = _daily_allowance(pace)
    unit = t("render.advice.unit_day")
    per_unit = _round(sust) if sust is not None else None
    unit_txt = t("render.advice.allowance", unit=unit, pct=per_unit) \
        if per_unit is not None else None
    rem_txt = t("render.advice.rem_days", d=max(0, reset_in.days))
    if st is PaceStatus.OVER:
        if unit_txt:
            slow = _round(-pace.adjust_percent) if pace.adjust_percent is not None else None
            head = t("render.advice.slow_down_pct", pct=slow) if slow \
                else t("render.advice.slow_down")
            return t("render.advice.slow_down_plan", head=head, rem=rem_txt,
                     allowance=unit_txt)
        return t("render.advice.over_generic")
    if st is PaceStatus.TIGHT:
        return t("render.advice.tight_plan", allowance=unit_txt) if unit_txt \
            else t("render.advice.tight")
    if st is PaceStatus.ON_TRACK:
        return t("render.advice.on_track_plan", allowance=unit_txt) if unit_txt \
            else _badge(st)
    if st is PaceStatus.SPARE:
        return t("render.advice.spare_plan", unit=unit, pct=per_unit) \
            if per_unit is not None else _badge(st)
    return _badge(st)


# ----------------------------------------------------------------- layout ---

HEADER_H = 40
MARGIN = 16
GAP = 16
AREA_TOP = HEADER_H + 12
AREA_BOTTOM = CANVAS_H - 8
CARD_RADIUS = 14

MAX_SERVICES = 8        # auto_layout keeps at most this many services
MAX_COLUMNS = 4         # 列の上限 (large 1 枚 / small 2 枚で 1 列、small だけなら最大 8 枚)
NARROW_W = 520          # below this a panel switches to the compact layout

_STATUS_RANK = {
    PaceStatus.EXHAUSTED: 5,
    PaceStatus.OVER: 4,
    PaceStatus.TIGHT: 3,
    PaceStatus.ON_TRACK: 2,
    PaceStatus.SPARE: 1,
    PaceStatus.FRESH: 1,     # 枠は開いている: spare 相当
    PaceStatus.UNKNOWN: 0,
}


def _main_rank(win):
    """Main-window preference: longest window, ties broken by key=="weekly".
    内訳窓 (part_of 付き) はどの窓より劣る最低順位で、絶対にメイン候補に
    ならない。"""
    if win.part_of is not None:
        return (-1.0, 0)
    return (win.length.total_seconds(), 1 if win.key == "weekly" else 0)


def _sub_label(main_win, sub_win):
    """Sub-window label: its own label minus the main label ("週間 Fable" ->
    "Fable" when the main window is "週間")."""
    lab = sub_win.label or sub_win.key
    m = main_win.label or ""
    if m and lab.startswith(m):
        lab = lab[len(m):]
    lab = lab.strip(" ·・-")
    return lab or sub_win.key


# ------------------------------------------------------------- slot layout --

def _group_services(cards):
    # type: (Sequence[Tuple[ProviderUsage, Optional[Pace]]]) -> List[dict]
    """Group a flat (ProviderUsage, Pace) list into one entry per service
    (provider), keeping first-seen order. Each group keeps its usable
    windows, its best main window (longest, weekly preferred) and, while the
    engine keeps showing the provider's last good data during fetch failures,
    a "stale" tuple (reason, last successful fetched_at) for the card mark."""
    order = []  # type: List[dict]
    groups = {}  # type: dict
    for pu, pace in cards:
        g = groups.get(pu.provider)
        if g is None:
            g = {"provider": pu.provider, "name": pu.name or pu.provider,
                 "error": None, "stale": None, "items": []}
            groups[pu.provider] = g
            order.append(g)
        if pu.error and g["error"] is None:
            g["error"] = pu.error
        if pu.stale_error and g["stale"] is None:
            g["stale"] = (pu.stale_error, pu.fetched_at)
        g["items"].append((pu, pace))
    for g in order:
        g["usable"] = [(pu, p) for (pu, p) in g["items"]
                       if p is not None and not pu.error]
        best = None
        for it in g["usable"]:
            if best is None or _main_rank(it[1].window) > _main_rank(best[1].window):
                best = it
        g["best"] = best
    return order


def group_columns(specs):
    # type: (Sequence[PanelSpec]) -> List[List[PanelSpec]]
    """PanelSpec を列に組み立てる (config 検証と render の共通規則)。

    panels を先頭から順に見る。size="large" はその 1 枚で 1 列。size="small"
    は直前の列が small 列でまだ 1 枚しか入っていなければその下段に入り、
    そうでなければ新しい small 列 (上段) になる。読み取るのは ``.size`` だけ
    なので、config 側の Panel (main/subs/size) もそのまま渡せる。
    """
    columns = []  # type: List[List[PanelSpec]]
    for spec in specs:
        prev = columns[-1] if columns else None
        if (spec.size == "small" and prev is not None
                and prev[0].size == "small" and len(prev) == 1):
            prev.append(spec)               # 直前の small 列の下段
        else:
            columns.append([spec])          # large 列 / 新しい small 列の上段
    return columns


def auto_layout(cards):
    # type: (Sequence[Tuple[ProviderUsage, Optional[Pace]]]) -> List[PanelSpec]
    """Default placement: one panel per service (main = its best window,
    subs = the service's remaining windows, small panels have none). Up to 4
    services all get large panels; with 5-8 the right side becomes 2-row
    small columns so everything fits in MAX_COLUMNS columns (n=5: large3+
    small2, ..., n=8: small8). More than MAX_SERVICES services are dropped.
    An error service becomes a panel with main = provider id."""
    groups = _group_services(cards)[:MAX_SERVICES]
    n = len(groups)
    n_large = n if n <= 4 else MAX_SERVICES - n   # n=5..8 -> 3, 2, 1, 0
    # Balance-only panels occupy small slots; retain the old layout otherwise.
    if any(g["best"] is None and not g["error"] for g in groups):
        groups.sort(key=lambda g: g["best"] is None and not g["error"])
        normal = sum(g["best"] is not None or bool(g["error"]) for g in groups)
        n_large = min(normal, MAX_SERVICES - n, 4)
    specs = []  # type: List[PanelSpec]
    for i, g in enumerate(groups):
        size = "large" if i < n_large else "small"
        if g["best"] is not None:
            main_id = g["best"][1].window.id
            subs = sorted([it[1].window.id for it in g["usable"]
                           if it is not g["best"]][:3],
                          key=lambda wid: not wid.endswith(":5h")) \
                if size == "large" else []           # 5 時間枠を先頭に
        else:
            main_id, subs = g["provider"], []
        specs.append(PanelSpec(main=main_id, subs=subs, size=size))
    return specs


def _resolve_subs(by_win, sub_ids, main_id):
    # type: (dict, Sequence[str], str) -> List[tuple]
    """Look up sub window ids; unknown ids and the main window itself are
    ignored, at most 3 subs are kept."""
    subs = []
    for sid in sub_ids:
        if len(subs) >= 3:
            break
        found = by_win.get(sid)
        if found is None or sid == main_id:
            continue
        subs.append(found)          # (group, ProviderUsage, Pace)
    return subs


def _lookups(cards):
    # type: (Sequence[Tuple[ProviderUsage, Optional[Pace]]]) -> Tuple[dict, dict]
    """(by provider id, by window id) lookup tables over the service groups."""
    groups = _group_services(cards)
    by_prov = {g["provider"]: g for g in groups}
    by_win = {}  # type: dict
    for g in groups:
        for pu, p in g["usable"]:
            by_win[p.window.id] = (g, pu, p)
    return by_prov, by_win


def _resolve_parts(g, main_win, sub_ids=None):
    # type: (dict, Window, Optional[Sequence[str]]) -> List[Tuple[Window, tuple]]
    """メイン窓の内訳: 同じサービスの usable 窓のうち part_of がメイン窓の
    key に等しいものを並び順どおりに集め、固定パレットの色を割り当てる。
    (Claude の weekly_fable のように part_of が None の窓は対象外。)
    sub_ids が渡されたとき (config の layout で subs が明示) は、その中に
    入る内訳だけに絞る。None / 空なら全内訳 (自動配置と同じ)。"""
    restrict = set(sub_ids) if sub_ids else None
    parts = []  # type: List[Tuple[Window, tuple]]
    for _pu, p in g["usable"]:
        w = p.window
        if w.id != main_win.id and w.part_of == main_win.key \
                and (restrict is None or w.id in restrict):
            parts.append((w, _PART_COLORS[len(parts) % len(_PART_COLORS)]))
    return parts


def _resolve_panel(lookups, spec):
    # type: (Tuple[dict, dict], PanelSpec) -> Optional[dict]
    """Turn one PanelSpec into a drawable panel. Returns None when ``main``
    cannot be resolved (the caller decides: a large column disappears, a
    small slot stays empty). main = provider id auto-picks the service's
    best window; a failed service (provider id, or a window id whose group
    has an error and no usable window) becomes an error panel (main=None).
    The panel dict also carries the main window's breakdown ("parts"):
    spec.subs が空 (自動) なら全内訳、明示ならその中の内訳だけ。"""
    by_prov, by_win = lookups
    g = by_prov.get(spec.main)
    if g is not None:                       # provider id
        mi = g["best"]
        subs = _resolve_subs(by_win, spec.subs,
                             mi[1].window.id) if mi is not None else []
        parts = _resolve_parts(g, mi[1].window, spec.subs) \
            if mi is not None else []
    else:
        found = by_win.get(spec.main)       # window id
        if found is None:
            g = by_prov.get(spec.main.split(":", 1)[0])
            if g is not None and g["error"] and g["best"] is None:
                return {"spec": spec, "g": g, "main": None, "subs": [],
                        "parts": []}
            return None                     # unknown id: not drawable
        g, mi = found[0], (found[1], found[2])
        subs = _resolve_subs(by_win, spec.subs, spec.main)
        parts = _resolve_parts(g, mi[1].window, spec.subs)
    return {"spec": spec, "g": g, "main": mi, "subs": subs, "parts": parts}


def _layout_columns(cards, specs):
    # type: (Sequence[Tuple[ProviderUsage, Optional[Pace]]], Sequence[PanelSpec]) -> List[Tuple[str, List[Optional[dict]]]]
    """Assemble specs into drawable columns via group_columns(), capped to
    MAX_COLUMNS. Each column is ("large", [panel]) or ("small", [top, bottom])
    where a slot is None when it stays empty (main="" / unknown id). A failed
    service keeps its error panel; a column vanishes only when nothing can be
    drawn in it (unresolvable large column, or both small rows empty)."""
    lookups = _lookups(cards)
    cols = []  # type: List[Tuple[str, List[Optional[dict]]]]
    for column in group_columns(specs)[:MAX_COLUMNS]:
        if column[0].size == "large":
            p = _resolve_panel(lookups, column[0])
            if p is not None:
                cols.append(("large", [p]))
        else:
            slots = [_resolve_panel(lookups, sp) if sp.main else None
                     for sp in column]
            if any(slot is not None for slot in slots):
                cols.append(("small", slots))
    return cols


# -------------------------------------------------------------- card parts --

def _draw_header(s, now, recommendation=None):
    # type: (_S, datetime, Optional[str]) -> None
    cy = HEADER_H / 2.0 + 2
    title = "QuotaCanvas"
    s.text((MARGIN, cy), title, font=F("w7", 24), fill=TEXT, anchor="lm")
    clock = _fmt_clock(now)
    clock_f = F("w4", 24)
    s.text((CANVAS_W - MARGIN, cy), clock, font=clock_f, fill=TEXT_DIM,
           anchor="rm")
    if recommendation:
        # F1: 「今使う先」の 1 行。タイトルと時計のあいだに中央寄せで描く
        tw = s.textlength(title, font=F("w7", 24))
        cw2 = s.textlength(clock, font=clock_f)
        max_w = CANVAS_W - 2 * MARGIN - tw - cw2 - 60
        rf, shown = _fit_text(s, recommendation, "w6", 17, max_w, min_size=11)
        s.text((CANVAS_W / 2.0, cy), shown, font=rf, fill=TEXT_DIM,
               anchor="mm")
    s.line((0, HEADER_H, CANVAS_W, HEADER_H), fill=DIVIDER, width=1)


def _divisions(win):
    # type: (Window) -> int
    """Equal budget slices: days for windows >= 1 day, else hours."""
    sec = win.length.total_seconds()
    if sec >= 86400.0:
        return max(1, int(math.ceil(sec / 86400.0)))
    return max(1, int(math.ceil(sec / 3600.0)))


def _hatch_rect(s, box, color, spacing=7, width=1):
    # type: (_S, Tuple[float, float, float, float], tuple, int, int) -> None
    """45-degree diagonal hatch lines (y = x + c) clipped to `box`."""
    x0, y0, x1, y1 = box
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    c0 = y0 - x1                       # min of (y - x) inside the rect
    c1 = y1 - x0                       # max of (y - x) inside the rect
    c = int(math.ceil((c0 + 1) / spacing) * spacing)
    while c < c1:
        xa = max(x0 + 1, y0 + 1 - c)
        xb = min(x1 - 1, y1 - 1 - c)
        if xb > xa:
            s.line((xa, xa + c, xb, xb + c), fill=color, width=width)
        c += spacing


def _checker_rect(s, box, color, cell=4):
    # type: (_S, Tuple[float, float, float, float], tuple, int) -> None
    """Checkerboard of `cell`-px squares in `color` over `box` (every other
    cell, anchored to the box's top-left corner)."""
    x0, y0, x1, y1 = box
    if x1 - x0 < 1 or y1 - y0 < 1:
        return
    row = 0
    y = y0
    while y < y1:
        x = x0 + (cell if row % 2 else 0)
        while x < x1:
            rx, ry = min(x + cell, x1) - 1, min(y + cell, y1) - 1
            if rx >= x and ry >= y:         # 端で 1px に満たない切れ端は省く
                s.rectangle((x, y, rx, ry), fill=color)
            x += 2 * cell
        y += cell
        row += 1


def _paint_parts(s, x0, y0, x1, y1, r, start, end, parts, muted=False):
    # type: (_S, float, float, float, float, float, float, float, Sequence[Tuple[float, tuple]], bool) -> None
    """バーの [start, end] % 区間に内訳 (used_percent, color) を左から順に
    塗る。muted=True のときは薄い内訳色の上に市松模様を重ね、半透明に
    見せる (gauge="remaining" の消費済み区間用)。区間の左端は、バーの左端
    (約 0%) から塗るときだけ角を丸め、右端が区間の終わり (end) に達すると
    きはその丸い終端に合わせて角を丸くする。内訳に入らない残りは下地
    (TRACK / テーマ色) のまま。"""
    bw = x1 - x0
    # 角丸の一部だけ丸める (corners 指定) とき、PIL は半径が高さの半分に
    # 満たないことを要求する
    rc = max(0.0, min(r, (y1 - y0) / 2.0 - 1.0))
    cur = _clamp(float(start), 0.0, 100.0)
    last = len(parts) - 1
    for i, (pct, col) in enumerate(parts):
        seg = _clamp(float(pct), 0.0, max(0.0, end - cur))
        px0 = x0 + bw * cur / 100.0
        px1 = x0 + bw * (cur + seg) / 100.0
        if px1 - px0 > 1.0:
            rl = (i == 0 and start <= 0.5)  # バー左端からのみ丸める
            rr = (i == last and cur + seg >= end - 0.5)
            # 半径は区間の幅にも収める (極小区間は角丸なしの矩形)
            radius = max(0.0, min(rc, (px1 - px0) / 2.0 - 1.0))
            fill = _mix(col, TRACK, 0.85) if muted else col
            if radius <= 0.0:
                s.rectangle((px0, y0, px1, y1), fill=fill)
            else:
                s.rrect((px0, y0, px1, y1), radius=radius, fill=fill,
                        corners=(rl, rr, rr, rl))
            if muted:
                # 市松模様で半透明っぽく見せる (丸めた角の内側には置かない)
                cx0 = px0 + (radius if rl else 0.0)
                cx1 = px1 - (radius if rr else 0.0)
                _checker_rect(s, (cx0, y0, cx1, y1), _mix(col, TRACK, 0.55))
        cur += seg
        if cur >= 100.0 - 0.5:
            break


def _draw_gauge(s, box, win, pace, fill_color, state_color, gauge,
                marker=1.0, marks=True, left_mark=None, parts=None):
    # type: (_S, Tuple[float, float, float, float], Window, Pace, tuple, tuple, str, float, bool, Optional[bool], Optional[Sequence[Tuple[float, tuple]]]) -> str
    """Window gauge bar. gauge="remaining": fill = what is left (full at
    100%); a 45-degree hatch in the state color over [predicted remaining,
    current remaining] shows how much of it will be gone by the reset, and a
    small "枯渇" mark sits at the left end when it is predicted to hit 0.
    gauge="used": fill = consumption piling up from the left; a dim
    state-color hatch on the unused part marks the projected end.
    Fill uses the service theme color `fill_color`; `state_color` is only
    used for the prediction hatch. Fixed budget boundaries (one line per
    day / hour): black 35% where they cross the fill, white 35% on the
    unfilled part, so they stay visible on bright theme colors too.
    parts は内訳 [(used_percent, color), ...]。あるとき消費した区間を内訳
    ごとに塗り分ける ("used": 左から順に各内訳色、残りはテーマ色 /
    "remaining": 右側の消費済み区間を TRACK 半分混ぜの内訳色)。parts が
    なくても描画結果は変わらない。
    Returns the caption string."""
    x0, y0, x1, y1 = box
    r = (y1 - y0) / 2.0
    s.rrect(box, radius=r, fill=TRACK)
    bw = x1 - x0

    if gauge == "remaining":
        head = t("render.word.remaining")
        value = win.remaining_percent
        ideal_pos = 100.0 - pace.ideal_used_percent
        fill_frac = _clamp(value / 100.0, 0.0, 1.0)
    else:
        head = t("render.gauge.used_head")
        value = win.used_percent
        ideal_pos = pace.ideal_used_percent
        fill_frac = _clamp(value / 100.0, 0.0, 1.0)

    fill_w = bw * fill_frac
    if fill_w > 2:
        s.rrect((x0, y0, x0 + fill_w, y1), radius=r, fill=fill_color)

    if parts:
        if gauge == "remaining":
            # 右側の消費済み区間 [残り%, 100%] を、TRACK と半々に混ぜた
            # 内訳色で残りの境目から順に塗り分ける
            _paint_parts(s, x0, y0, x1, y1, r, value, 100.0, parts, muted=True)
        else:
            # 消費した区間 [0, 使用%] を左から順に内訳色で塗る
            _paint_parts(s, x0, y0, x1, y1, r, 0.0, fill_frac * 100.0, parts)

    pe = pace.projected_end_percent
    if pe is not None and gauge == "remaining":
        # hatch the part of the fill predicted to be consumed by the reset
        pred_frac = _clamp(max(0.0, 100.0 - pe) / 100.0, 0.0, 1.0)
        px = x0 + bw * pred_frac
        fx = x0 + fill_w
        if px < fx - 2:
            # state color darkened so the hatch reads on any theme fill,
            # including bright ones (grok's near-white)
            _hatch_rect(s, (px, y0 + 2, fx, y1 - 2),
                        _mix(state_color, BLACK, 0.35))
        if (marks if left_mark is None else left_mark) and pe >= 100:
            # 左端の「枯渇」マーク: en では "Exhausted" と長いので、塗りの幅に
            # 収める (_fit_text が縮小 → 省略。狭い塗りでも描画をやめない)。
            ef, exhausted = _fit_text(s, t("render.gauge.exhausted"), "w7", 13,
                                      max(fill_w - 4, 12), min_size=9)
            s.text((x0 + 2, y0 - 5), exhausted, font=ef, fill=WHITE, anchor="ls")
    elif pe is not None and pe > win.used_percent:
        ext_end = x0 + bw * _clamp(pe / 100.0, 0.0, 1.0)
        if ext_end - (x0 + fill_w) > 2:
            _hatch_rect(s, (x0 + fill_w, y0 + 2, ext_end, y1 - 2),
                        _dim(state_color, 0.4))
        if marks and pe > 100:
            cy = (y0 + y1) / 2.0
            tf = F("w7", 14)
            empty = t("render.gauge.exhausted")
            tw = s.textlength(empty, font=tf)
            s.text((x1 - 4, cy), empty, font=tf, fill=WHITE, anchor="rm")
            tx_ = x1 - 4 - tw - 10
            s.polygon([(tx_ - 8, cy - 5), (tx_ - 8, cy + 5), (tx_, cy)],
                      fill=WHITE)

    # fixed budget boundaries: one line per day (hour) of the window;
    # dark over the fill, light over the track, neutral on the card ticks
    div_dark = _mix(fill_color, BLACK, 0.35)
    div_light = _mix(TRACK, WHITE, 0.35)
    div_tick = _mix(CARD_BG, WHITE, 0.35)
    n = _divisions(win)
    for k in range(1, n):
        lx = x0 + bw * k / n
        s.line((lx, y0 - 4, lx, y0), fill=div_tick, width=1)
        s.line((lx, y1, lx, y1 + 4), fill=div_tick, width=1)
        s.line((lx, y0, lx, y1),
               fill=div_dark if lx < x0 + fill_w else div_light, width=1)

    # ideal position: small white triangle pointing at the bar from above
    # (kept 4px clear of the bar so it does not touch the top edge)
    ix = x0 + bw * _clamp(ideal_pos / 100.0, 0.0, 1.0)
    s.polygon([(ix - 5 * marker, y0 - 13 * marker),
               (ix + 5 * marker, y0 - 13 * marker),
               (ix, y0 - 6 * marker)], fill=WHITE)

    return t("render.gauge.caption", head=head, value=_fmt_num(value),
             ideal=_fmt_num(ideal_pos))


def _stale_text(fetched_at):
    # type: (Optional[datetime]) -> str
    """カード右上の失敗中表示の文言。「HH:MM」は最後に取得に成功した時刻
    (fetched_at) のローカル時刻。"""
    l = _local(fetched_at)
    if l is None:
        return t("render.stale.text")
    return t("render.stale.at", hh=l.hour, mm=l.minute)


def _draw_stale_mark(s, x, y, stale, max_w):
    # type: (_S, float, float, Tuple[str, Optional[datetime]], float) -> None
    """取得失敗中も last good データを表示しているカードの右上に、小さめの
    警告色 (YELLOW) で「取得失敗中 HH:MM 時点」と描く。stale は (失敗理由,
    最終成功時刻)。呼び出し側がステータスバッジなどの右上の表示の下に置く。"""
    _reason, fetched_at = stale
    f, shown = _fit_text(s, _stale_text(fetched_at), "w6", 13, max_w, min_size=10)
    s.text((x, y), shown, font=f, fill=YELLOW, anchor="rt")


#: 大パネルで「前回」を大きい % の行に足しても、右の指標列に残す最低幅。
PREV_INDICATOR_MIN_W = 230


def _draw_service_card(s, box, name, main_item, subs, now, gauge, theme,
                       stale=None, parts=None):
    # type: (_S, Tuple[int, int, int, int], str, tuple, List[dict], datetime, str, tuple, Optional[Tuple[str, Optional[datetime]]], Optional[List[Tuple[Window, tuple]]]) -> None
    """One large panel: main window (big number, indicators, main gauge) plus
    one compact row per sub window at the bottom, and a single advice line on
    the bottom row. `subs` are dicts {"pace", "label", "theme"}; a sub may
    belong to a different service (its own label and theme color). Gauges and
    the name dot use the theme color `theme`; state colors appear only on
    badge / advice / left band / prediction hatch, plus red / green
    on the % numbers when over / spare (white when percent_colors is
    off)."""
    x0, y0, x1, y1 = box
    card_w = x1 - x0
    narrow = card_w < NARROW_W
    mp = main_item[1]
    win = mp.window
    color = _STATUS_COLOR[mp.status]

    # badge = the main window's status. Sub rows carry their own colors, so a
    # model-scoped sub ("週間 Fable") or another service must not drag the
    # whole card down. Only when the main window is short (just noise) does
    # the worst long window of the same service stand in.
    pool = [mp]
    if win.length < timedelta(days=1):
        long_subs = [sub["pace"] for sub in subs
                     if sub["pace"].window.provider == win.provider
                     and sub["pace"].window.length >= timedelta(days=1)]
        pool = long_subs or pool
    badge_status = max((p.status for p in pool),
                       key=lambda st: _STATUS_RANK[st])
    badge_color = _STATUS_COLOR[badge_status]

    s.rrect(box, radius=CARD_RADIUS, fill=CARD_BG)
    s.rrect((x0, y0, x0 + 6, y1), radius=3, fill=badge_color,
            corners=(True, False, False, True))
    pad = 12 if narrow else 18
    tx0 = x0 + 6 + pad + 4
    tx1 = x1 - pad
    cw = tx1 - tx0

    # -- 1. header: service name + status badge -----------------------------
    badge = _badge(badge_status)
    bf = F("w6", 17)
    bh = 26
    bx1 = tx1
    bx0 = bx1 - s.textlength(badge, font=bf) - 22
    by0, by1 = y0 + 14, y0 + 14 + bh
    s.rrect((bx0, by0, bx1, by1), radius=bh / 2.0, fill=badge_color)
    s.text(((bx0 + bx1) / 2.0, (by0 + by1) / 2.0 + 1), badge, font=bf,
           fill=_on(badge_color), anchor="mm")
    if stale is not None:
        # 取得失敗中: バッジの真下 (サービス名の行より下) に重ならない位置
        _draw_stale_mark(s, tx1, by1 + 3, stale, cw)
    nf = _fit_font(s, name, "w7", 20 if narrow else 24,
                   bx0 - 10 - (tx0 + 20), min_size=13)
    s.rrect((tx0, (by0 + by1) / 2.0 - 6, tx0 + 12, (by0 + by1) / 2.0 + 6),
            radius=6, fill=theme)
    s.text((tx0 + 20, (by0 + by1) / 2.0), name, font=nf, fill=TEXT,
           anchor="lm")

    # -- 2. main window: big number + word + label --------------------------
    if gauge == "remaining":
        pct = "%d%%" % _round(win.remaining_percent)
        word = t("render.word.remaining")
    else:
        pct = "%d%%" % _round(win.used_percent)
        word = t("render.word.used")
    pf = _fit_font(s, pct, "w8", 64 if narrow else 84, cw * 0.5, min_size=44)
    pw = s.textlength(pct, font=pf)
    num_base = y0 + (138 if narrow else 150)
    s.text((tx0, num_base), pct, font=pf, fill=_num_color(s, mp.status),
           anchor="ls")
    wx = tx0 + pw + 12
    wf = F("w5", 17)
    s.text((wx, num_base + 2), word, font=wf, fill=TEXT_DIM, anchor="ls")
    block_end = wx + s.textlength(word, font=wf)
    if win.label:
        lf = F("w5", 15)
        g = s.lead_gap(8)
        s.text((block_end + g, num_base + 2), "· " + win.label, font=lf,
               fill=TEXT_FAINT, anchor="ls")
        block_end += g + s.textlength("· " + win.label, font=lf)
    prev_txt = _prev_line(mp, gauge)          # F3: "前回 36% 余り" など
    if prev_txt:
        # 大きい % の横 ("残り · 週間" の後ろ) に同じ小ささで添え、経過など
        # 右の指標とは混ぜない。後ろに入りきらない広いカード (右に指標列が
        # ある) では "残り · 週間" の真上に置く。それも無理なら出さない。
        lf = F("w5", 15)
        tail = "· " + prev_txt
        tail_w = s.textlength(tail, font=lf)
        prev_w = s.textlength(prev_txt, font=lf)
        limit = tx1 if narrow else tx1 - PREV_INDICATOR_MIN_W
        g = s.lead_gap(8)
        if block_end + g + tail_w <= limit:
            s.text((block_end + g, num_base + 2), tail, font=lf,
                   fill=TEXT_FAINT, anchor="ls")
            block_end += g + tail_w
        elif not narrow and wx + prev_w <= limit:
            s.text((wx, num_base - 24), prev_txt, font=lf, fill=TEXT_FAINT,
                   anchor="ls")
            block_end = max(block_end, wx + prev_w)

    # indicators: units follow the window (days / hours)
    el = _elapsed_line(mp, now, total=not narrow)
    # 枯渇の日時はカード内で 1 か所だけ: 下段のアドバイスが「このペースだと
    # … に尽きる」(burst) を出すときは、ここでは消費見込み % にする。
    burst_in_advice = (mp.burst_exhausts_at is not None
                       and mp.status is not PaceStatus.OVER)
    if mp.exhausts_at is not None and not burst_in_advice:
        proj = t("render.card.exhaust_at",
                 dt=_fmt_time_short(mp.exhausts_at, now),
                 span=_fmt_span(mp.exhausts_at - now))
    elif mp.projected_end_percent is not None:
        proj = t("render.card.projected", pct=_round(mp.projected_end_percent))
    else:
        proj = t("render.card.no_projection")
    reset_span = _fmt_span(win.resets_at - now) if win.resets_at is not None else None
    reset_line = t("render.card.reset_line", dt=_fmt_dt(win.resets_at),
                   span=reset_span) \
        if win.resets_at is not None else t("render.card.no_reset")

    balances = getattr(main_item[0], "balances", None) or []
    if balances:
        reset_line += " · " + _balance_text(balances[0])

    if narrow:
        l1 = el + (t("render.card.narrow_el_reset", span=reset_span)
                   if reset_span else "")
        if balances:
            l1 += " · " + _balance_text(balances[0])
        ind_lines = [l1, proj]
        for k, txt in enumerate(ind_lines):
            f, shown = _fit_text(s, txt, "w4", 16, cw, min_size=11)
            s.text((tx0, num_base + 24 + k * 23), shown, font=f,
                   fill=TEXT, anchor="lm")
        block_bottom = num_base + 24 + 23 + 12
        gy0, gy1 = y0 + 210, y0 + 236
    else:
        ix = block_end + 20
        avail = max(60, tx1 - ix)
        ind_lines = [el, proj, reset_line]
        sizes = [_fit_font(s, t, "w4", 17, avail, min_size=12).size
                 for t in ind_lines]
        ind_f = F("w4", min(sizes))
        for k, txt in enumerate(ind_lines):
            f, shown = _fit_text(s, txt, "w4", ind_f.size, tx1 - ix, min_size=11)
            s.text((ix, num_base - 38 + k * 26), shown, font=f,
                   fill=TEXT, anchor="lm")
        block_bottom = num_base + 26
        gy0, gy1 = y0 + 192, y0 + 218

    # -- 3. main gauge; fixed y for every card (sub rows live between the
    #       gauge and the advice line, and stay empty when there are none) --
    n_subs = len(subs)
    advice_cy = y1 - 24
    _draw_gauge(s, (tx0, gy0, tx1, gy1), win, mp, theme, color, gauge,
                left_mark=not narrow,
                parts=[(w.used_percent, c) for w, c in (parts or [])])

    # -- 4. sub-window rows between the gauge and the advice line, packed
    #       from just under the gauge (same top as the reset credits) --------
    first_cy = gy1 + 38.0
    if n_subs:
        first_cy = min(first_cy, advice_cy - 38 - (n_subs - 1) * 33)

    def row_cy(k):
        return first_cy + k * 33

    part_color = {w.id: c for w, c in (parts or [])}
    # サブ行のゲージの始まりと終わりを揃える: 内訳行・通常行それぞれで、
    # いちばん長いラベルと、いちばん幅の広い右側 (数値 + 語/状態) に合わせる。
    nf2 = F("w8", 17)
    wf2 = F("w5", 13)
    part_lx = part_rw = row_lx = row_nw = row_bw = 0.0
    for sub in subs:
        sw = sub["pace"].window
        label = sub["label"]
        lf = _fit_font(s, label, "w6", 16, cw * 0.24, min_size=10)
        if sw.part_of is not None:
            num = "%d%%" % _round(sw.used_percent)
            part_lx = max(part_lx, 4.0 * 2 + 7 + s.textlength(label, font=lf))
            part_rw = max(part_rw, s.textlength(num, font=nf2) + 6
                          + s.textlength(t("render.word.used"), font=wf2))
        else:
            num = "%d%%" % _round(sw.remaining_percent if gauge == "remaining"
                                  else sw.used_percent)
            txt = _badge_short(_sub_status(sub["pace"].status))
            row_lx = max(row_lx, s.textlength(label, font=lf))
            row_nw = max(row_nw, s.textlength(num, font=nf2))
            row_bw = max(row_bw, s.textlength(txt, font=F("w6", 15)))
    for k, sub in enumerate(subs):
        sp = sub["pace"]
        sw = sp.window
        cy = row_cy(k)
        label = sub["label"]
        if sw.part_of is not None:
            # 内訳行: 凡例の小さな丸 + ラベル、内訳色の細いバー (使用量ぶん
            # だけ塗る。gauge 設定に関係なく使用量で表示)、数値は「使用」の
            # 語を添える。状態バッジ・予測ハッチ・理想▼・目盛りなし。
            pcol = part_color.get(sw.id, sub["theme"])
            lf = _fit_font(s, label, "w6", 16, cw * 0.24, min_size=10)
            dr = 4.0
            s.rrect((tx0, cy - dr, tx0 + dr * 2, cy + dr), radius=dr,
                    fill=pcol)
            s.text((tx0 + dr * 2 + 7, cy), label, font=lf, fill=TEXT,
                   anchor="lm")
            lx = tx0 + part_lx + 12
            num = "%d%%" % _round(sw.used_percent)
            word = t("render.word.used")
            ww = s.textlength(word, font=wf2)
            nx = tx1 - ww - 6 - s.textlength(num, font=nf2)
            gx1 = tx1 - part_rw - 12
            if gx1 - lx >= 30:
                br = 3.5
                s.rrect((lx, cy - br, gx1, cy + br), radius=br, fill=TRACK)
                used_w = (gx1 - lx) * _clamp(sw.used_percent / 100.0, 0.0, 1.0)
                if used_w > 2:
                    s.rrect((lx, cy - br, lx + used_w, cy + br), radius=br,
                            fill=pcol)
            s.text((nx, cy), num, font=nf2, fill=TEXT, anchor="lm")
            s.text((tx1, cy), word, font=wf2, fill=TEXT_DIM, anchor="rm")
            continue
        sst = _sub_status(sp.status)
        scol = _STATUS_COLOR[sst]
        lf = _fit_font(s, label, "w6", 16, cw * 0.24, min_size=10)
        s.text((tx0, cy), label, font=lf, fill=TEXT, anchor="lm")
        lx = tx0 + row_lx + 12
        num = "%d%%" % _round(sw.remaining_percent if gauge == "remaining"
                              else sw.used_percent)
        txt = _badge_short(sst)
        nx1 = tx1 - row_bw - 12                   # 数値の右端 (行をまたいで揃える)
        gx1 = nx1 - row_nw - 12
        if gx1 - lx >= 50:
            _draw_gauge(s, (lx, cy - 7, gx1, cy + 7), sw, sp, sub["theme"],
                        scol, gauge, marker=0.7, marks=False)
        s.text((nx1, cy), num, font=nf2,
               fill=_num_color(s, sst, TEXT), anchor="rm")
        s.text((tx1, cy), txt, font=F("w6", 15), fill=scol, anchor="rm")

    # -- 4.5 reset credits: top-aligned under the gauge, or under the sub
    #       rows when there are any, down to the advice line ----------------
    credits = list(getattr(main_item[0], "reset_credits", None) or [])
    if credits:
        gap_top = (row_cy(n_subs - 1) + 22.0) if subs else (first_cy - 13.0)
        limit = advice_cy - 20.0
        _draw_reset_credits(s, tx0, cw, gap_top, limit, credits, now)

    # -- 5. advice (bottom row) ----------------------------------------------
    adv = advice_text(mp, narrow, now)
    note = _sub_note(mp, subs, now)           # F2: 先に尽きるサブ窓に触れる
    color = advice_color(mp)  # burst の警告が出るときは spare / on_track でも黄 (バッジ・帯は status 色)
    if note:
        joined = "%s · %s" % (adv, note)
        if s.textlength(joined, F("w6", 22)) <= cw * 1.2:
            adv = joined
        else:
            # 並べると小さすぎて読めない: 先に尽きるサブ窓のほうが差し迫っているので注記だけ
            adv = note
            if color not in (RED, YELLOW):
                color = YELLOW
    af, adv = _fit_text(s, adv, "w6", 22, cw, min_size=12)
    s.text((tx0, advice_cy), adv, font=af, fill=color, anchor="lm")


def _draw_error_card(s, box, g, theme):
    # type: (_S, Tuple[int, int, int, int], dict, tuple) -> None
    x0, y0, x1, y1 = box
    s.rrect(box, radius=CARD_RADIUS, fill=CARD_BG)
    s.rrect((x0, y0, x0 + 6, y1), radius=3, fill=GRAY,
            corners=(True, False, False, True))
    tx0 = x0 + 6 + 22
    tx1 = x1 - 20
    cy = (y0 + y1) / 2.0
    s.rrect((tx0, cy - 22, tx0 + 12, cy - 10), radius=6, fill=theme)
    cw = tx1 - (tx0 + 20)
    tf = _fit_font(s, g["name"], "w7", 22, cw, min_size=13)
    s.text((tx0 + 20, cy - 16), g["name"], font=tf, fill=TEXT, anchor="lm")
    reason = t("render.error.reason",
               reason=g["error"] or t("render.error.unknown"))
    rf = _fit_font(s, reason, "w4", 17, cw, min_size=12)
    s.text((tx0, cy + 14), reason, font=rf, fill=TEXT_DIM, anchor="lm")


def _draw_small_error(s, box, name, error, theme):
    # type: (_S, Tuple[int, int, int, int], str, Optional[str], tuple) -> None
    x0, y0, x1, y1 = box
    s.rrect(box, radius=CARD_RADIUS, fill=CARD_BG)
    s.rrect((x0, y0, x0 + 6, y1), radius=3, fill=GRAY,
            corners=(True, False, False, True))
    tx0 = x0 + 6 + 18
    tx1 = x1 - 16
    cy = (y0 + y1) / 2.0
    s.rrect((tx0, cy - 22, tx0 + 10, cy - 12), radius=5, fill=theme)
    tf = _fit_font(s, name, "w7", 20, tx1 - (tx0 + 18), min_size=12)
    s.text((tx0 + 18, cy - 17), name, font=tf, fill=TEXT, anchor="lm")
    reason = t("render.error.reason",
               reason=error or t("render.error.unknown"))
    rf, shown = _fit_text(s, reason, "w4", 15, tx1 - tx0, min_size=10)
    s.text((tx0, cy + 12), shown, font=rf, fill=TEXT_DIM, anchor="lm")


def _draw_small_panel(s, box, name, main_item, now, gauge, theme, stale=None,
                      parts=None):
    # type: (_S, Tuple[int, int, int, int], str, tuple, datetime, str, tuple, Optional[Tuple[str, Optional[datetime]]], Optional[List[Tuple[Window, tuple]]]) -> None
    """One small slot panel: theme dot + service name + window label, a small
    status badge, a big remaining %, a divider + gauge (with the ideal
    triangle), the reset countdown and a one-line compact advice. stale is the
    (reason, fetched_at) tuple shown as the "取得失敗中" mark while the engine
    keeps displaying this provider's last good data."""
    x0, y0, x1, y1 = box
    mp = main_item[1]
    win = mp.window
    color = _STATUS_COLOR[mp.status]        # badge == main status (no subs)

    s.rrect(box, radius=CARD_RADIUS, fill=CARD_BG)
    s.rrect((x0, y0, x0 + 6, y1), radius=3, fill=color,
            corners=(True, False, False, True))
    pad = 16
    tx0 = x0 + 6 + pad + 2
    tx1 = x1 - pad
    cw = tx1 - tx0

    # -- 1. header: theme dot + "サービス名 · 窓ラベル", badge on the right --
    badge = _badge(mp.status)
    bf = F("w6", 13)
    bh = 22
    bx1 = tx1
    bx0 = bx1 - s.textlength(badge, font=bf) - 20
    by0, by1 = y0 + 12, y0 + 12 + bh
    s.rrect((bx0, by0, bx1, by1), radius=bh / 2.0, fill=color)
    s.text(((bx0 + bx1) / 2.0, (by0 + by1) / 2.0 + 1), badge, font=bf,
           fill=_on(color), anchor="mm")
    if stale is not None:
        # 取得失敗中: バッジの真下 (名前行より下) に重ならない位置
        _draw_stale_mark(s, tx1, by1 + 2, stale, cw)
    name_cy = (by0 + by1) / 2.0
    s.rrect((tx0, name_cy - 5, tx0 + 10, name_cy + 5), radius=5, fill=theme)
    nx = tx0 + 18
    nf = _fit_font(s, name, "w7", 19, cw * 0.55, min_size=12)
    s.text((nx, name_cy), name, font=nf, fill=TEXT, anchor="lm")
    if win.label:
        nx += s.textlength(name, font=nf)
        lf, shown = _fit_text(s, " · " + win.label, "w5", 14,
                              bx0 - 10 - nx, min_size=10)
        s.text((nx, name_cy), shown, font=lf, fill=TEXT_FAINT, anchor="lm")

    # -- 2. big remaining % ---------------------------------------------------
    if gauge == "remaining":
        pct, word = ("%d%%" % _round(win.remaining_percent),
                     t("render.word.remaining"))
    else:
        pct, word = ("%d%%" % _round(win.used_percent),
                     t("render.word.used"))
    pf = _fit_font(s, pct, "w8", 52, cw * 0.6, min_size=32)
    s.text((tx0, y0 + 94), pct, font=pf, fill=_num_color(s, mp.status),
           anchor="ls")
    wx = tx0 + s.textlength(pct, font=pf) + 10
    word_f = F("w5", 15)
    s.text((wx, y0 + 96), word, font=word_f, fill=TEXT_DIM, anchor="ls")
    prev_txt = _prev_line(mp, gauge)          # F3: 入らなければ省略
    if prev_txt:
        px = wx + s.textlength(word, font=word_f) + 10
        avail = tx1 - px
        if avail >= 60:
            vf, shown = _fit_text(s, prev_txt, "w5", 13, avail, min_size=9)
            s.text((px, y0 + 96), shown, font=vf, fill=TEXT_FAINT, anchor="ls")

    # -- 3. divider + gauge ---------------------------------------------------
    s.line((tx0, y0 + 108, tx1, y0 + 108), fill=DIVIDER, width=1)
    _draw_gauge(s, (tx0, y0 + 122, tx1, y0 + 140), win, mp, theme, color,
                gauge, marker=0.8, marks=False,
                parts=[(w.used_percent, c) for w, c in (parts or [])])

    # -- 4. reset countdown + reset credits + compact advice ------------------
    credits = list(getattr(main_item[0], "reset_credits", None) or [])
    adv = advice_text(mp, True, now)
    af, adv_shown = _fit_text(s, adv, "w6", 15, cw, min_size=10)
    reset_span = _fmt_span(win.resets_at - now) if win.resets_at is not None \
        else None
    rtxt = t("render.card.reset_compact", span=reset_span) \
        if reset_span is not None else t("render.card.no_reset")

    balances = getattr(main_item[0], "balances", None) or []
    if balances:
        rtxt = _balance_text(balances[0]) + " · " + rtxt

    # リセット行は「目盛り下端 (ゲージ下端 +4px) 〜アドバイス行 bbox 上端」の
    # 帯に、実フォントの bbox で上下 2px 以上の余白を保てる行数だけ描く
    # (大パネルの _draw_reset_credits と同じ方針)。2 行 (残り時間 + リセット
    # 権) 入らなければ残り時間の 1 行に縮め、それも入らなければ描かない。
    band_top = y0 + 140 + 4 + 2          # 目盛り下端 + 最低余白
    band_bot = _text_bbox(s, (tx0, y1 - 8), adv_shown, af, "ls")[1] - 2
    avail = band_bot - band_top
    rows = []                            # (font, text, bbox) 上から順
    if credits:
        ctxt = _credit_compact(credits)
        for r_sz, c_sz in ((13, 12), (12, 11), (11, 10), (11, 9), (10, 9)):
            rf, rt = _fit_text(s, rtxt, "w4", r_sz, cw, min_size=10)
            rb = _text_bbox(s, (0, 0), rt, rf, "ls")
            rh = rb[3] - rb[1]
            if rh + 2 > avail:
                continue
            cf, ct = _fit_text(s, ctxt, "w5", c_sz, cw, min_size=9)
            cb = _text_bbox(s, (0, 0), ct, cf, "ls")
            if rh + 2 + (cb[3] - cb[1]) <= avail:
                rows = [(rf, rt, rb), (cf, ct, cb)]
                break
    if not rows:                         # 残り時間の 1 行だけ (リセット権は省略)
        rf, rt = _fit_text(s, rtxt, "w4", 13, cw, min_size=10)
        rb = _text_bbox(s, (0, 0), rt, rf, "ls")
        if rb[3] - rb[1] <= avail:
            rows = [(rf, rt, rb)]
    ty = band_top
    for f, txt, bb in rows:                  # txt: モジュールの t (i18n) を保つ
        s.text((tx0, ty - bb[1]), txt, font=f, fill=TEXT_DIM, anchor="ls")
        ty += (bb[3] - bb[1]) + 2
    # burst の警告が出るときは spare / fresh でも黄 (バッジ・帯は status 色)
    s.text((tx0, y1 - 8), adv_shown, font=af, fill=advice_color(mp),
           anchor="ls")
# ------------------------------------------------------------------ entry ---

def _balance_text(balance: Balance) -> str:
    """Format a display-only amount with its label or the default balance label."""
    symbol = {"USD": "$", "CNY": "¥"}.get(balance.currency, balance.currency + " ")
    return "%s %s%.2f" % (balance.label or t("providers.balance"), symbol, balance.amount)


def _draw_balance_card(s, box, g, theme):
    # type: (_S, Tuple[int, int, int, int], dict, tuple) -> None
    """Draw a balance-only provider in the selected panel."""
    x0, y0, x1, y1 = box
    usage = g["items"][0][0]
    s.rrect(box, radius=CARD_RADIUS, fill=CARD_BG)
    s.rrect((x0, y0, x0 + 6, y1), radius=3, fill=theme,
            corners=(True, False, False, True))
    x = x0 + 24
    s.text((x, y0 + 24), g["name"], font=F("w7", 20), fill=TEXT, anchor="lm")
    for i, b in enumerate(usage.balances):
        text = _balance_text(b)
        font, text = _fit_text(s, text, "w5", 18, x1 - x - 16, min_size=10)
        s.text((x, y0 + 58 + i * 27), text, font=font, fill=TEXT, anchor="lm")
    if usage.stale_error:
        _draw_stale_mark(s, x1 - 16, y1 - 25, g["stale"], x1 - x - 16)


def _draw_large_column(s, box, p, colors, gauge, now):
    # type: (_S, Tuple[int, int, int, int], dict, Optional[dict], str, datetime) -> None
    """One large column: a service card, or the gray error card when the
    provider's fetch failed."""
    theme = _brand_color(colors, p["g"]["provider"])
    if p["main"] is None:
        if not p["g"]["error"] and p["g"]["items"][0][0].balances:
            _draw_balance_card(s, box, p["g"], theme)
            return
        _draw_error_card(s, box, p["g"], theme)
        return
    mi = p["main"]
    parts = p.get("parts") or []
    subs = []
    for (sg, su, sp) in p["subs"]:
        if su.provider != mi[0].provider:
            # sub frame of another service: label carries its name
            label = "%s %s" % (sg["name"], sp.window.label or sp.window.key)
        else:
            label = _sub_label(mi[1].window, sp.window)
        subs.append({"pace": sp, "label": label,
                     "theme": _brand_color(colors, su.provider)})
    _draw_service_card(s, box, p["g"]["name"], mi, subs, now, gauge, theme,
                       stale=p["g"].get("stale"), parts=parts)


def render(cards, now, offset=(0, 0), gauge="remaining", colors=None,
           layout=None, recommendation=None, percent_colors=True,
           separator=None, scale=1.0):
    # type: (Sequence[Tuple[ProviderUsage, Optional[Pace]]], datetime, Tuple[int, int], str, Optional[dict], Optional[Sequence[PanelSpec]], Optional[str], bool, Optional[str], float) -> Image.Image
    """Render the 1920x462 canvas. `cards` are (provider, pace) pairs in
    display order; pace=None marks a provider whose fetch failed.
    gauge: "remaining" (fill = what is left, default) or "used"
    (fill = consumption piling up).
    colors: optional {provider: "#RRGGBB"} overriding the default
    BRAND_COLORS theme colors used for gauge fills and name dots.
    layout: panel placement; None -> auto_layout(cards).
    recommendation: F1 の「今使う先」の 1 行 (None ならヘッダは従来どおり)。
    percent_colors: True なら % の数字を状態色 (超過・枯渇は赤、余裕ありは緑)
    にする。False なら全部白。
    separator: 文言の区切り " · " をこの文字列に置き換える (None なら置き換えない。
    設定の "" は呼び出し側が default_separator() にする)。
    group_columns() で
    最大 MAX_COLUMNS 列に組み立て、全列を等分幅で描く。large 列は大パネル 1 枚、
    small 列は上下 2 段の小パネル。main="" や解決できない id の small 段は
    空き枠として位置を保ち、解決できない large 列と両段空の small 列だけが
    消える (残りの列が詰まる)。"""
    if gauge not in ("remaining", "used"):
        raise ValueError("gauge must be 'remaining' or 'used'")
    img = Image.new("RGB", (round(CANVAS_W * scale), round(CANVAS_H * scale)), BG)
    s = _S(img, offset, scale)
    s.percent_colors = percent_colors
    s.sep = separator
    _draw_header(s, now, recommendation)
    cols = _layout_columns(cards,
                           auto_layout(cards) if layout is None else layout)
    ncols = len(cols)
    if ncols == 0:
        return img
    area = (MARGIN, AREA_TOP, CANVAS_W - MARGIN, AREA_BOTTOM)
    aw = area[2] - area[0]
    col_w = (aw - (ncols - 1) * GAP) / ncols

    def col_box(i):
        x = float(area[0] + i * (col_w + GAP))
        return (int(round(x)), area[1], int(round(x + col_w)), area[3])

    small_h = (area[3] - area[1] - GAP) / 2.0
    for i, (kind, slots) in enumerate(cols):
        box = col_box(i)
        if kind == "large":
            _draw_large_column(s, box, slots[0], colors, gauge, now)
            continue
        sboxes = [(box[0], area[1], box[2], int(round(area[1] + small_h))),
                  (box[0], int(round(area[1] + small_h + GAP)), box[2],
                   area[3])]
        for p, sbox in zip(slots, sboxes):
            if p is None:
                continue                # 空き段: 何も描かず位置だけ保つ
            theme = _brand_color(colors, p["g"]["provider"])
            if p["main"] is None and not p["g"]["error"] and p["g"]["items"][0][0].balances:
                _draw_balance_card(s, sbox, p["g"], theme)
            elif p["main"] is None:
                _draw_small_error(s, sbox, p["g"]["name"], p["g"]["error"],
                                  theme)
            else:
                _draw_small_panel(s, sbox, p["g"]["name"], p["main"], now,
                                  gauge, theme, stale=p["g"].get("stale"),
                                  parts=p.get("parts") or [])
    return img


def render_error(message, now, scale=1.0):
    # type: (str, datetime, float) -> Image.Image
    img = Image.new("RGB", (round(CANVAS_W * scale), round(CANVAS_H * scale)), BG)
    s = _S(img, (0, 0), scale)
    _draw_header(s, now)
    cy = (HEADER_H + CANVAS_H) / 2.0
    s.rrect((CANVAS_W / 2.0 - 22, cy - 86, CANVAS_W / 2.0 + 22, cy - 78),
            radius=4, fill=RED)
    s.text((CANVAS_W / 2.0, cy - 26), t("render.error.title"),
           font=F("w7", 34), fill=TEXT, anchor="mm")
    mf = _fit_font(s, message, "w4", 22, CANVAS_W - 2 * MARGIN - 120, min_size=14)
    s.text((CANVAS_W / 2.0, cy + 26), message, font=mf, fill=TEXT_DIM,
           anchor="mm")
    return img
