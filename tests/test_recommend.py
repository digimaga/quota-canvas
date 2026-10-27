"""Tests for recommend.py: 今使う先 (F1) の score・使える判定・文言。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quotacanvas import i18n
from quotacanvas.history import History
from quotacanvas.models import ProviderUsage, ResetCredit, Window
from quotacanvas.recommend import recommend

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)


def _win(provider, key, used, resets_in, length, label=None, part_of=None):
    lab = label or key
    resets = NOW + resets_in if resets_in is not None else None
    return Window(provider, key, lab, used, resets, length, part_of)


def _usage(provider, name, windows, error=None, credits=(), stale_error=None):
    usage = ProviderUsage(provider, name, list(windows), error, NOW,
                          list(credits))
    usage.stale_error = stale_error
    return usage


def _rec(usages, tmp_path):
    return recommend(usages, History(tmp_path / "history.jsonl"), NOW)


# ------------------------------------------------------------------- score

def test_picks_service_that_wastes_the_most(tmp_path):
    # glm 5h: UNKNOWN (経過 0) -> waste = remaining 98%, 長さ 5時間 / あと 5時間
    #   -> score = 98 * 5 / 5 = 98
    # claude 週間: 使用 30%, 経過 3日, 残り 4日 -> 予測 70% -> waste 30
    #   -> score = 30 * 168 / 96 = 52.5
    glm = _usage("glm", "GLM", [_win("glm", "5h", 2.0, timedelta(hours=5), timedelta(hours=5), "5時間")])
    claude = _usage("claude", "Claude Max",
                    [_win("claude", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間")])
    rec = _rec([claude, glm], tmp_path)
    assert rec is not None
    assert rec.provider == "glm"
    assert rec.window_id == "glm:5h"
    assert rec.text == "GLM を使う · 5時間の残り 98% · リセットまで 5時間"
    assert [e["provider"] for e in rec.ranking] == ["glm", "claude"]
    assert rec.ranking[0]["score"] == pytest.approx(98.0)
    assert rec.ranking[1]["score"] == pytest.approx(52.5)
    assert rec.ranking[0]["usable"] is True
    assert rec.ranking[1]["reason"] == "週間の残り 70% · リセットまで 4日"


def test_longer_window_outranks_short_one(tmp_path):
    # 実データでの後退修正: score は窓の長さで重み付けするので、
    # 「残り 60%・あと 2.5時間」の 5時間枠より「残り 72%・あと 20時間」の
    # 週間が上になる (長さで重み付けしないと 5 時間枠が常に勝っていた)
    glm = _usage("glm", "GLM", [_win("glm", "5h", 20.0, timedelta(hours=5) - timedelta(hours=2, minutes=30),
                                     timedelta(hours=5), "5時間")])
    grok = _usage("grok", "Grok", [_win("grok", "weekly", 24.0, timedelta(hours=20),
                                        timedelta(days=7), "週間")])
    rec = _rec([glm, grok], tmp_path)
    assert rec is not None
    assert rec.provider == "grok"
    scores = {e["provider"]: e["score"] for e in rec.ranking}
    assert scores["grok"] > scores["glm"]


def test_all_gone_when_nothing_usable(tmp_path):
    claude = _usage("claude", "Claude Max",
                    [_win("claude", "weekly", 80.0, timedelta(days=4), timedelta(days=7), "週間")])
    assert _rec([claude], tmp_path) is None  # 予測がリセット前に 100% (OVER)


def test_unusable_when_any_window_is_over(tmp_path):
    # 週間は余裕でも、同じサービスの 5h が OVER ならそのサービスは使えない
    # (5h: 使用 80%, 経過 3時間, 残り 2時間 -> 45分で 100% に届く)
    claude = _usage("claude", "Claude Max", [
        _win("claude", "weekly", 10.0, timedelta(days=6), timedelta(days=7), "週間"),
        _win("claude", "5h", 80.0, timedelta(hours=2), timedelta(hours=5), "5時間"),
    ])
    assert _rec([claude], tmp_path) is None


def test_unusable_when_remaining_below_5_percent(tmp_path):
    # 経過 0 で UNKNOWN (OVER ではない) でも、残り 4% (< 5%) なら使えない
    glm = _usage("glm", "GLM", [_win("glm", "5h", 96.0, timedelta(hours=5), timedelta(hours=5), "5時間")])
    assert _rec([glm], tmp_path) is None


def test_error_service_is_skipped(tmp_path):
    bad = _usage("glm", "GLM", [], error="HTTP 401")
    assert _rec([bad], tmp_path) is None


def test_past_reset_window_makes_service_not_usable(tmp_path):
    # resets_at が過ぎている窓: MIN_HOURS の床で score が跳ねるので「リセット待ち」
    # として推奨対象から外す (ほかに使えるサービスがあればそちらが勝つ)
    ok = _usage("claude", "Claude Max",
                [_win("claude", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間")])
    waiting = _usage("glm", "GLM",
                     [_win("glm", "5h", 30.0, timedelta(hours=-1), timedelta(hours=5), "5時間")])
    rec = _rec([waiting, ok], tmp_path)
    assert rec is not None
    assert rec.provider == "claude"
    entry = next(e for e in rec.ranking if e["provider"] == "glm")
    assert entry["usable"] is False
    assert entry["reason"] == "リセット待ち"


def test_all_windows_past_reset_recommends_nothing(tmp_path):
    waiting = _usage("glm", "GLM",
                     [_win("glm", "5h", 30.0, timedelta(hours=-1), timedelta(hours=5), "5時間")])
    assert _rec([waiting], tmp_path) is None


def test_stale_error_service_is_not_usable(tmp_path):
    # 取得失敗中 (last-good を表示中): データが古いので推奨しない
    ok = _usage("claude", "Claude Max",
                [_win("claude", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間")])
    failing = _usage("glm", "GLM",
                     [_win("glm", "5h", 2.0, timedelta(hours=5), timedelta(hours=5), "5時間")],
                     stale_error="HTTP 429")
    rec = _rec([failing, ok], tmp_path)
    assert rec is not None
    assert rec.provider == "claude"  # score は glm のほうが高いが使えるものだけを推奨
    entry = next(e for e in rec.ranking if e["provider"] == "glm")
    assert entry["usable"] is False
    assert entry["reason"] == "取得失敗中"


def test_stale_error_service_alone_recommends_nothing(tmp_path):
    failing = _usage("glm", "GLM",
                     [_win("glm", "5h", 2.0, timedelta(hours=5), timedelta(hours=5), "5時間")],
                     stale_error="HTTP 429")
    assert _rec([failing], tmp_path) is None


def test_service_without_independent_windows_is_skipped(tmp_path):
    # 内訳 (part_of 付き) だけのサービスは候補にならない
    claude = _usage("claude", "Claude Max", [
        _win("claude", "weekly_build", 90.0, timedelta(days=6), timedelta(days=7),
             "週間 Build", part_of="weekly"),
    ])
    assert _rec([claude], tmp_path) is None


def test_part_of_window_does_not_break_usability(tmp_path):
    claude = _usage("claude", "Claude Max", [
        _win("claude", "weekly", 10.0, timedelta(days=6), timedelta(days=7), "週間"),
        _win("claude", "weekly_build", 95.0, timedelta(days=6), timedelta(days=7),
             "週間 Build", part_of="weekly"),
    ])
    rec = _rec([claude], tmp_path)
    assert rec is not None
    assert rec.provider == "claude"


# --------------------------------------------------------------- score 調整

def test_short_window_below_20_percent_halves_score(tmp_path):
    # claude: 週間 score = 30 * 168 / 96 = 52.5 だが、5h (経過 0, 残り 15%) が
    # あるので半分。codex: 同じ週間だけ -> 52.5 のままで勝つ
    claude = _usage("claude", "Claude Max", [
        _win("claude", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間"),
        _win("claude", "5h", 85.0, timedelta(hours=5), timedelta(hours=5), "5時間"),
    ])
    codex = _usage("codex", "Codex",
                   [_win("codex", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間")])
    rec = _rec([claude, codex], tmp_path)
    assert rec.provider == "codex"
    scores = {e["provider"]: e["score"] for e in rec.ranking}
    assert scores["claude"] == pytest.approx(26.25)
    assert scores["codex"] == pytest.approx(52.5)


def test_short_window_with_room_does_not_halve(tmp_path):
    # 短い窓の残りが 20% 以上なら半分にしない (score は週間だけで決まる)
    claude = _usage("claude", "Claude Max", [
        _win("claude", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間"),
        _win("claude", "5h", 10.0, timedelta(hours=5), timedelta(hours=5), "5時間"),
    ])
    rec = _rec([claude], tmp_path)
    assert rec.ranking[0]["score"] == pytest.approx(52.5)


def test_soon_reset_credit_boosts_score_and_reason(tmp_path):
    credit = ResetCredit("Full reset", NOW - timedelta(days=1), NOW + timedelta(hours=18))
    claude = _usage("claude", "Claude Max",
                    [_win("claude", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間")])
    codex = _usage("codex", "Codex",
                   [_win("codex", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間")],
                   credits=[credit])
    rec = _rec([claude, codex], tmp_path)
    assert rec.provider == "codex"
    assert rec.ranking[0]["score"] == pytest.approx(52.5 * 1.5)
    assert rec.text == ("Codex を使う · 週間の残り 70% · リセットまで 4日"
                        " · リセット権が 18時間で失効")


def test_credit_beyond_24h_does_not_boost(tmp_path):
    credit = ResetCredit("Full reset", NOW - timedelta(days=1), NOW + timedelta(days=3))
    codex = _usage("codex", "Codex",
                   [_win("codex", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間")],
                   credits=[credit])
    rec = _rec([codex], tmp_path)
    assert rec.ranking[0]["score"] == pytest.approx(52.5)
    assert "リセット権" not in rec.text


def test_unusable_service_is_never_recommended_even_with_high_score(tmp_path):
    # OVER でも score は計算するが、usable が最優先 (並びも usable が先)
    over = _usage("claude", "Claude Max",
                  [_win("claude", "weekly", 80.0, timedelta(days=4), timedelta(days=7), "週間")])
    ok = _usage("glm", "GLM", [_win("glm", "5h", 2.0, timedelta(hours=5), timedelta(hours=5), "5時間")])
    rec = _rec([over, ok], tmp_path)
    assert rec.provider == "glm"
    assert [e["provider"] for e in rec.ranking] == ["glm", "claude"]
    assert rec.ranking[1]["usable"] is False


# ------------------------------------------------------------------- その他

def test_no_usages_gives_none(tmp_path):
    assert _rec([], tmp_path) is None


def test_window_without_resets_at_uses_length_as_span(tmp_path):
    claude = _usage("claude", "Claude Max",
                    [_win("claude", "5h", 30.0, None, timedelta(hours=5), "5時間")])
    rec = _rec([claude], tmp_path)
    assert rec is not None
    assert rec.ranking[0]["resets_at"] is None
    assert rec.text == "Claude Max を使う · 5時間の残り 70%"  # リセットまで...は出ない


def test_payload_shape(tmp_path):
    glm = _usage("glm", "GLM", [_win("glm", "5h", 2.0, timedelta(hours=5), timedelta(hours=5), "5時間")])
    rec = _rec([glm], tmp_path)
    payload = rec.payload()
    assert payload["provider"] == "glm"
    assert payload["name"] == "GLM"
    assert payload["window_id"] == "glm:5h"
    assert payload["text"] == rec.text
    assert payload["ranking"][0]["remaining"] == 98.0
    assert payload["ranking"][0]["resets_at"] == (NOW + timedelta(hours=5)).astimezone().isoformat(timespec="seconds")


def test_english_wording(tmp_path):
    i18n.set_language("en")
    glm = _usage("glm", "GLM", [_win("glm", "5h", 2.0, timedelta(hours=5), timedelta(hours=5), "5時間")])
    rec = _rec([glm], tmp_path)
    assert rec.text == "Use GLM · 98% left on 5時間 · resets in 5h"
    i18n.set_language("ja")


def test_short_window_past_reset_keeps_service_usable(tmp_path):
    # 5時間枠だけがリセット時刻を過ぎて次の取得待ち: 代表窓 (週間) の score は
    # 正しいので、サービスごと外さない
    claude = _usage("claude", "Claude Max", [
        _win("claude", "weekly", 30.0, timedelta(days=4), timedelta(days=7), "週間"),
        _win("claude", "5h", 30.0, timedelta(minutes=-2), timedelta(hours=5), "5時間"),
    ])
    rec = _rec([claude], tmp_path)
    assert rec is not None
    assert rec.provider == "claude"
    assert rec.ranking[0]["usable"] is True
