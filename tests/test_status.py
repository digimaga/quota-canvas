"""Tests for status.py: status.json の形 (docs/app-contract.md) と原子的な書き込み。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from PIL import Image

from quotacanvas.config import load_config
from quotacanvas.models import PaceStatus, ProviderUsage, ResetCredit, Window
from quotacanvas.pacing import compute_pace
from quotacanvas.render import advice_text
from quotacanvas.recommend import Recommendation
from quotacanvas.status import build_status, write_frame, write_status

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path, text=""):
    p = tmp_path / "config.toml"
    if text:
        p.write_text(text, encoding="utf-8")
    return load_config(p)


def _win(provider, key, used, label="週間", length=timedelta(days=7), resets_in=timedelta(days=3)):
    return Window(provider, key, label, used, NOW + resets_in, length)


def _pace(win):
    return compute_pace(win, [], NOW)


def test_build_status_card_shape(tmp_path):
    cfg = _cfg(tmp_path, '[display]\npaused = true\ngauge = "used"\n')
    win = _win("codex", "weekly", 80.0)
    pu = ProviderUsage("codex", "Codex Pro", [win], None, NOW)
    pace = _pace(win)

    status = build_status(cfg, [(pu, pace)], [pu], NOW)

    assert status["paused"] is True
    assert status["gauge"] == "used"
    assert status["updated_at"] == NOW.astimezone().isoformat(timespec="seconds")
    assert len(status["cards"]) == 1
    card = status["cards"][0]
    assert card["id"] == "codex:weekly"
    assert card["provider"] == "codex"
    assert card["name"] == "Codex Pro"
    assert card["label"] == "週間"
    assert card["used"] == 80.0
    assert card["remaining"] == 20.0
    assert card["status"] == pace.status.value
    assert card["status"] == PaceStatus.OVER.value  # 80%/4日 → リセット前に 100% 超え
    assert card["status_label"] == "ペース超過"
    assert card["advice"] == advice_text(pace, False, NOW)
    assert card["resets_at"] == (NOW + timedelta(days=3)).astimezone().isoformat(timespec="seconds")
    assert status["errors"] == []


def test_build_status_labels_match_pace(tmp_path):
    cfg = _cfg(tmp_path)
    win = _win("claude", "weekly", 10.0)
    pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
    status = build_status(cfg, [(pu, _pace(win))], [pu], NOW)
    card = status["cards"][0]
    assert card["status"] == PaceStatus.SPARE.value  # 10%/4日 → 予測 17.5% で余裕あり
    assert card["status_label"] == "余裕あり"


def test_build_status_skips_part_of_windows(tmp_path):
    # 内訳窓 (part_of 付き) は status.json の cards に入らない:
    # ゲージの塗り分け (render.build_cards) 専用の窓なので
    cfg = _cfg(tmp_path)
    main = _win("grok", "weekly", 22.0)
    build = Window("grok", "weekly_build", "週間 Build", 20.0, main.resets_at,
                   main.length, part_of="weekly")
    pu = ProviderUsage("grok", "Grok", [main, build], None, NOW)
    status = build_status(cfg, [(pu, _pace(main)), (pu, _pace(build))], [pu], NOW)
    assert [c["id"] for c in status["cards"]] == ["grok:weekly"]


def test_build_status_errors_list(tmp_path):
    cfg = _cfg(tmp_path)
    bad = ProviderUsage("glm", "GLM", [], "HTTP 401", NOW)
    status = build_status(cfg, [(bad, None)], [bad], NOW)
    assert status["cards"] == []
    assert status["errors"] == [{"provider": "glm", "name": "GLM", "error": "HTTP 401"}]


def test_build_status_defaults(tmp_path):
    status = build_status(_cfg(tmp_path), [], [], NOW)
    assert status["paused"] is False
    assert status["gauge"] == "remaining"
    assert status["cards"] == [] and status["errors"] == []


def test_write_status_is_atomic_and_overwritable(tmp_path):
    path = tmp_path / "sub" / "status.json"
    write_status({"a": 1}, path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert not path.with_name("status.json.tmp").exists()  # rename 後に残らない
    write_status({"a": 2}, path)
    assert json.loads(path.read_text(encoding="utf-8"))["a"] == 2


def test_status_labels_match_render_wording():
    # status.json の status_label は画面表示 (render.py) と同じ文言を保つ
    from quotacanvas.render import _STATUS_BADGE
    from quotacanvas.status import _STATUS_LABEL

    for status, label in _STATUS_BADGE.items():
        assert _STATUS_LABEL[status] == label


def test_write_status_keeps_utf8_and_offset_strings(tmp_path):
    path = tmp_path / "status.json"
    updated = NOW.astimezone().isoformat(timespec="seconds")
    payload = {"updated_at": updated, "advice": "ペース超過 · 残り3日は1日4%まで"}
    write_status(payload, path)
    raw = path.read_text(encoding="utf-8")  # ensure_ascii=False の UTF-8
    assert "ペース超過" in raw
    assert json.loads(raw)["updated_at"] == updated  # オフセット付き ISO 8601


def test_write_frame_png(tmp_path):
    path = tmp_path / "frame.png"
    img = Image.new("RGB", (1920, 462), (11, 13, 18))
    write_frame(img, path)
    assert path.stat().st_size > 0
    with Image.open(path) as loaded:
        assert loaded.size == (1920, 462)


# --- layout / colors (配置画面の初期表示用) -----------------------------------------


def test_build_status_layout_from_render_specs(tmp_path):
    # cli が render に渡した PanelSpec のリストがそのまま dict 配列で入る
    from quotacanvas.render import PanelSpec
    cfg = _cfg(tmp_path)
    win = _win("claude", "weekly", 30.0)
    pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
    used = [PanelSpec(main="claude:weekly", subs=["claude:5h"]),
            PanelSpec(main="codex:weekly", size="small")]
    status = build_status(cfg, [(pu, _pace(win))], [pu], NOW, layout=used)
    assert status["layout"] == [
        {"main": "claude:weekly", "subs": ["claude:5h"], "size": "large"},
        {"main": "codex:weekly", "subs": [], "size": "small"},
    ]


def test_build_status_layout_reports_empty_slots_in_order(tmp_path):
    # 空き段 (main = "" の small) も順番どおりに出る: アプリは同じ列組み立て
    # 規則で列構成を復元できる (上段空き + 下段あり では必須)
    from quotacanvas.render import PanelSpec
    cfg = _cfg(tmp_path)
    used = [PanelSpec(main="", size="small"),
            PanelSpec(main="codex:weekly", size="small"),
            PanelSpec(main="claude:weekly")]
    status = build_status(cfg, [], [], NOW, layout=used)
    assert status["layout"] == [
        {"main": "", "subs": [], "size": "small"},
        {"main": "codex:weekly", "subs": [], "size": "small"},
        {"main": "claude:weekly", "subs": [], "size": "large"},
    ]


def test_build_status_layout_auto_uses_render_auto_layout(tmp_path):
    # layout 指定なし (= 自動配置) のときも、render.auto_layout の結果を報告する
    from quotacanvas.render import auto_layout
    cfg = _cfg(tmp_path)
    w1 = _win("claude", "weekly", 30.0)
    w2 = _win("codex", "weekly", 10.0)
    u1 = ProviderUsage("claude", "Claude Max", [w1], None, NOW)
    u2 = ProviderUsage("codex", "Codex Pro", [w2], None, NOW)
    cards = [(u1, _pace(w1)), (u2, _pace(w2))]
    status = build_status(cfg, cards, [u1, u2], NOW)
    assert status["layout"] == [{"main": sp.main, "subs": list(sp.subs), "size": sp.size}
                                for sp in auto_layout(cards)]
    assert status["layout"][0]["main"] == "claude:weekly"  # 長い窓が main になる


def test_build_status_layout_omitted_without_render_support(tmp_path, monkeypatch):
    # render 側がまだ auto_layout を持たない場合は省略 (互換)
    import quotacanvas.render as render_mod
    monkeypatch.delattr(render_mod, "auto_layout")
    status = build_status(_cfg(tmp_path), [], [], NOW)
    assert "layout" not in status


def test_build_status_colors_defaults_and_overrides(tmp_path):
    # 既定色 (BRAND_COLORS) 込み。config の providers.<p>.color で上書き
    from quotacanvas.render import BRAND_COLORS
    cfg = _cfg(tmp_path, '[providers.claude]\ncolor = "#FF0000"\n')
    status = build_status(cfg, [], [], NOW)
    expected = dict(BRAND_COLORS)
    expected["claude"] = "#FF0000"
    assert status["colors"] == expected


def test_build_status_colors_omitted_without_render_support(tmp_path, monkeypatch):
    # render 側がまだ BRAND_COLORS を持たない場合は省略 (互換)
    import quotacanvas.render as render_mod
    monkeypatch.delattr(render_mod, "BRAND_COLORS")
    status = build_status(_cfg(tmp_path), [], [], NOW)
    assert "colors" not in status


# --- reset_credits (Codex のリセット権、トップレベル) -------------------------------


def test_build_status_reset_credits(tmp_path):
    cfg = _cfg(tmp_path)
    win = _win("codex", "weekly", 10.0)
    pu = ProviderUsage("codex", "Codex Pro", [win], None, NOW, reset_credits=[
        ResetCredit("Full reset", NOW - timedelta(days=1), NOW + timedelta(days=5)),
    ])
    status = build_status(cfg, [(pu, _pace(win))], [pu], NOW)
    entry = status["reset_credits"]["codex"][0]
    assert entry["title"] == "Full reset"
    assert entry["granted_at"] == (NOW - timedelta(days=1)).astimezone().isoformat(timespec="seconds")
    assert entry["expires_at"] == (NOW + timedelta(days=5)).astimezone().isoformat(timespec="seconds")
    assert entry["expires_at"][-6] in "+-"  # オフセット付き ISO 8601


def test_build_status_reset_credits_omitted_when_none(tmp_path):
    cfg = _cfg(tmp_path)
    win = _win("claude", "weekly", 10.0)
    pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
    status = build_status(cfg, [(pu, _pace(win))], [pu], NOW)
    assert "reset_credits" not in status


# --- card の追加フィールド (previous_used / exhausts_at / burst_exhausts_at) ----


def test_card_reports_over_fields_and_previous_used(tmp_path):
    # 80% / 残り 3日 / 長さ 7日 (経過 4日) -> 1日で 100% に届く OVER
    cfg = _cfg(tmp_path)
    win = _win("claude", "weekly", 80.0, resets_in=timedelta(days=3))
    pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
    pace = replace(_pace(win), previous_used=64.0)
    status = build_status(cfg, [(pu, pace)], [pu], NOW)
    card = status["cards"][0]
    assert card["status"] == PaceStatus.OVER.value
    assert card["exhausts_at"] == pace.exhausts_at.astimezone().isoformat(timespec="seconds")
    assert card["previous_used"] == 64.0
    assert "burst_exhausts_at" not in card  # 無ければ省略


def test_card_reports_burst_exhausts_at(tmp_path):
    cfg = _cfg(tmp_path)
    win = _win("claude", "weekly", 80.0, resets_in=timedelta(days=3))
    pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
    pace = replace(_pace(win), burst_exhausts_at=NOW + timedelta(hours=5))
    status = build_status(cfg, [(pu, pace)], [pu], NOW)
    assert status["cards"][0]["burst_exhausts_at"] == \
        (NOW + timedelta(hours=5)).astimezone().isoformat(timespec="seconds")


def test_card_omits_optional_fields_when_none(tmp_path):
    cfg = _cfg(tmp_path)
    win = _win("claude", "weekly", 10.0)  # SPARE
    pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
    status = build_status(cfg, [(pu, _pace(win))], [pu], NOW)
    card = status["cards"][0]
    assert "previous_used" not in card
    assert "exhausts_at" not in card
    assert "burst_exhausts_at" not in card
    assert "stale_error" not in card


def test_card_reports_fresh_status(tmp_path):
    # リセット直後 (F5): status の値は "fresh"、ラベルは render.badge.fresh
    cfg = _cfg(tmp_path)
    win = _win("claude", "weekly", 1.0, resets_in=timedelta(days=7))
    pace = replace(_pace(win), status=PaceStatus.FRESH)
    pu = ProviderUsage("claude", "Claude Max", [win], None, NOW)
    status = build_status(cfg, [(pu, pace)], [pu], NOW)
    card = status["cards"][0]
    assert card["status"] == "fresh"
    assert card["status_label"] == "リセット直後"


# --- recommendation (今使う先、トップレベル) ------------------------------------


def test_build_status_recommendation_payload(tmp_path):
    cfg = _cfg(tmp_path)
    win = _win("codex", "weekly", 10.0)
    pu = ProviderUsage("codex", "Codex Pro", [win], None, NOW)
    rec = Recommendation(
        provider="codex", name="Codex", window_id="codex:weekly",
        text="Codex を使う · 週の残り 62% · リセットまで 1日3時間",
        ranking=[{"provider": "codex", "name": "Codex", "window_id": "codex:weekly",
                  "usable": True, "score": 1.23, "remaining": 62.0,
                  "resets_at": None, "reason": "週の残り 62%"}],
    )
    status = build_status(cfg, [(pu, _pace(win))], [pu], NOW, recommendation=rec)
    assert status["recommendation"]["provider"] == "codex"
    assert status["recommendation"]["text"] == rec.text
    assert status["recommendation"]["ranking"] == rec.ranking


def test_build_status_recommendation_accepts_ready_dict(tmp_path):
    cfg = _cfg(tmp_path)
    rec = {"provider": "glm", "name": "GLM", "window_id": "glm:5h", "text": "GLM を使う",
           "ranking": []}
    status = build_status(cfg, [], [], NOW, recommendation=rec)
    assert status["recommendation"] == rec


def test_build_status_recommendation_omitted_when_none(tmp_path):
    cfg = _cfg(tmp_path)
    status = build_status(cfg, [], [], NOW, recommendation=None)
    assert "recommendation" not in status

def test_card_reports_fetched_at_separately_from_updated_at(tmp_path):
    win = _win("codex", "weekly", 30.0)
    fetched = NOW - timedelta(hours=3)
    pu = ProviderUsage("codex", "Codex Pro", [win], None, fetched)
    status = build_status(_cfg(tmp_path), [(pu, _pace(win))], [pu], NOW)
    card = status["cards"][0]
    assert card["fetched_at"] == fetched.astimezone().isoformat(timespec="seconds")
    assert status["updated_at"] == NOW.astimezone().isoformat(timespec="seconds")


def test_card_omits_fetched_at_when_unknown(tmp_path):
    win = _win("codex", "weekly", 30.0)
    pu = ProviderUsage("codex", "Codex Pro", [win], None, None)
    status = build_status(_cfg(tmp_path), [(pu, _pace(win))], [pu], NOW)
    assert "fetched_at" not in status["cards"][0]


def test_stale_card_reports_last_successful_fetch_time(tmp_path):
    win = _win("codex", "weekly", 30.0)
    fetched = NOW - timedelta(minutes=45)
    pu = replace(ProviderUsage("codex", "Codex Pro", [win], None, fetched),
                 stale_error="HTTP 429")
    card = build_status(_cfg(tmp_path), [(pu, _pace(win))], [pu], NOW)["cards"][0]
    assert card["stale_error"] == "HTTP 429"
    assert card["fetched_at"] == fetched.astimezone().isoformat(timespec="seconds")
