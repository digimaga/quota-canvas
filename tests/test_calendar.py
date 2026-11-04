"""カレンダーパネル (main = "calendar") と display.week_start のテスト。

曜日→列の対応 (weekday_column / weekday_columns)、月のマス組み立て
(month_weeks)、リセット日の印 (calendar_marks)、display.week_start の検証、
main = "calendar" の配置読み込み、そして large / small / 5 列での描画。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from quotacanvas.config import load_config
from quotacanvas.models import PaceStatus, ProviderUsage, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import (CALENDAR_MAIN, CANVAS_H, CANVAS_W, PanelSpec,
                                calendar_marks, month_weeks, render,
                                weekday_column, weekday_columns)

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fixed_local_tz(monkeypatch):
    """印の日付は実行環境のタイムゾーンに依存しないように固定する
    (TZ=America/Los_Angeles 等でも同じ期待値で通る)。個別のテストは
    もう一度 monkeypatch すれば別のタイムゾーンで検証できる。"""
    from quotacanvas import render as render_mod

    tz = ZoneInfo("Asia/Tokyo")
    monkeypatch.setattr(
        render_mod, "_local",
        lambda dt: dt.astimezone(tz) if dt is not None else None)

# ------------------------------------------------- 曜日 → 列の対応


def test_weekday_columns_sunday_start():
    # 既定は日曜始まり: 日, 月, .., 土
    assert weekday_columns("sunday") == ("sun", "mon", "tue", "wed", "thu",
                                         "fri", "sat")


def test_weekday_columns_monday_start():
    assert weekday_columns("monday") == ("mon", "tue", "wed", "thu", "fri",
                                         "sat", "sun")


def test_weekday_column_sunday_start():
    # 2026-10-24 は土曜: 日曜始まりでは最後の列 (6)
    assert weekday_column(date(2026, 10, 24), "sunday") == 6
    # 2026-10-25 は日曜: 先頭の列 (0)
    assert weekday_column(date(2026, 10, 25), "sunday") == 0


def test_weekday_column_monday_start():
    # 月曜始まりでは 10-24 (土) は 5 番目、10-25 (日) が最後
    assert weekday_column(date(2026, 10, 24), "monday") == 5
    assert weekday_column(date(2026, 10, 25), "monday") == 6


def test_weekday_column_matches_weekday_columns():
    # weekday_column の列番号は常に weekday_columns の並びと一致する
    for week_start in ("sunday", "monday"):
        cols = weekday_columns(week_start)
        for wd in range(7):
            day = date(2026, 10, 19 + wd)     # 10-19 (月) から 1 週間
            assert cols[weekday_column(day, week_start)] \
                == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][wd]


# ------------------------------------------------- 月のマス組み立て


def test_month_weeks_october_2026_sunday():
    # 2026-10-01 は木曜: 日曜始まりでは先頭 4 マスが空き、5 行
    weeks = month_weeks(2026, 10, "sunday")
    assert len(weeks) == 5
    assert [d.day if d else None for d in weeks[0]] == \
        [None, None, None, None, 1, 2, 3]
    assert [d.day for d in weeks[-1]] == [25, 26, 27, 28, 29, 30, 31]
    # すべての週が 7 マス
    assert all(len(w) == 7 for w in weeks)


def test_month_weeks_october_2026_monday():
    # 月曜始まり: 先頭 3 マスが空き、末尾 2 マスも空き
    weeks = month_weeks(2026, 10, "monday")
    assert len(weeks) == 5
    assert [d.day if d else None for d in weeks[0]] == [None, None, None, 1, 2, 3, 4]
    assert [d.day if d else None for d in weeks[-1]] == [26, 27, 28, 29, 30, 31, None]


def test_month_weeks_february_2026_28_days():
    # 2026-02 (28 日、2/1 は日曜): 日曜始まりならちょうど 4 行
    weeks = month_weeks(2026, 2, "sunday")
    assert len(weeks) == 4
    assert [d.day for d in weeks[0]] == list(range(1, 8))
    assert [d.day for d in weeks[-1]] == [22, 23, 24, 25, 26, 27, 28]


def test_month_weeks_feb_2021_28_days_monday():
    # 2021-02-01 は月曜: 月曜始まりで 4 行、前後の空きなし
    weeks = month_weeks(2021, 2, "monday")
    assert len(weeks) == 4
    assert weeks[0][0] == date(2021, 2, 1)


def test_month_weeks_31_day_month_spans_6_rows():
    # 2026-08-01 は土曜: 日曜始まりなら 6 行になる月もある
    weeks = month_weeks(2026, 8, "sunday")
    n_days = sum(1 for w in weeks for d in w if d)
    assert n_days == 31
    assert 4 <= len(weeks) <= 6
    # 日付は 1..月末日が一度ずつ
    flat = sorted(d.day for w in weeks for d in w if d)
    assert flat == list(range(1, 32))


@pytest.mark.parametrize("year,month", [
    (2024, 1), (2024, 2), (2026, 10), (2027, 7), (2026, 12),
])
@pytest.mark.parametrize("week_start", ["sunday", "monday"])
def test_month_weeks_day_counts(year, month, week_start):
    import calendar as cal
    weeks = month_weeks(year, month, week_start)
    days = [d for w in weeks for d in w if d]
    assert all(len(w) == 7 for w in weeks)
    assert len(weeks) in (4, 5, 6)
    assert sorted(days) == [date(year, month, d)
                            for d in range(1, cal.monthrange(year, month)[1] + 1)]
    # 各日の列番号は weekday_column と一致
    for w in weeks:
        for c, d in enumerate(w):
            if d:
                assert weekday_column(d, week_start) == c


# ------------------------------------------------- リセット日の印


def _card(provider, name, key, length, resets_at, used=10.0):
    win = Window(provider, key, key, used, resets_at, length)
    pu = ProviderUsage(provider, name, [win], None, NOW)
    return (pu, compute_pace(win, [], NOW))


def test_calendar_marks_weekly_window_marks_every_matching_day():
    # 週間窓: リセット日が 10/2 なら以降 7 日ごと。10/1 にリセットが
    # あれば 10/1 自身にも印が付く (前回ぶん)
    resets = datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)
    cards = [_card("claude", "Claude Max", "weekly", timedelta(days=7), resets)]
    marks = calendar_marks(cards, 2026, 10)
    assert [(p, [d.day for d in ds]) for p, _n, ds in marks] == \
        [("claude", [2, 9, 16, 23, 30])]


def test_calendar_marks_marks_the_day_of_reset_in_past():
    # 10/1 2:00 リセットの週間窓: 前回ぶんの 10/1 と以降 8, 15, 22, 29 に印
    resets = datetime(2026, 10, 1, 2, 0, 0, tzinfo=timezone.utc)
    cards = [_card("claude", "Claude", "weekly", timedelta(days=7), resets)]
    _p, _n, days = calendar_marks(cards, 2026, 10)[0]
    assert [d.day for d in days] == [1, 8, 15, 22, 29]


def test_calendar_marks_excludes_short_windows():
    # 5 時間窓は 1 日未満なので印の対象外
    resets = datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)
    cards = [_card("claude", "Claude", "5h", timedelta(hours=5), resets)]
    assert calendar_marks(cards, 2026, 10) == []


def test_calendar_marks_excludes_daily_windows():
    # 1 日ちょうどの窓は毎日に印が付いて意味が無いので対象外
    resets = datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)
    cards = [_card("devin", "Devin", "1d", timedelta(days=1), resets)]
    assert calendar_marks(cards, 2026, 10) == []


def test_calendar_marks_excludes_unknown_reset():
    resets = datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)
    win = Window("claude", "weekly", "週間", 10.0, None, timedelta(days=7))
    pu = ProviderUsage("claude", "Claude", [win], None, NOW)
    assert calendar_marks([(pu, compute_pace(win, [], NOW))], 2026, 10) == []


def test_calendar_marks_excludes_fetch_failed_cards():
    # pace = None (取得失敗) のサービスは対象外
    pu = ProviderUsage("claude", "Claude", [], "boom", NOW)
    resets = datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)
    cards = [(pu, None),
             _card("codex", "Codex", "weekly", timedelta(days=7), resets)]
    assert [p for p, _n, _d in calendar_marks(cards, 2026, 10)] == ["codex"]


def test_calendar_marks_merges_same_service_windows():
    # 同じサービスの複数窓 (週間 + 月間) は 1 つの印 (日付の和集合) にまとめる
    weekly = _card("claude", "Claude", "weekly", timedelta(days=7),
                   datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc))
    monthly = _card("claude", "Claude", "monthly", timedelta(days=30),
                    datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc))
    marks = calendar_marks([weekly, monthly], 2026, 10)
    assert len(marks) == 1
    assert marks[0][0] == "claude"
    assert [d.day for d in marks[0][2]] == [2, 9, 16, 20, 23, 30]


def test_calendar_marks_keeps_cards_order_and_names():
    resets = datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)
    cards = [_card("grok", "Grok", "weekly", timedelta(days=7), resets),
             _card("claude", "Claude Max", "weekly", timedelta(days=7), resets)]
    marks = calendar_marks(cards, 2026, 10)
    assert [(p, n) for p, n, _d in marks] == [("grok", "Grok"),
                                              ("claude", "Claude Max")]


def test_calendar_marks_local_dates_across_month_boundaries():
    # リセットが前月 (9/28) でも 10 月に入る分 (10/5, 12, ...) は印が付く
    resets = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    cards = [_card("codex", "Codex", "weekly", timedelta(days=7), resets)]
    _p, _n, days = calendar_marks(cards, 2026, 10)[0]
    assert [d.day for d in days] == [5, 12, 19, 26]


def test_calendar_marks_local_timezone_conversion():
    # UTC 10/1 22:00 のリセットは JST では 10/2。_local と同じ扱いで日付が変わる
    # (fixture で JST に固定しているので期待値も環境に依存しない)
    resets = datetime(2026, 10, 1, 22, 0, 0, tzinfo=timezone.utc)
    cards = [_card("codex", "Codex", "weekly", timedelta(days=7), resets)]
    _p, _n, days = calendar_marks(cards, 2026, 10)[0]
    assert days[0] == resets.astimezone(ZoneInfo("Asia/Tokyo")).date()


def test_calendar_marks_dst_boundary_uses_real_intervals(monkeypatch):
    # DST 境界: 実際のリセットは実時間間隔 (UTC で 168 時間ごと)。
    # 2026-03-05 23:30 (America/New_York, EST) の週間リセットは、
    # サマータイム突き抜けで現地 3/13 00:30 EDT に来る → 印は 13 日。
    # 壁時計どおり +7 日 (3/12 23:30) だと日付が 1 日ずれる。
    from quotacanvas import render as render_mod

    monkeypatch.setattr(render_mod, "_local",
                        lambda dt: dt.astimezone(ZoneInfo("America/New_York")))
    resets = datetime(2026, 3, 5, 23, 30, 0, tzinfo=ZoneInfo("America/New_York"))
    cards = [_card("codex", "Codex", "weekly", timedelta(days=7), resets)]
    _p, _n, days = calendar_marks(cards, 2026, 3)[0]
    assert [d.day for d in days] == [5, 13, 20, 27]


def test_calendar_marks_stay_inside_requested_month():
    # 前後に展開しても、戻り値の日はすべて指定月の中 (月跨ぎの鎖でも混ざらない)
    resets = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    cards = [_card("claude", "Claude", "weekly", timedelta(days=7), resets)]
    for year, month in [(2026, 9), (2026, 10), (2026, 11), (2027, 1)]:
        marks = calendar_marks(cards, year, month)
        for _p, _n, days in marks:
            assert all(d.year == year and d.month == month for d in days)
            assert days == sorted(days)


# ------------------------------------------------- display.week_start 設定


def _write_cfg(tmp_path, text, name="config.toml"):
    from pathlib import Path
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_config_week_start_default(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.display.week_start == "sunday"


def test_config_week_start_accepts_both_values(tmp_path):
    for value in ("monday", "sunday"):
        p = _write_cfg(tmp_path, '[display]\nweek_start = "%s"\n' % value)
        assert load_config(p).display.week_start == value


def test_config_week_start_invalid_value_raises(tmp_path):
    from quotacanvas.config import ConfigError
    p = _write_cfg(tmp_path, '[display]\nweek_start = "friday"\n')
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "display.week_start" in str(ei.value)


def test_config_week_start_roundtrips_through_dict(tmp_path):
    from quotacanvas.config import config_from_dict, config_to_dict
    p = _write_cfg(tmp_path, '[display]\nweek_start = "monday"\n')
    cfg = load_config(p)
    d = config_to_dict(cfg)
    assert d["display"]["week_start"] == "monday"
    assert config_from_dict(d).display.week_start == "monday"


def test_config_week_start_in_template():
    from quotacanvas.config import format_toml, Config
    text = format_toml(Config())
    assert 'week_start = "sunday"' in text


# ------------------------------------------------- main = "calendar" の配置


def test_config_accepts_calendar_panel_large(tmp_path):
    p = _write_cfg(tmp_path,
                  '[[layout.panels]]\nmain = "calendar"\nsize = "large"\n')
    cfg = load_config(p)
    assert cfg.layout.panels[0].main == CALENDAR_MAIN
    assert cfg.layout.panels[0].size == "large"


def test_config_accepts_calendar_panel_small(tmp_path):
    p = _write_cfg(tmp_path,
                  '[[layout.panels]]\nmain = "calendar"\nsize = "small"\n')
    cfg = load_config(p)
    assert cfg.layout.panels[0].size == "small"


def test_config_calendar_panel_ignores_subs(tmp_path):
    # subs を書いてもエラーにしない (local:<名前> パネルと同じ扱い)
    p = _write_cfg(tmp_path, """
        [[layout.panels]]
        main = "calendar"
        subs = ["claude:5h"]
    """)
    cfg = load_config(p)
    assert cfg.layout.panels[0].main == "calendar"


# ------------------------------------------------- 描画 (例外なし・寸法)


def _render_cards(n=3):
    cards = []
    for i in range(n):
        provider = "p%d" % i
        win = Window(provider, "weekly", "週間", 10.0,
                     NOW + timedelta(days=5), timedelta(days=7))
        pu = ProviderUsage(provider, provider.title(), [win], None, NOW)
        cards.append((pu, compute_pace(win, [], NOW)))
    return cards


def test_render_calendar_large_and_small():
    cards = _render_cards()
    layout = [PanelSpec(CALENDAR_MAIN),
              PanelSpec("p0:weekly"),
              PanelSpec(CALENDAR_MAIN, size="small"),
              PanelSpec("p1:weekly", size="small")]
    img = render(cards, NOW, layout=layout)
    assert img.size == (CANVAS_W, CANVAS_H)


def test_render_calendar_in_five_columns():
    # 5 列 (列幅約 365px) でも small / large とも例外なし
    cards = _render_cards(4)
    layout = [PanelSpec(CALENDAR_MAIN), PanelSpec("p0:weekly"),
              PanelSpec("p1:weekly"), PanelSpec("p2:weekly"),
              PanelSpec(CALENDAR_MAIN, size="small"),
              PanelSpec("p3:weekly", size="small")]
    img = render(cards, NOW, layout=layout)
    assert img.size == (CANVAS_W, CANVAS_H)


def test_render_calendar_with_monday_start():
    cards = _render_cards(2)
    layout = [PanelSpec(CALENDAR_MAIN), PanelSpec("p0:weekly"),
              PanelSpec(CALENDAR_MAIN, size="small"),
              PanelSpec("p1:weekly", size="small")]
    for week_start in ("monday", "sunday"):
        img = render(cards, NOW, layout=layout, week_start=week_start)
        assert img.size == (CANVAS_W, CANVAS_H)


def test_render_rejects_invalid_week_start():
    cards = _render_cards(1)
    layout = [PanelSpec(CALENDAR_MAIN)]
    with pytest.raises(ValueError):
        render(cards, NOW, layout=layout, week_start="friday")


def test_render_monday_start_changes_pixels():
    # 週の始まりが変わると曜日見出し行が動く = 出力が変わる
    cards = _render_cards(1)
    layout = [PanelSpec(CALENDAR_MAIN)]
    a = render(cards, NOW, layout=layout, week_start="sunday")
    b = render(cards, NOW, layout=layout, week_start="monday")
    assert a.tobytes() != b.tobytes()


def test_render_calendar_only_with_no_windows_marked():
    # 印が 1 つもない (リセット不明) ときも描ける
    win = Window("p0", "weekly", "週間", 10.0, None, timedelta(days=7))
    pu = ProviderUsage("p0", "P0", [win], None, NOW)
    cards = [(pu, compute_pace(win, [], NOW))]
    img = render(cards, NOW, layout=[PanelSpec(CALENDAR_MAIN)])
    assert img.size == (CANVAS_W, CANVAS_H)


# ------------------------------------------------- 描画の見た目 (ヘッダ余白・印)


def _drawn_texts(monkeypatch):
    """render 中に描いたテキスト (位置, 文言, フォント, アンカー) を記録する。"""
    from quotacanvas import render as render_mod

    records = []
    orig = render_mod._S.text

    def spy(self, pos, text, **kw):
        records.append((pos, text, kw.get("font"), kw.get("anchor")))
        return orig(self, pos, text, **kw)

    monkeypatch.setattr(render_mod._S, "text", spy)
    return records


def _bbox_of(draw, rec):
    pos, text, font, anchor = rec
    return draw.textbbox(pos, text, font=font, anchor=anchor)


def _header_gap(records, draw, i18n_mod):
    """見出し (年月) の bbox 下端と曜日行の bbox 上端の余白。"""
    title = i18n_mod.t("render.calendar.title", year=2026,
                       month=i18n_mod.t("render.month.oct"))
    weekdays = {i18n_mod.t("render.weekday." + k)
                for k in ("sun", "mon", "tue", "wed", "thu", "fri", "sat")}
    titles = [r for r in records if r[1] == title]
    wds = [r for r in records
           if r[1] in weekdays and len(r[1]) <= 3 and r[2].size in (10, 13)]
    assert len(titles) == 1
    assert len(wds) == 7
    t_bottom = _bbox_of(draw, titles[0])[3]
    w_top = min(_bbox_of(draw, r)[1] for r in wds)
    return t_bottom, w_top, w_top - t_bottom, titles[0][2].size


def test_calendar_header_gap_large(monkeypatch):
    # large: 見出しと曜日行が重ならない (下端と上端のあいだに余白)
    from PIL import Image, ImageDraw
    from quotacanvas import i18n

    records = _drawn_texts(monkeypatch)
    cards = _render_cards(1)
    img = render(cards, NOW, layout=[PanelSpec(CALENDAR_MAIN)])
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    t_bottom, w_top, gap, title_sz = _header_gap(records, draw, i18n)
    assert gap >= 2, (t_bottom, w_top)
    assert title_sz == 20


def test_calendar_header_gap_small(monkeypatch):
    from PIL import Image, ImageDraw
    from quotacanvas import i18n

    records = _drawn_texts(monkeypatch)
    cards = _render_cards(2)
    render(cards, NOW, layout=[PanelSpec(CALENDAR_MAIN, size="small"),
                               PanelSpec("p0:weekly", size="small")])
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    t_bottom, w_top, gap, title_sz = _header_gap(records, draw, i18n)
    assert gap >= 2, (t_bottom, w_top)
    assert title_sz >= 15     # small の見出しは 14 より大きくする


def _max_color_run(px, color, x0, x1, y0, y1, tol=8):
    best = run = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            if all(abs(px[x, y][i] - color[i]) <= tol for i in range(3)):
                run += 1
                best = max(best, run)
            else:
                run = 0
    return best


def test_calendar_mark_dot_size_large():
    # large の印は直径約 12px (以前の 6px の 2 倍): ブランド色の最大横連続長で検証
    from quotacanvas.render import _brand_color

    resets = datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)
    win = Window("claude", "weekly", "週間", 10.0, resets, timedelta(days=7))
    pu = ProviderUsage("claude", "Claude", [win], None, NOW)
    img = render([(pu, compute_pace(win, [], NOW))], NOW,
                 layout=[PanelSpec(CALENDAR_MAIN)])
    run = _max_color_run(img.load(), _brand_color(None, "claude"), 16, 400, 52, 454)
    assert run >= 8, run      # 直径 12 の丸 (以前の直径 6 は 4-5 しか数えられない)


def test_calendar_mark_dot_size_small():
    # small の印は直径約 6px (以前の 4px の 1.5 倍)
    from quotacanvas.render import _brand_color

    resets = datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)
    win = Window("claude", "weekly", "週間", 10.0, resets, timedelta(days=7))
    pu = ProviderUsage("claude", "Claude", [win], None, NOW)
    cards = [(pu, compute_pace(win, [], NOW)),
             (_render_cards(1)[0])]
    img = render(cards, NOW,
                 layout=[PanelSpec(CALENDAR_MAIN, size="small"),
                         PanelSpec("p1:weekly", size="small")])
    # small のカレンダーは上段 (2 パネルで 1 列に 2 段になる)
    run = _max_color_run(img.load(), _brand_color(None, "claude"),
                         16, 1904, 52, 245)
    assert run >= 5, run      # 直径 6 の丸 (以前の直径 4 は 3 程度しか数えられない)


# ------------------------------------- 同日の全サービス印・subs 予約・空 cards


def _count_color(px, color, x0, x1, y0, y1, tol=4):
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            if all(abs(px[x, y][i] - color[i]) <= tol for i in range(3)):
                n += 1
    return n


_SAME_DAY_NAMES = {"claude": "Claude Max", "codex": "Codex", "glm": "GLM",
                   "grok": "Grok", "gemini": "Gemini"}


def _same_day_reset_cards(providers, key="weekly", length=timedelta(days=7)):
    """指定サービスすべてが同じ日 (10/2) にリセットする週間窓のダミーデータ。"""
    resets = datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)
    return [_card(p, _SAME_DAY_NAMES[p], key, length, resets)
            for p in providers]


def _five_providers_same_reset_day():
    """5 サービスが同じ日 (10/2) に週間リセットするダミーデータ。"""
    return _same_day_reset_cards(("claude", "codex", "glm", "grok", "gemini"))


def test_calendar_shows_all_services_on_same_day_large():
    # 5 サービスが同日リセットでも切り捨てない: 5 列の狭い large カレンダーで
    # 全サービスのブランド色の丸が描かれている (丸は縮んで全 5 個並ぶ)
    from quotacanvas.render import _brand_color

    cards = _five_providers_same_reset_day()
    layout = [PanelSpec(CALENDAR_MAIN),
              PanelSpec("claude:weekly"), PanelSpec("codex:weekly"),
              PanelSpec("glm:weekly"), PanelSpec("grok:weekly", size="small"),
              PanelSpec("gemini:weekly", size="small")]
    img = render(cards, NOW, layout=layout)
    px = img.load()
    for prov, _name in [("claude", ""), ("codex", ""), ("glm", ""),
                        ("grok", ""), ("gemini", "")]:
        n = _count_color(px, _brand_color(None, prov), 16, 380, 120, 410)
        assert n >= 10, (prov, n)


def test_calendar_shows_all_services_on_same_day_small():
    from quotacanvas.render import _brand_color

    cards = _five_providers_same_reset_day()
    layout = [PanelSpec("claude:weekly"), PanelSpec("codex:weekly"),
              PanelSpec("glm:weekly"), PanelSpec("grok:weekly"),
              PanelSpec(CALENDAR_MAIN, size="small"),
              PanelSpec("gemini:weekly", size="small")]
    img = render(cards, NOW, layout=layout)
    px = img.load()
    for prov in ("claude", "codex", "glm", "grok", "gemini"):
        n = _count_color(px, _brand_color(None, prov), 1540, 1904, 95, 230)
        assert n >= 8, (prov, n)


def test_render_layout_calendar_subs_not_reserved(tmp_path):
    # calendar パネルに明示 subs があってもそれは予約しない: 他パネルの
    # 自動サブからその窓が消えない (subs は描画されないので無視される)
    from quotacanvas.cli import _render_layout
    from quotacanvas.config import Config, Display, Layout, Panel

    weekly = Window("claude", "weekly", "週間", 10.0,
                    NOW + timedelta(days=5), timedelta(days=7))
    five = Window("claude", "5h", "5時間", 20.0,
                  NOW + timedelta(hours=3), timedelta(hours=5))
    pu = ProviderUsage("claude", "Claude", [weekly, five], None, NOW)
    cards = [(pu, compute_pace(w, [], NOW)) for w in (weekly, five)]
    cfg = Config(display=Display(),
                 layout=Layout(panels=[Panel(main="calendar",
                                             subs=["claude:5h"]),
                                       Panel(main="claude:weekly")]))
    specs = _render_layout(cfg, cards)
    assert [(s.main, s.subs) for s in specs] == \
        [("calendar", []), ("claude:weekly", ["claude:5h"])]


def test_render_frame_empty_cards_with_calendar_layout():
    # cards が空でも配置にカレンダーがあればエラー画面ではなく通常描画
    from quotacanvas.cli import _render_frame
    from quotacanvas.config import Config
    from quotacanvas.i18n import t
    from quotacanvas.render import render_error

    err = render_error(t("cli.no_usage"), NOW)
    assert _render_frame([], NOW, Config(), None).tobytes() == err.tobytes()
    img = _render_frame([], NOW, Config(), [PanelSpec(CALENDAR_MAIN)])
    assert img.size == (CANVAS_W, CANVAS_H)
    assert img.tobytes() != err.tobytes()
    # カレンダー以外のパネルは解決できず列ごと消えるが、カレンダーは残る
    mixed = _render_frame([], NOW, Config(),
                          [PanelSpec(CALENDAR_MAIN), PanelSpec("claude:weekly")])
    assert mixed.tobytes() != err.tobytes()


def test_calendar_four_services_same_day_large():
    # 回帰: 5 列の large で 4 サービス同日リセットのとき、半径を決めた同じ式
    # から収容数を出さないと浮動小数点誤差で 4.0 → 3.999.. になり、末尾の
    # サービス (Grok) の印が全日欠けた。全 4 サービスの丸が描かれること。
    from quotacanvas.render import _brand_color

    cards = _same_day_reset_cards(("claude", "codex", "glm", "grok"))
    cards.append(_card("gemini", "Gemini", "5h", timedelta(hours=5),
                       datetime(2026, 10, 3, 0, 0, 0, tzinfo=timezone.utc)))
    layout = [PanelSpec(CALENDAR_MAIN), PanelSpec("claude:weekly"),
              PanelSpec("codex:weekly"), PanelSpec("glm:weekly"),
              PanelSpec("grok:weekly", size="small"),
              PanelSpec("gemini:5h", size="small")]
    img = render(cards, NOW, layout=layout)
    px = img.load()
    for prov in ("claude", "codex", "glm", "grok"):
        n = _count_color(px, _brand_color(None, prov), 16, 380, 120, 410, tol=2)
        assert n >= 100, (prov, n)   # 5 日ぶんの丸 (欠けると ~0)


def test_calendar_four_services_same_day_small():
    from quotacanvas.render import _brand_color

    cards = _same_day_reset_cards(("claude", "codex", "glm", "grok"))
    cards.append(_card("gemini", "Gemini", "5h", timedelta(hours=5),
                       datetime(2026, 10, 3, 0, 0, 0, tzinfo=timezone.utc)))
    layout = [PanelSpec("claude:weekly"), PanelSpec("codex:weekly"),
              PanelSpec("glm:weekly"), PanelSpec("grok:weekly"),
              PanelSpec(CALENDAR_MAIN, size="small"),
              PanelSpec("gemini:5h", size="small")]
    img = render(cards, NOW, layout=layout)
    px = img.load()
    for prov in ("claude", "codex", "glm", "grok"):
        n = _count_color(px, _brand_color(None, prov), 1540, 1904, 95, 230,
                         tol=2)
        assert n >= 60, (prov, n)
