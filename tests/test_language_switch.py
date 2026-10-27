"""言語切り替えの一貫性のテスト。

- 取得時に焼き付いたエラー文 (ja の「認証情報なし」など) は、表示言語を
  切り替えたあとも no_credentials として扱う (translations の集合で比較)。
- detect / 設定エラー文は、設定を読む前に display.language に従う。
- ゲージ左の「枯渇」マークは en ("Exhausted") でも塗りの幅に収まる。
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from PIL import Image

from quotacanvas import cli, i18n, providers
from quotacanvas.history import History
from quotacanvas.i18n import translations
from quotacanvas.models import Pace, PaceStatus, ProviderUsage, Window
from quotacanvas.render import _S, _draw_gauge

NOW = datetime(2026, 10, 24, 9, 0, 0, tzinfo=timezone.utc)


def _run(monkeypatch, capsys, argv, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


# ------------------------------------------- (a) 焼き付いたエラー文の比較

def test_no_credentials_fetched_in_ja_is_skipped_in_en(tmp_path):
    """ja で取得した「認証情報なし」も、en 表示ではエラーカードにならない。"""
    history = History(tmp_path / "history.jsonl")
    no_creds = ProviderUsage("glm", "GLM (Z.ai)", [], "認証情報なし", NOW)
    real_error = ProviderUsage("glm", "GLM (Z.ai)", [], "API キーが未設定", NOW)
    assert translations("providers.no_credentials") == {"認証情報なし", "No credentials"}
    i18n.set_language("en")
    assert cli.build_cards([no_creds], history, NOW) == []
    cards = cli.build_cards([real_error], history, NOW)
    assert len(cards) == 1 and cards[0][1] is None  # 本物のエラーはカードのまま
    i18n.set_language("ja")  # 逆方向も同じく
    assert cli.build_cards([
        ProviderUsage("glm", "GLM (Z.ai)", [], "No credentials", NOW)], history, NOW) == []


# ------------------------------------ (b) 設定ファイルの language を先に反映

def test_detect_uses_config_language(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[display]\nlanguage = "en"\n', encoding="utf-8")
    monkeypatch.setattr(providers, "detect_all", lambda now=None: [
        {"provider": "glm", "name": "GLM (Z.ai)", "detected": False, "source": None,
         "needs_key": True, "plan": None, "windows": [], "error": "API キーが未設定"},
    ])
    rc, out = _run(monkeypatch, capsys, ["--config", str(cfg), "detect"])
    assert rc == 0
    assert "unusable: API キーが未設定" in out  # cli.detect.unusable の en 文
    assert i18n.current_language() == "en"


def test_config_error_uses_config_language(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[display]\nlanguage = "en"\nbrightness = 200\n', encoding="utf-8")
    rc, out = _run(monkeypatch, capsys, ["--config", str(cfg), "config", "--json"])
    assert rc == 1
    assert "must be an integer" in out  # config.int_required の en 文


def test_early_language_falls_back_to_auto(monkeypatch, tmp_path):
    monkeypatch.setattr(i18n, "system_language", lambda: "ja")
    assert cli._early_language(["detect"]) == "auto"  # 設定ファイルが無い
    bad = tmp_path / "broken.toml"
    bad.write_text("[display\n", encoding="utf-8")
    assert cli._early_language(["--config", str(bad), "detect"]) == "auto"  # 壊れている
    unk = tmp_path / "unknown.toml"
    unk.write_text('[display]\nlanguage = "fr"\n', encoding="utf-8")
    assert cli._early_language(["--config=" + str(unk), "detect"]) == "auto"  # 不正な値


# ------------------------------- (c) 枯渇マークが狭い塗りでも描ける

def _exhausted_pace(win):
    return Pace(window=win, status=PaceStatus.EXHAUSTED, elapsed_fraction=0.3,
                ideal_used_percent=30.0, rate_per_day=20.0,
                projected_end_percent=140.0, exhausts_at=None,
                sustainable_per_day=1.0, adjust_percent=None)


def test_exhausted_mark_draws_on_narrow_fill():
    """残り 1% (塗り幅 4px 弱) でも en の "Exhausted" が例外なく描ける。"""
    img = Image.new("RGB", (400, 100), "black")
    s = _S(img, (0, 0))
    win = Window("grok", "weekly", "Weekly", 99.0, NOW + timedelta(days=7),
                 timedelta(days=7))
    i18n.set_language("en")
    caption = _draw_gauge(s, (10.0, 40.0, 400.0, 80.0), win, _exhausted_pace(win),
                          (200, 60, 60), (255, 80, 80), "remaining")
    assert caption
    i18n.set_language("ja")
    assert _draw_gauge(s, (10.0, 40.0, 400.0, 80.0), win, _exhausted_pace(win),
                       (200, 60, 60), (255, 80, 80), "remaining")
