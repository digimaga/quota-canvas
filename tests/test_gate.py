"""gate.py のテスト: 旧 ~/.claude/bin/quota-gate の主要な挙動の移植。

- 各ペース status → verdict の対応、floor、最悪窓、内訳 (part_of) 除外
- stale / missing / error は unknown (fail-open)
- burn の昇格と期限切れ、リセット権表示、line / check / stale の文字列 (ja)
- [routing] 設定の読み込み・検証・保存
- status.json の "windows" / "routing" の書き出し
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import gate, i18n
from quotacanvas.config import ConfigError, config_from_dict, config_to_dict, format_toml, load_config
from quotacanvas.gate import (
    RoutingResult,
    WorkerResult,
    evaluate,
    format_line,
    format_stale_line,
    parse_dt,
    pct_str,
)

NOW = datetime(2026, 10, 26, 8, 39, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _restore_language():
    """set_language したテストが言語を globals に残さないように戻す。"""
    prev = i18n.current_language()
    yield
    i18n.set_language(prev)


def cfg(**kw):
    from quotacanvas.config import Routing

    return Routing(**kw)


def win(wid, provider, status, remaining, *, resets=NOW + timedelta(days=3),
        exhausts=None, fetched=NOW, stale_error=None, part_of=None, key=None):
    entry = {
        "id": wid,
        "provider": provider,
        "key": key or wid.split(":", 1)[1],
        "part_of": part_of,
        "used": round(100.0 - remaining, 1),
        "remaining": remaining,
        "status": status,
        "resets_at": resets.isoformat(timespec="seconds") if resets else None,
    }
    if exhausts is not None:
        entry["exhausts_at"] = exhausts.isoformat(timespec="seconds")
    if fetched is not None:
        entry["fetched_at"] = fetched.isoformat(timespec="seconds")
    if stale_error is not None:
        entry["stale_error"] = stale_error
    return entry


def status(*windows, errors=None, credits=None, updated=NOW):
    data = {"updated_at": updated.isoformat(timespec="seconds"), "windows": list(windows)}
    if errors is not None:
        data["errors"] = errors
    if credits is not None:
        data["reset_credits"] = credits
    return data


# --- 各 status → verdict (quota-gate の判定表) --------------------------------


def test_pace_status_to_verdict():
    cases = {
        "exhausted": "stop", "over": "reserve", "tight": "go", "on_track": "go",
        "unknown": "go", "spare": "boost", "fresh": "boost",
    }
    for pace, verdict in cases.items():
        result = evaluate(status(win("glm:5h", "glm", pace, 50.0)), cfg(), {}, NOW)
        assert result.workers[0].verdict == verdict, pace


def test_reason_matches_pace_status():
    result = evaluate(status(win("glm:5h", "glm", "over", 50.0)), cfg(), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("reserve", "over")
    result = evaluate(status(win("glm:5h", "glm", "spare", 50.0)), cfg(), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("boost", "spare")
    # 壊れた status は unknown 扱いで go
    result = evaluate(status(win("glm:5h", "glm", "weird", 50.0)), cfg(), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("go", "unknown")


# --- floor (残り floor_percent 未満は stop) -------------------------------------


def test_below_floor_stops_when_reset_is_far_away():
    # ペースは余裕ありでも残り 4.9% なら stop
    result = evaluate(status(win("glm:5h", "glm", "spare", 4.9)), cfg(), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("stop", "below_floor")


@pytest.mark.parametrize("hours,pace,expected", [
    (72, "spare", ("stop", "below_floor")),
    (10, "spare", ("boost", "spare")),
    (10, "on_track", ("go", "on_track")),
    (10, "over", ("reserve", "over")),
    (24, "spare", ("stop", "below_floor")),
    (24 - 1 / 3600, "spare", ("boost", "spare")),
])
def test_floor_depends_on_time_until_reset(hours, pace, expected):
    entry = win("glm:weekly", "glm", pace, 4, resets=NOW + timedelta(hours=hours))
    entry["length"] = 7 * 24 * 3600
    worker = evaluate(status(entry), cfg(), {}, NOW).workers[0]
    assert (worker.verdict, worker.reason) == expected


@pytest.mark.parametrize("reset", [None, "broken"])
def test_floor_applies_when_reset_missing_or_invalid(reset):
    entry = win("glm:weekly", "glm", "spare", 4)
    entry["resets_at"] = reset
    worker = evaluate(status(entry), cfg(), {}, NOW).workers[0]
    assert (worker.verdict, worker.reason) == ("stop", "below_floor")


@pytest.mark.parametrize("hours", [4, -1])
def test_zero_floor_hours_always_applies_floor(hours):
    entry = win("glm:5h", "glm", "spare", 4, resets=NOW + timedelta(hours=hours))
    entry["length"] = 5 * 3600
    worker = evaluate(status(entry), cfg(floor_hours=0), {}, NOW).workers[0]
    assert (worker.verdict, worker.reason) == ("stop", "below_floor")


def test_custom_floor_hours_changes_boundary():
    entry = win("glm:weekly", "glm", "spare", 4, resets=NOW + timedelta(hours=4))
    entry["length"] = 7 * 24 * 3600
    assert evaluate(status(entry), cfg(floor_hours=4), {}, NOW).workers[0].verdict == "stop"
    assert evaluate(status(entry), cfg(floor_hours=4.5), {}, NOW).workers[0].verdict == "boost"


def test_short_window_below_floor_still_uses_hysteresis():
    reset = NOW + timedelta(minutes=20)
    entry = short_window(30, reset=reset)
    entry["remaining"] = 4
    first = evaluate(status(entry), cfg(), {}, NOW)
    assert (first.workers[0].verdict, first.workers[0].reason) == ("reserve", "over")
    entry = short_window(25, reset=reset)
    entry["remaining"] = 4
    retained = evaluate(status(entry), cfg(), {}, NOW, previous=first.to_dict())
    assert retained.workers[0].verdict == "reserve"
    entry = short_window(7, reset=reset)
    entry["remaining"] = 4
    near = evaluate(status(entry), cfg(), {}, NOW, previous=retained.to_dict())
    assert (near.workers[0].verdict, near.workers[0].reason) == ("go", "over_near_reset")
    entry["status"] = "exhausted"
    entry["remaining"] = 0
    assert evaluate(status(entry), cfg(), {}, NOW).workers[0].verdict == "stop"


@pytest.mark.parametrize("minutes,length,margin,expected", [
    (120, 5, None, ("stop", "below_floor")),
    (30, 5, None, ("stop", "below_floor")),
    (20, 5, None, ("boost", "spare")),
    (6, 1, None, ("stop", "below_floor")),
    (5, 1, None, ("boost", "spare")),
    (10, 5, 10, ("stop", "below_floor")),
    (9, 5, 10, ("boost", "spare")),
    (20, 5, 45, ("boost", "spare")),
])
def test_short_window_floor_uses_margin(minutes, length, margin, expected):
    entry = win("glm:5h", "glm", "spare", 4, resets=NOW + timedelta(minutes=minutes))
    entry["length"] = length * 3600
    worker = evaluate(status(entry), cfg(short_over_margin_minutes=margin), {}, NOW).workers[0]
    assert (worker.verdict, worker.reason) == expected


def test_floor_missing_length_uses_floor_hours():
    entry = win("glm:5h", "glm", "spare", 4, resets=NOW + timedelta(hours=2))
    assert evaluate(status(entry), cfg(), {}, NOW).workers[0].verdict == "boost"
    assert evaluate(status(entry), cfg(floor_hours=2), {}, NOW).workers[0].verdict == "stop"


@pytest.mark.parametrize("length,expected", [(24 * 3600 - 1, "stop"), (24 * 3600, "boost")])
def test_floor_selects_margin_by_window_length(length, expected):
    entry = win("glm:window", "glm", "spare", 4, resets=NOW + timedelta(hours=3))
    entry["length"] = length
    assert evaluate(status(entry), cfg(), {}, NOW).workers[0].verdict == expected


@pytest.mark.parametrize("reset", [None, "broken"])
def test_short_window_floor_applies_if_reset_unknown(reset):
    entry = short_window(7)
    entry["remaining"] = 4
    entry["resets_at"] = reset
    worker = evaluate(status(entry), cfg(), {}, NOW).workers[0]
    assert (worker.verdict, worker.reason) == ("stop", "below_floor")


def test_floor_boundary_is_not_below():
    # ちょうど 5% は stop ではない (未満のみ)
    result = evaluate(status(win("glm:5h", "glm", "spare", 5.0)), cfg(), {}, NOW)
    assert result.workers[0].verdict == "boost"
    result = evaluate(status(win("glm:5h", "glm", "spare", 4.0)), cfg(floor_percent=4.0), {}, NOW)
    assert result.workers[0].verdict == "boost"


def test_exhausted_reason_is_exhausted():
    result = evaluate(status(win("glm:5h", "glm", "exhausted", 0.0)), cfg(), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("stop", "exhausted")


# --- 複数窓: 一番悪い窓で判定・表示 ----------------------------------------------


def test_worst_window_wins():
    data = status(
        win("glm:5h", "glm", "spare", 60.0, resets=NOW + timedelta(hours=2)),
        win("glm:weekly", "glm", "over", 85.0, exhausts=NOW + timedelta(days=2)),
    )
    result = evaluate(data, cfg(workers={"glm": ["glm:5h", "glm:weekly"]}), {}, NOW)
    w = result.workers[0]
    assert w.verdict == "reserve"
    assert w.remaining == 85.0
    assert w.windows == ["glm:5h", "glm:weekly"]


def test_worst_window_tiebreaks_on_remaining():
    # 同じ verdict なら残量が少ない方 (残 30% < 残 50%)
    data = status(
        win("glm:5h", "glm", "spare", 50.0),
        win("glm:weekly", "glm", "spare", 30.0),
    )
    result = evaluate(data, cfg(workers={"glm": ["glm:5h", "glm:weekly"]}), {}, NOW)
    assert result.workers[0].remaining == 30.0


def test_stop_outranks_reserve_in_worst_pick():
    data = status(
        win("glm:5h", "glm", "over", 80.0),
        win("glm:weekly", "glm", "exhausted", 95.0),
    )
    result = evaluate(data, cfg(workers={"glm": ["glm:5h", "glm:weekly"]}), {}, NOW)
    assert result.workers[0].verdict == "stop"


# --- 内訳窓 (part_of) はどの指定でも判定に入れない -------------------------------


def test_breakdown_windows_never_judged():
    data = status(
        win("grok:weekly", "grok", "spare", 70.0),
        win("grok:weekly_build", "grok", "over", 99.0, part_of="weekly"),
    )
    # ワイルドカードでも内訳は入らない
    result = evaluate(data, cfg(workers={"grok": ["grok:*"]}), {}, NOW)
    w = result.workers[0]
    assert (w.verdict, w.windows) == ("boost", ["grok:weekly"])
    # id を直接書いても内訳は入らない (だけなら missing)
    result = evaluate(data, cfg(workers={"grok": ["grok:weekly_build"]}), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("unknown", "missing")


def test_breakdown_still_triggers_worker_stale():
    # 内訳窓自体は判定に入れないが、同じ provider のデータが古ければ stale
    data = status(
        win("grok:weekly", "grok", "spare", 70.0),
        win("grok:weekly_build", "grok", "spare", 71.0, part_of="weekly",
            fetched=NOW - timedelta(hours=2)),
    )
    result = evaluate(data, cfg(workers={"grok": ["grok:weekly"]}), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("unknown", "stale")


# --- ワイルドカード --------------------------------------------------------------


def test_wildcard_prefix():
    data = status(
        win("grok:weekly", "grok", "over", 80.0),
        win("grok:5h", "grok", "spare", 70.0),
    )
    result = evaluate(data, cfg(workers={"grok": ["grok:*"]}), {}, NOW)
    assert result.workers[0].verdict == "reserve"  # over の grok:weekly も入る
    result = evaluate(data, cfg(workers={"grok": ["grok:5*"]}), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].windows) \
        == ("boost", ["grok:5h"])  # grok:5h だけ (grok:weekly は接頭不一致)


def test_cards_used_when_windows_missing():
    # windows を書く前のエンジンの status.json (別の Mac からのコピー) は cards で代用
    data = status(win("glm:5h", "glm", "over", 60.0))
    data["cards"] = data.pop("windows")
    result = evaluate(data, cfg(workers={"glm": ["glm:5h"]}), {}, NOW)
    assert result.workers[0].verdict == "reserve"


# --- stale / missing / error → unknown ------------------------------------------


def test_stale_error_marks_unknown():
    data = status(win("glm:5h", "glm", "spare", 50.0, stale_error="HTTP 429"))
    result = evaluate(data, cfg(), {}, NOW)
    w = result.workers[0]
    assert (w.verdict, w.reason, w.stale) == ("unknown", "stale", True)


def test_old_fetched_at_marks_unknown():
    data = status(win("glm:5h", "glm", "spare", 50.0,
                      fetched=NOW - timedelta(minutes=25)))
    result = evaluate(data, cfg(), {}, NOW)  # 既定 stale_minutes=20
    assert (result.workers[0].verdict, result.workers[0].reason) == ("unknown", "stale")
    # stale_minutes を広げれば新しい扱い
    result = evaluate(data, cfg(stale_minutes=30), {}, NOW)
    assert result.workers[0].verdict == "boost"


def test_broken_fetched_at_marks_unknown():
    data = status(win("glm:5h", "glm", "spare", 50.0, fetched=NOW))
    data["windows"][0]["fetched_at"] = "not-a-date"
    result = evaluate(data, cfg(), {}, NOW)
    assert result.workers[0].reason == "stale"


def test_missing_fetched_at_is_trusted_when_file_fresh():
    data = status(win("glm:5h", "glm", "spare", 50.0, fetched=None))
    result = evaluate(data, cfg(), {}, NOW)
    assert result.workers[0].verdict == "boost"


def test_missing_windows_give_unknown_missing():
    result = evaluate(status(), cfg(workers={"glm": ["glm:5h"]}), {}, NOW)
    w = result.workers[0]
    assert (w.verdict, w.reason) == ("unknown", "missing")
    assert w.windows == []


def test_error_provider_gives_unknown_error():
    data = status(win("glm:5h", "glm", "spare", 50.0),
                  errors=[{"provider": "glm", "name": "GLM", "error": "HTTP 401"}])
    result = evaluate(data, cfg(), {}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("unknown", "error")


def test_error_provider_propagates_to_windows_of_same_provider():
    # argon は gemini provider のデータ: gemini の取得失敗で一緒に unknown (quota-gate と同じ)
    data = status(win("gemini:argon", "gemini", "spare", 50.0),
                  errors=[{"provider": "gemini", "error": "agy 見つからず"}])
    result = evaluate(data, cfg(workers={"argon": ["gemini:argon"]}), {}, NOW)
    assert result.workers[0].reason == "error"


def test_stale_wins_over_error():
    data = status(win("glm:5h", "glm", "spare", 50.0, stale_error="HTTP 429"),
                  errors=[{"provider": "glm", "error": "HTTP 500"}])
    result = evaluate(data, cfg(), {}, NOW)
    assert result.workers[0].reason == "stale"


def test_unknown_status_json_is_fail_open():
    # windows キーが無い古い status.json でも例外を出さない
    result = evaluate({"updated_at": NOW.isoformat()}, cfg(workers={"glm": ["glm:5h"]}), {}, NOW)
    assert result.workers[0].verdict == "unknown"
    result = evaluate(None, cfg(), {}, NOW)
    assert result.workers == []


# --- 既定のワーカー (workers 未指定) ---------------------------------------------


def test_default_workers_are_present_providers_with_independent_windows():
    data = status(
        win("claude:weekly", "claude", "spare", 60.0),
        win("claude:5h", "claude", "over", 95.0),
        win("grok:weekly", "grok", "spare", 70.0),
        win("grok:weekly_build", "grok", "spare", 71.0, part_of="weekly"),
        errors=[{"provider": "codex", "error": "HTTP 401"}],
    )
    result = evaluate(data, cfg(), {}, NOW)
    assert [w.name for w in result.workers] == ["claude", "codex", "grok"]  # canonical 順
    claude = result.workers[0]
    assert claude.windows == ["claude:weekly", "claude:5h"]  # 内訳を除く独立窓すべて
    assert claude.verdict == "reserve"  # claude:5h が over なので一番悪い方
    assert result.workers[1].reason == "error"  # 窓が無くても errors に出ている provider は出す


def test_default_worker_order_follows_known_providers():
    data = status(win("glm:5h", "glm", "spare", 50.0),
                  win("claude:weekly", "claude", "spare", 50.0))
    result = evaluate(data, cfg(), {}, NOW)
    assert [w.name for w in result.workers] == ["claude", "glm"]


# --- burn (使い切りモード) -------------------------------------------------------


def test_burn_promotes_go_and_reserve_to_boost():
    data = status(win("glm:5h", "glm", "on_track", 50.0),
                  win("grok:weekly", "grok", "over", 80.0))
    burn = {"glm": NOW + timedelta(hours=1), "grok": NOW + timedelta(hours=1)}
    result = evaluate(data, cfg(workers={"glm": ["glm:5h"], "grok": ["grok:weekly"]}),
                      burn, NOW)
    glm, grok = result.workers
    assert (glm.verdict, glm.reason) == ("boost", "burn")
    assert (grok.verdict, grok.reason) == ("boost", "burn")
    assert glm.burn_until is not None


def test_burn_keeps_boost_and_does_not_lift_stop_or_unknown():
    data = status(win("glm:5h", "glm", "spare", 50.0),
                  win("grok:weekly", "grok", "exhausted", 100.0))
    burn = {"glm": NOW + timedelta(hours=1), "grok": NOW + timedelta(hours=1),
            "codex": NOW + timedelta(hours=1)}
    result = evaluate(data, cfg(workers={"glm": ["glm:5h"], "grok": ["grok:weekly"],
                                         "codex": ["codex:weekly"]}), burn, NOW)
    glm, grok, codex = result.workers
    assert (glm.verdict, glm.reason) == ("boost", "spare")  # もと boost: 理由は変わらない
    assert (grok.verdict, grok.reason) == ("stop", "exhausted")  # stop は上げない
    assert grok.burn_until is not None  # line では stop にも使い切りを添える (quota-gate と同じ)
    assert (codex.verdict, codex.reason) == ("unknown", "missing")
    assert codex.burn_until is None


def test_expired_burn_is_ignored():
    data = status(win("glm:5h", "glm", "on_track", 50.0))
    burn = {"glm": NOW - timedelta(minutes=1)}
    result = evaluate(data, cfg(workers={"glm": ["glm:5h"]}), burn, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("go", "on_track")
    assert result.workers[0].burn_until is None


# --- リセット権 (表示だけ) --------------------------------------------------------


def test_reset_credits_counted_for_display_only():
    data = status(
        win("codex:weekly", "codex", "on_track", 50.0),
        credits={"codex": [
            {"title": "Full reset", "expires_at": (NOW + timedelta(days=5)).isoformat()},
            {"title": "Full reset", "expires_at": (NOW - timedelta(days=1)).isoformat()},  # 期限切れ
            {"title": "Full reset", "expires_at": None},  # 期限不明は有効扱い
        ]},
    )
    result = evaluate(data, cfg(workers={"codex": ["codex:weekly"]}), {}, NOW)
    assert result.reset_credits == {"codex": 2}
    # 判定は変わらない
    assert result.workers[0].verdict == "go"


# --- line の文字列 (ja は quota-gate と同じ形) ------------------------------------


def test_line_matches_quota_gate_format():
    now = datetime.now().astimezone()
    today_reset = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
    later_reset = (now + timedelta(days=3, hours=2)).replace(second=0, microsecond=0)
    exhaust = (now + timedelta(days=2, hours=1)).replace(second=0, microsecond=0)
    data = status(
        win("glm:5h", "glm", "spare", 49.0, resets=today_reset, fetched=now),
        win("grok:weekly", "grok", "over", 85.0, resets=later_reset, exhausts=exhaust,
            fetched=now),
        win("codex:weekly", "codex", "over", 87.0, resets=later_reset, exhausts=exhaust,
            fetched=now),
        win("gemini:weekly", "gemini", "spare", 89.9, resets=None, fetched=now),
        updated=now,
    )
    i18n.set_language("ja")
    result = evaluate(data, cfg(workers={
        "glm": ["glm:5h"], "grok": ["grok:weekly"], "codex": ["codex:weekly"],
        "gemini": ["gemini:weekly"]}), {}, now)
    line = format_line(result, now, "ja")
    assert line == (
        f"quota: glm=boost(49%,~{gate.fmt_reset(today_reset, now)}) "
        f"grok=reserve(85%,ペース超過·~{gate.fmt_reset(exhaust, now)}枯渇見込み,"
        f"~{gate.fmt_reset(later_reset, now)}) "
        f"codex=reserve(87%,ペース超過·~{gate.fmt_reset(exhaust, now)}枯渇見込み,"
        f"~{gate.fmt_reset(later_reset, now)}) gemini=boost(90%,spare) "
        f"({gate.fmt_hhmm(now)}時点)"
    )


def test_line_shows_credits_burn_and_no_reset_time():
    now = datetime.now().astimezone()
    burn_until = now.replace(hour=23, minute=59, second=0, microsecond=0)
    data = status(
        win("codex:weekly", "codex", "on_track", 87.0,
            resets=(now + timedelta(days=3)).replace(second=0, microsecond=0),
            fetched=now),
        win("gemini:weekly", "gemini", "spare", 89.9, resets=None, fetched=now),
        credits={"codex": [{"title": "Full reset",
                            "expires_at": (now + timedelta(days=5)).isoformat()}]},
        updated=now,
    )
    i18n.set_language("ja")
    result = evaluate(data, cfg(workers={"codex": ["codex:weekly"],
                                         "gemini": ["gemini:weekly"]}),
                      {"codex": burn_until}, now)
    line = format_line(result, now, "ja")
    assert line == (
        "quota: codex=boost(87%,リセット権1,使い切り~23:59,"
        f"~{gate.fmt_reset(data['windows'][0]['resets_at'], now)}) "
        f"gemini=boost(90%,spare) ({gate.fmt_hhmm(now)}時点)"
    )


def test_line_unknown_and_stale_markers():
    now = datetime.now().astimezone()
    data = status(
        win("glm:5h", "glm", "spare", 50.0, stale_error="HTTP 429"),
        updated=now,
    )
    i18n.set_language("ja")
    result = evaluate(data, cfg(workers={"glm": ["glm:5h"], "grok": ["grok:weekly"]}), {}, now)
    assert format_line(result, now, "ja") == f"quota: glm=stale grok=? ({gate.fmt_hhmm(now)}時点)"


def test_line_rounds_remaining_like_quota_gate():
    now = datetime.now().astimezone()
    data = status(win("glm:5h", "glm", "spare", 49.4, resets=now + timedelta(hours=2),
                      fetched=now),
                  updated=now)
    result = evaluate(data, cfg(workers={"glm": ["glm:5h"]}), {}, now)
    assert "glm=boost(49%," in format_line(result, now, "ja")
    assert pct_str(None) == "?%"


def test_line_without_remaining_shows_question_mark():
    now = datetime.now().astimezone()
    data = status(win("glm:5h", "glm", "on_track", 50.0, fetched=now))
    del data["windows"][0]["remaining"]
    result = evaluate(data, cfg(workers={"glm": ["glm:5h"]}), {}, now)
    assert "=go(?%" in format_line(result, now, "ja")


def test_line_english_wording():
    now = datetime.now().astimezone()
    data = status(win("glm:5h", "glm", "over", 85.0,
                      resets=(now + timedelta(days=3)).replace(second=0, microsecond=0),
                      exhausts=(now + timedelta(days=2)).replace(second=0, microsecond=0),
                      fetched=now),
                  updated=now)
    result = evaluate(data, cfg(workers={"glm": ["glm:5h"]}), {}, now)
    line = format_line(result, now, "en")
    expected = ("glm=reserve(85%,pace over"
                f" · ~{gate.fmt_reset(data['windows'][0]['exhausts_at'], now)} projected to exhaust,"
                f"~{gate.fmt_reset(data['windows'][0]['resets_at'], now)}) "
                f"(as of {gate.fmt_hhmm(now)})")
    assert expected in line


# --- stale 行 --------------------------------------------------------------------


def test_stale_lines_match_quota_gate(tmp_path):
    now = datetime.now().astimezone()
    i18n.set_language("ja")
    assert format_stale_line("missing", tmp_path / "s.json", None, now, "ja") \
        == "quota: stale（status.json なし）"
    old_at = now - timedelta(hours=2)
    old = {"updated_at": old_at.isoformat(timespec="seconds")}
    assert format_stale_line("old", tmp_path / "s.json", old, now, "ja") \
        == f"quota: stale（{gate.fmt_hhmm(old_at)}時点・古いので残量は判断に使わない）"
    assert format_stale_line("broken", tmp_path / "s.json", old, now, "ja") \
        == f"quota: stale（{gate.fmt_hhmm(old_at)}時点・読めないので残量は判断に使わない）"
    # updated_at も無ければファイルの mtime に落ちる
    (tmp_path / "s.json").write_text("{}", encoding="utf-8")
    line = format_stale_line("broken", tmp_path / "s.json", None, now, "ja")
    assert "時点・読めないので残量は判断に使わない）" in line


# --- check メッセージ --------------------------------------------------------------


def test_check_messages():
    now = datetime.now().astimezone()
    reset = (now + timedelta(hours=3)).replace(second=0, microsecond=0)
    i18n.set_language("ja")
    stop = WorkerResult("glm", "stop", "below_floor", remaining=3.0)
    assert gate.check_message(stop, now, "ja") \
        == "quota-canvas: glm=stop（残り3%）。QUOTA_GATE_FORCE=1 で無視"
    reserve = WorkerResult("glm", "reserve", "over", remaining=85.0,
                           resets_at=reset.isoformat(timespec="seconds"))
    assert gate.check_message(reserve, now, "ja") \
        == (f"quota-canvas: glm=reserve（残り85%、~{gate.fmt_reset(reset, now)}）。"
            "可能なら他のワーカーを優先")
    unknown = WorkerResult("glm", "unknown", "stale", stale=True)
    assert gate.check_message(unknown, now, "ja") \
        == "quota-canvas: glm=unknown（stale）。判定できないためそのまま続行"
    assert gate.check_message(WorkerResult("glm", "go", "on_track"), now, "ja") is None
    assert gate.check_message(WorkerResult("glm", "boost", "spare"), now, "ja") is None


def test_check_message_without_remaining_uses_status():
    now = datetime.now().astimezone()
    i18n.set_language("ja")
    stop = WorkerResult("glm", "stop", "exhausted", remaining=None, status="exhausted")
    assert "上限" not in gate.check_message(stop, now, "ja")
    assert "exhausted" in gate.check_message(stop, now, "ja")


# --- load_status / burn ファイル ---------------------------------------------------


def test_load_status_states(tmp_path):
    path = tmp_path / "status.json"
    st, data, detail = gate.load_status(path, 20, NOW)
    assert (st, data, detail) == ("stale", None, "missing")
    path.write_text("{ broken", encoding="utf-8")
    assert gate.load_status(path, 20, NOW)[2] == "broken"
    path.write_text('"array"', encoding="utf-8")
    assert gate.load_status(path, 20, NOW)[2] == "broken"
    fresh = status(win("glm:5h", "glm", "spare", 50.0))
    path.write_text(json.dumps(fresh), encoding="utf-8")
    st, data, detail = gate.load_status(path, 20, NOW)
    assert (st, detail) == ("ok", "")
    # updated_at が古い → old (paused は stale 扱いにしない)
    old = status(win("glm:5h", "glm", "spare", 50.0), updated=NOW - timedelta(minutes=25))
    path.write_text(json.dumps({**old, "paused": True}), encoding="utf-8")
    assert gate.load_status(path, 20, NOW)[2] == "old"
    # updated_at が無い → 検証できないので stale
    path.write_text(json.dumps({"cards": []}), encoding="utf-8")
    assert gate.load_status(path, 20, NOW)[2] == "broken"


def test_burn_store_clear_and_purge(tmp_path):
    path = tmp_path / "routing-burn.json"
    gate.store_burn(path, "glm", NOW + timedelta(hours=1), NOW)
    gate.store_burn(path, "grok", NOW - timedelta(hours=1), NOW)  # 期限切れは掃除される
    active = gate.load_burn(path, NOW)
    assert set(active) == {"glm"}
    gate.store_burn(path, "codex", NOW + timedelta(days=1), NOW)
    assert set(gate.load_burn(path, NOW)) == {"glm", "codex"}
    assert not path.with_name(path.name + ".tmp").exists()  # 原子的な書き込み
    gate.clear_burn(path, "glm", NOW)
    assert set(gate.load_burn(path, NOW)) == {"codex"}
    gate.clear_burn(path, "glm", NOW)  # 無い名前でもエラーにならない
    # 壊れたファイルは無視
    path.write_text("{ broken", encoding="utf-8")
    assert gate.load_burn(path, NOW) == {}


def test_parse_until():
    base = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    until = gate.parse_until("23:00", base)
    assert until is not None and (until.hour, until.minute) == (23, 0)
    until = gate.parse_until("09:00", base)  # 過ぎているので翌日
    assert until is not None and until > base + timedelta(hours=12)
    assert gate.parse_until("2026-10-27T09:00:00+00:00", base) \
        == datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    assert gate.parse_until("25:00", base) is None
    assert gate.parse_until("nope", base) is None
    assert gate.parse_until("", base) is None


# --- [routing] 設定 ---------------------------------------------------------------


def test_routing_config_defaults(tmp_path):
    cfg_ = load_config(tmp_path / "none.toml")
    assert cfg_.routing.enabled is False
    assert cfg_.routing.floor_percent == pytest.approx(5.0)
    assert cfg_.routing.floor_hours == 24
    assert cfg_.routing.stale_minutes == 20
    assert cfg_.routing.short_over_margin_minutes is None
    assert cfg_.routing.workers == {}


def test_routing_config_load_and_roundtrip(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[routing]\nenabled = true\nfloor_percent = 8.5\nfloor_hours = 12.5\nstale_minutes = 15\nshort_over_margin_minutes = 12.5\n'
        '[routing.workers]\nglm = ["glm:5h", "glm:weekly"]\n'
        'argon = ["gemini:argon"]\ngrok = ["grok:*"]\n',
        encoding="utf-8")
    cfg_ = load_config(p)
    assert cfg_.routing.enabled is True
    assert cfg_.routing.floor_percent == pytest.approx(8.5)
    assert cfg_.routing.floor_hours == 12.5
    assert cfg_.routing.stale_minutes == 15
    assert cfg_.routing.short_over_margin_minutes == 12.5
    assert cfg_.routing.workers == {"glm": ["glm:5h", "glm:weekly"],
                                    "argon": ["gemini:argon"], "grok": ["grok:*"]}
    parsed = format_toml(cfg_)
    assert "[routing.workers]" in parsed
    assert '"glm" = ' in parsed
    assert config_to_dict(load_config_from_text(parsed))["routing"] == config_to_dict(cfg_)["routing"]
    d = config_to_dict(cfg_)
    assert config_to_dict(config_from_dict(d)) == d
    assert d["routing"] == {"enabled": True, "floor_percent": 8.5, "floor_hours": 12.5, "stale_minutes": 15,
                            "short_over_margin_minutes": 12.5,
                            "workers": {"glm": ["glm:5h", "glm:weekly"],
                                        "argon": ["gemini:argon"], "grok": ["grok:*"]},
                            # config --json は表示順も worker_order で返す (アプリ用)
                            "worker_order": ["glm", "argon", "grok"]}


def test_floor_hours_zero_roundtrip():
    settings = config_from_dict({"routing": {"floor_hours": 0}})
    assert config_to_dict(settings)["routing"]["floor_hours"] == 0
    assert load_config_from_text(format_toml(settings)).routing.floor_hours == 0


def load_config_from_text(text):
    import os
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        return load_config(Path(name))
    finally:
        os.unlink(name)


@pytest.mark.parametrize(
    "text",
    [
        '[routing]\nenabled = "yes"',
        "[routing]\nfloor_percent = -1",
        "[routing]\nfloor_percent = 101",
        '[routing]\nfloor_hours = -1',
        '[routing]\nfloor_hours = "24"',
        '[routing]\nfloor_hours = true',
        '[routing]\nfloor_hours = nan',
        '[routing]\nfloor_hours = inf',
        '[routing]\nstale_minutes = 0',
        '[routing]\nstale_minutes = "soon"',
        '[routing]\nshort_over_margin_minutes = 0',
        '[routing]\nshort_over_margin_minutes = -1',
        '[routing]\nshort_over_margin_minutes = "30"',
        '[routing]\nshort_over_margin_minutes = true',
        '[routing]\nshort_over_margin_minutes = nan',
        '[routing]\nshort_over_margin_minutes = inf',
        "[routing]\nworkers = 3",                             # テーブルでない
        '[routing.workers]\nglm = "glm:5h"',                  # 配列でない
        '[routing.workers]\nglm = []',                        # 空: どの窓も使わない
        '[routing.workers]\nglm = [5]',
        '[routing.workers]\n"" = ["glm:5h"]',                 # 名前が空
    ],
)
def test_routing_config_invalid_raises(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_routing_config_unknown_subkey_ignored(tmp_path, caplog):
    import logging

    p = tmp_path / "c.toml"
    p.write_text('[routing]\nmode = "aggressive"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="quotacanvas"):
        cfg_ = load_config(p)
    assert cfg_.routing.enabled is False
    assert any("routing.mode" in r.getMessage() for r in caplog.records)


# --- status.json の windows / routing 書き出し -------------------------------------


def _engine_cfg(tmp_path, text=""):
    from quotacanvas.config import load_config as lc

    p = tmp_path / "engine.toml"
    p.write_text(text, encoding="utf-8")
    return lc(p)


def _engine_cards():
    from quotacanvas.models import ProviderUsage, Window
    from quotacanvas.pacing import compute_pace

    w1 = Window("glm", "5h", "5時間", 51.0, NOW + timedelta(hours=2), timedelta(hours=5))
    w2 = Window("grok", "weekly", "週間", 30.0, NOW + timedelta(days=3), timedelta(days=7))
    w3 = Window("grok", "weekly_build", "週間 Build", 20.0, NOW + timedelta(days=3),
                timedelta(days=7), part_of="weekly")
    u1 = ProviderUsage("glm", "GLM", [w1], None, NOW)
    u2 = ProviderUsage("grok", "Grok", [w2, w3], None, NOW)
    return ([(u1, compute_pace(w1, [], NOW)), (u2, compute_pace(w2, [], NOW)),
             (u2, compute_pace(w3, [], NOW))], [u1, u2])


def test_build_status_writes_windows_unfiltered_and_always(tmp_path):
    from quotacanvas.status import build_status

    cards, usages = _engine_cards()
    cfg_ = _engine_cfg(tmp_path)  # routing.enabled は false
    payload = build_status(cfg_, cards, usages, NOW)
    ids = [w["id"] for w in payload["windows"]]
    assert ids == ["glm:5h", "grok:weekly", "grok:weekly_build"]  # 内訳も含む全窓
    item = payload["windows"][0]
    assert item["provider"] == "glm" and item["key"] == "5h" and item["part_of"] is None
    assert item["remaining"] == 49.0 and item["status"] == "on_track"
    assert item["length"] == 5 * 3600
    assert item["fetched_at"] == NOW.astimezone().isoformat(timespec="seconds")
    assert "T" in item["resets_at"]  # ISO 8601
    breakdown = payload["windows"][2]
    assert breakdown["part_of"] == "weekly"
    # enabled=false でも windows は書く。routing は書かない
    assert "routing" not in payload


def test_build_status_writes_routing_when_enabled(tmp_path):
    from quotacanvas.status import build_status

    cards, usages = _engine_cards()
    cfg_ = _engine_cfg(tmp_path, '[routing]\nenabled = true\n[routing.workers]\nglm = ["glm:5h"]\n')
    payload = build_status(cfg_, cards, usages, NOW, burn={"glm": NOW + timedelta(hours=1)})
    routing = payload["routing"]
    assert [w["name"] for w in routing["workers"]] == ["glm"]
    worker = routing["workers"][0]
    assert worker["verdict"] == "boost" and worker["reason"] == "on_track"
    assert worker["burn_until"] is not None
    assert routing["at"] == payload["updated_at"]
    assert routing["line"].startswith("quota: glm=boost(")  # routing line と同じ 1 行


def test_build_status_windows_honors_all_windows_over_cards(tmp_path):
    # all_windows を渡すと表示フィルタ後の cards に無い窓も windows に入る
    from quotacanvas.status import build_status

    cards, usages = _engine_cards()
    shown = cards[:1]  # glm:5h だけ表示した体
    cfg_ = _engine_cfg(tmp_path)
    payload = build_status(cfg_, shown, usages, NOW, all_windows=cards)
    assert [w["id"] for w in payload["windows"]] == ["glm:5h", "grok:weekly", "grok:weekly_build"]
    assert [c["id"] for c in payload["cards"]] == ["glm:5h"]  # cards は従来どおり


def test_routing_result_dict_shape():
    result = RoutingResult(workers=[WorkerResult("glm", "go", "on_track", windows=["glm:5h"],
                                     remaining=50.0, status="on_track")],
                           at=NOW.isoformat(timespec="seconds"), reset_credits={"codex": 2})
    d = result.to_dict()
    assert d["reset_credits"] == {"codex": 2}
    assert d["workers"][0]["name"] == "glm"
    assert d["workers"][0]["verdict"] == "go"
    assert set(d["workers"][0]) == {"name", "verdict", "reason", "windows", "remaining",
                                    "status", "resets_at", "exhausts_at", "burn_until",
                                    "stale", "provider"}


def test_format_line_empty_workers_returns_empty_string():
    result = gate.RoutingResult(workers=[], at=NOW.isoformat(timespec="seconds"))
    assert gate.format_line(result, NOW, "ja") == ""


def short_window(gap_minutes, *, length_hours=5, pace="over", wid="glm:5h", provider="glm", reset=None):
    reset = reset or NOW + timedelta(hours=4)
    entry = win(wid, provider, pace, 50, resets=reset,
                exhausts=reset - timedelta(minutes=gap_minutes))
    entry["length"] = length_hours * 3600
    return entry


@pytest.mark.parametrize("gap,expected", [(7, ("go", "over_near_reset")),
                                           (30, ("reserve", "over"))])
def test_short_over_boundaries(gap, expected):
    worker = evaluate(status(short_window(gap)), cfg(), {}, NOW).workers[0]
    assert (worker.verdict, worker.reason) == expected
    if gap == 7:
        line = format_line(evaluate(status(short_window(gap)), cfg(), {}, NOW), NOW, "ja")
        assert "glm=go(" in line and "ペース超過" in line and "枯渇見込み" in line


@pytest.mark.parametrize("length,gap,margin,expected", [
    (1, 5.9, None, "go"), (1, 6, None, "reserve"),
    (5, 9, 10, "go"), (5, 10, 10, "reserve"),
])
def test_short_margin(length, gap, margin, expected):
    data = status(short_window(gap, length_hours=length))
    assert evaluate(data, cfg(short_over_margin_minutes=margin), {}, NOW).workers[0].verdict == expected


@pytest.mark.parametrize("broken", ["length", "exhausts_at", "bad_exhausts", "bad_reset"])
def test_short_over_missing_data_is_reserve(broken):
    entry = short_window(7)
    if broken == "bad_exhausts":
        entry["exhausts_at"] = "broken"
    elif broken == "bad_reset":
        entry["resets_at"] = "broken"
    else:
        del entry[broken]
    assert evaluate(status(entry), cfg(), {}, NOW).workers[0].verdict == "reserve"


def test_long_window_preserves_rules():
    entry = short_window(2 * 24 * 60, length_hours=7 * 24)
    assert evaluate(status(entry), cfg(), {}, NOW).workers[0].verdict == "reserve"
    entry["status"] = "on_track"
    assert evaluate(status(entry), cfg(), {}, NOW).workers[0].verdict == "go"
    entry["length"] = 24 * 3600
    assert evaluate(status(entry), cfg(), {}, NOW).workers[0].verdict == "go"
    entry["length"] -= 1
    assert evaluate(status(entry), cfg(), {}, NOW).workers[0].verdict == "boost"


def test_short_over_hysteresis_and_idempotence():
    first = evaluate(status(short_window(30)), cfg(), {}, NOW)
    assert first.holds == {"glm:5h": short_window(30)["resets_at"]}
    for gap, expected in [(25, "reserve"), (20, "reserve"), (19, "go")]:
        data = status(short_window(gap))
        result = evaluate(data, cfg(), {}, NOW, previous=first.to_dict())
        assert result.workers[0].verdict == expected
        assert evaluate(data, cfg(), {}, NOW, previous=result.to_dict()).to_dict() == result.to_dict()
    data = status(short_window(25))
    assert evaluate(data, cfg(), {}, NOW).workers[0].verdict == "go"
    different = {"holds": {"glm:5h": (NOW + timedelta(days=1)).isoformat()}}
    assert evaluate(data, cfg(), {}, NOW, previous=different).workers[0].verdict == "go"
    entry = short_window(25, pace="tight")
    assert evaluate(status(entry), cfg(), {}, NOW, previous=first.to_dict()).holds == {}
    entry["status"] = "over"
    entry["remaining"] = 1
    assert evaluate(status(entry), cfg(floor_hours=0), {}, NOW, previous=first.to_dict()).holds == {}


@pytest.mark.parametrize("weekly,short,expected", [("over", "over", "reserve"),
    ("on_track", "exhausted", "stop"), ("spare", "on_track", "boost")])
def test_mixed_windows(weekly, short, expected):
    week = short_window(2 * 24 * 60, length_hours=168, pace=weekly,
                        wid="gemini:weekly", provider="gemini")
    five = short_window(7, pace=short, wid="gemini:5h", provider="gemini")
    result = evaluate(status(week, five), cfg(), {}, NOW)
    assert result.workers[0].verdict == expected


def test_holds_track_each_window_even_when_not_representative_or_burned():
    five = short_window(30, wid="gemini:5h", provider="gemini")
    week = short_window(0, length_hours=168, pace="exhausted", wid="gemini:week", provider="gemini")
    result = evaluate(status(five, week), cfg(), {"gemini": NOW + timedelta(hours=1)}, NOW)
    assert result.workers[0].verdict == "stop"
    assert result.holds == {"gemini:5h": five["resets_at"]}
    five = short_window(25, wid="gemini:5h", provider="gemini")
    result = evaluate(status(five), cfg(), {"gemini": NOW + timedelta(hours=1)}, NOW,
                      previous=result.to_dict())
    assert result.workers[0].verdict == "boost"
    assert result.holds == {"gemini:5h": five["resets_at"]}


def test_engine_writes_and_reuses_previous_routing(tmp_path, monkeypatch):
    from dataclasses import replace
    from quotacanvas import cli
    from quotacanvas.models import PaceStatus
    from quotacanvas.status import write_status

    path = tmp_path / "status.json"
    monkeypatch.setattr(cli, "STATUS_PATH", path)
    monkeypatch.setattr(cli, "write_status", lambda payload: write_status(payload, path))
    monkeypatch.setattr(gate, "BURN_PATH", tmp_path / "burn.json")
    monkeypatch.setattr(gate, "OFF_PATH", tmp_path / "off.json")
    cards, usages = _engine_cards()
    pu, pace = cards[0]
    reset = pace.window.resets_at
    pace = replace(pace, status=PaceStatus.OVER, exhausts_at=reset - timedelta(minutes=30))
    settings = _engine_cfg(tmp_path, '[routing]\nenabled = true\n')
    cli._write_app_files(settings, [(pu, pace)], usages, NOW, None)
    first = json.loads(path.read_text())
    assert first["routing"]["holds"] == {"glm:5h": first["windows"][0]["resets_at"]}
    stale_pu = replace(pu, stale_error="temporary failure")
    cli._write_app_files(settings, [(stale_pu, pace)], [stale_pu], NOW, None)
    interrupted = json.loads(path.read_text())
    assert interrupted["routing"]["workers"][0]["verdict"] == "unknown"
    assert interrupted["routing"]["holds"] == first["routing"]["holds"]
    pace = replace(pace, exhausts_at=reset - timedelta(minutes=25))
    cli._write_app_files(settings, [(pu, pace)], usages, NOW, None)
    second = json.loads(path.read_text())
    assert second["routing"]["workers"][0]["verdict"] == "reserve"
    assert second["routing"]["holds"] == first["routing"]["holds"]


@pytest.mark.parametrize("interruption", ["stale", "other_stale", "error", "missing"])
def test_hysteresis_survives_unjudged_windows(interruption):
    settings = cfg(workers={"glm": ["glm:5h"]})
    first = evaluate(status(short_window(30)), settings, {}, NOW)
    entry = short_window(25)
    if interruption == "stale":
        entry["stale_error"] = "temporary failure"
        data = status(entry)
    elif interruption == "other_stale":
        other = win("glm:other", "glm", "spare", 50, stale_error="temporary failure")
        data = status(entry, other)
    elif interruption == "error":
        data = status(entry, errors=[{"provider": "glm"}])
    else:
        data = status()
    interrupted = evaluate(data, settings, {}, NOW, previous=first.to_dict())
    assert interrupted.workers[0].verdict == "unknown"
    assert interrupted.holds == first.holds
    assert evaluate(data, settings, {}, NOW, previous=interrupted.to_dict()).to_dict() == interrupted.to_dict()
    recovered = evaluate(status(short_window(25)), settings, {}, NOW, previous=interrupted.to_dict())
    assert (recovered.workers[0].verdict, recovered.workers[0].reason) == ("reserve", "over")
    assert recovered.holds == first.holds


def test_unjudged_expired_holds_are_discarded():
    previous = {"holds": {
        "glm:past": (NOW - timedelta(seconds=1)).isoformat(),
        "glm:now": NOW.isoformat(),
        "glm:future": (NOW + timedelta(seconds=1)).isoformat(),
        "glm:broken": "bad timestamp",
    }}
    result = evaluate(status(), cfg(workers={"glm": ["glm:*"]}), {}, NOW, previous=previous)
    assert result.holds == {"glm:future": previous["holds"]["glm:future"]}


def test_expired_hold_does_not_affect_judgment_or_idempotence():
    entry = short_window(25)
    reset = NOW - timedelta(seconds=1)
    entry["resets_at"] = reset.isoformat()
    entry["exhausts_at"] = (reset - timedelta(minutes=25)).isoformat()
    data = status(entry)
    previous = {"holds": {"glm:5h": entry["resets_at"]}}
    result = evaluate(data, cfg(), {}, NOW, previous=previous)
    assert result.workers[0].verdict == "go"
    assert result.holds == {}
    assert evaluate(data, cfg(), {}, NOW, previous=result.to_dict()).to_dict() == result.to_dict()


def test_judged_window_clears_hold_after_interruption():
    settings = cfg(workers={"glm": ["glm:5h"]})
    first = evaluate(status(short_window(30)), settings, {}, NOW)
    interrupted = evaluate(status(errors=[{"provider": "glm"}]), settings, {}, NOW, previous=first.to_dict())
    result = evaluate(status(short_window(25, pace="tight")), settings, {}, NOW,
                      previous=interrupted.to_dict())
    assert result.workers[0].verdict == "go"
    assert result.holds == {}
    recovered = evaluate(status(short_window(25)), settings, {}, NOW, previous=result.to_dict())
    assert recovered.workers[0].reason == "over_near_reset"


def test_hold_is_replaced_for_judged_window_while_another_remains_unjudged():
    first_glm = short_window(30)
    first_gemini = short_window(30, wid="gemini:5h", provider="gemini")
    settings = cfg(workers={"glm": ["glm:5h"], "gemini": ["gemini:5h"]})
    first = evaluate(status(first_glm, first_gemini), settings, {}, NOW)
    second = evaluate(status(short_window(25, pace="tight"), first_gemini,
                             errors=[{"provider": "gemini"}]), settings, {}, NOW,
                      previous=first.to_dict())
    assert second.holds == {"gemini:5h": first_gemini["resets_at"]}
    assert second.worker("glm").verdict == "go"
    assert second.worker("gemini").verdict == "unknown"


def test_reserve_without_reset_has_no_dangling_separator():
    worker = WorkerResult("glm", "reserve", "over", remaining=85.0)
    assert "残り85%）" in gate.check_message(worker, NOW, "ja")
    assert "remaining 85%)" in gate.check_message(worker, NOW, "en")


def test_english_credit_singular_and_plural():
    worker = WorkerResult("codex", "boost", "spare", remaining=50, provider="codex")
    result = RoutingResult(workers=[worker], reset_credits={"codex": 1})
    assert "1 reset credit)" in format_line(result, NOW, "en")
    result.reset_credits["codex"] = 2
    assert "2 reset credits)" in format_line(result, NOW, "en")


def test_parse_until_rejects_past_and_equal_iso():
    assert gate.parse_until(NOW.isoformat(), NOW) is None
    assert gate.parse_until((NOW - timedelta(seconds=1)).isoformat(), NOW) is None


@pytest.mark.parametrize("case", ["boost", "burn", "stale", "error", "missing"])
def test_monitor_only_overrides_verdict_and_keeps_display_data(case):
    window = win("grok:weekly", "grok", "spare" if case == "boost" else "over", 63,
                 fetched=NOW - timedelta(hours=2) if case == "stale" else NOW)
    data = status(*([] if case == "missing" else [window]),
                  errors=[{"provider": "grok"}] if case == "error" else None)
    result = evaluate(data, cfg(workers={"grok": ["grok:weekly"]}),
                      {"grok": NOW + timedelta(hours=1)}, NOW, off={"grok"})
    worker = result.worker("grok")
    assert (worker.verdict, worker.reason, worker.burn_until) == ("stop", "disabled", None)
    if case != "missing":
        assert worker.remaining == 63 and worker.resets_at == window["resets_at"]
    assert "grok=stop(" in format_line(result, NOW, "ja")
    assert "監視のみ" in format_line(result, NOW, "ja")
    assert "monitor only" in format_line(result, NOW, "en")
    assert "監視のみ" in gate.check_message(worker, NOW, "ja")
    assert "monitor only" in gate.check_message(worker, NOW, "en")


def test_off_store_clear(tmp_path):
    path = tmp_path / "off.json"
    gate.store_off(path, "grok")
    gate.store_off(path, "codex")
    assert json.loads(path.read_text()) == ["codex", "grok"]
    gate.clear_off(path, "grok")
    assert gate.load_off(path) == {"codex"}


def test_engine_reads_monitor_only_and_records_it(tmp_path, monkeypatch):
    from quotacanvas import cli
    from quotacanvas.status import write_status
    path = tmp_path / "status.json"
    monkeypatch.setattr(cli, "STATUS_PATH", path)
    monkeypatch.setattr(cli, "write_status", lambda payload: write_status(payload, path))
    monkeypatch.setattr(gate, "BURN_PATH", tmp_path / "burn.json")
    monkeypatch.setattr(gate, "OFF_PATH", tmp_path / "off.json")
    gate.store_off(gate.OFF_PATH, "grok")
    cards, usages = _engine_cards()
    settings = _engine_cfg(tmp_path, '[routing]\nenabled = true\n')
    cli._write_app_files(settings, cards, usages, NOW, None)
    result = json.loads(path.read_text())
    worker = next(w for w in result["routing"]["workers"] if w["name"] == "grok")
    assert (worker["verdict"], worker["reason"], worker["remaining"]) == ("stop", "disabled", 70.0)
    assert any(w["id"] == "grok:weekly" for w in result["windows"])


@pytest.mark.parametrize("name", ["openrouter", "deepseek", "kimi"])
def test_off_does_not_include_balance_only_workers(name):
    data = status(win(name + ":daily", name, "spare", 63))
    result = evaluate(data, cfg(workers={name: [name + ":daily"],
                                         "balance_alias": [name + ":daily"]}),
                      {}, NOW, off={name, "balance_alias"})
    assert not result.workers


def test_burn_concurrent_updates_preserve_each_worker(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import time

    path = tmp_path / "burn.json"
    original_load = gate.load_burn
    start = Barrier(8)

    def slow_load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        time.sleep(0.01)
        return result

    monkeypatch.setattr(gate, "load_burn", slow_load)

    def update(i):
        start.wait(timeout=5)
        gate.store_burn(path, f"worker{i}", NOW + timedelta(hours=1), NOW)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(8)))
    assert set(original_load(path, NOW)) == {f"worker{i}" for i in range(8)}


@pytest.mark.parametrize("patterns", [
    ["codex:weekly", "codex:5h"],
    ["codex:weekly", "codex:short*"],
])
def test_partial_window_match_is_missing_even_with_burn(patterns):
    routing = cfg(workers={"codex": patterns})
    data = status(win("codex:weekly", "codex", "spare", 80))
    result = evaluate(data, routing, {"codex": NOW + timedelta(hours=1)}, NOW)
    assert (result.workers[0].verdict, result.workers[0].reason) == ("unknown", "missing")
