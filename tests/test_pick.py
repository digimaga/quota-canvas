"""Tests for `quota-canvas pick` (F1): status.json を読む / 古ければ 1 回だけ取得。

本物の ~/Library/Application Support には触れない (STATUS_PATH / history を tmp に)。
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

from quotacanvas import cli, providers
from quotacanvas.models import ProviderUsage, Window
from quotacanvas.status import write_status

UTC = timezone.utc
NOW = datetime.now(UTC)  # status.json の新しさは実時刻で判定されるので実時刻を使う

REC = {
    "provider": "codex", "name": "Codex", "window_id": "codex:weekly",
    "text": "Codex を使う · 週間の残り 62% · リセットまで 1日3時間",
    "ranking": [{"provider": "codex", "name": "Codex", "window_id": "codex:weekly",
                 "usable": True, "score": 1.23, "remaining": 62.0,
                 "resets_at": "2026-10-26T12:00:00+00:00", "reason": "週間の残り 62%"}],
}


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def _argv(tmp_path, *extra):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[display]\nlanguage = "ja"\n', encoding="utf-8")
    return ["--config", str(cfg_path), "--history", str(tmp_path / "history.jsonl"),
            "pick", *extra]


def _status_file(tmp_path, updated_at, recommendation=REC):
    path = tmp_path / "status.json"
    payload = {"updated_at": updated_at.astimezone(UTC).isoformat(), "cards": []}
    if recommendation is not None:
        payload["recommendation"] = recommendation
    write_status(payload, path)
    return path


def _fetch_result(now=None):
    now = now or NOW
    win = Window("claude", "weekly", "週間", 30.0, now + timedelta(days=4), timedelta(days=7))
    return [ProviderUsage("claude", "Claude Max", [win], None, now),
            ProviderUsage("codex", "Codex", [], "HTTP 401", now),
            ProviderUsage("glm", "GLM", [], "HTTP 401", now),
            ProviderUsage("grok", "Grok", [], "HTTP 401", now)]


# --------------------------------------------------------- 新しい status.json

def test_pick_uses_fresh_status_json(monkeypatch, capsys, tmp_path):
    path = _status_file(tmp_path, NOW)
    monkeypatch.setattr(cli, "STATUS_PATH", path)
    monkeypatch.setattr(providers, "fetch_all",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("取得しない")))
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path))
    assert rc == 0
    assert out.strip() == REC["text"]


def test_pick_json_prints_recommendation_as_is(monkeypatch, capsys, tmp_path):
    path = _status_file(tmp_path, NOW)
    monkeypatch.setattr(cli, "STATUS_PATH", path)
    monkeypatch.setattr(providers, "fetch_all",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("取得しない")))
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path, "--json"))
    assert rc == 0
    assert json.loads(out) == REC


# --------------------------------------- 無い / 古い -> 1 回だけ取得して計算

def _mock_fetch(monkeypatch, calls):
    def fake(now=None, enabled=None):
        calls.append(True)
        return _fetch_result()
    monkeypatch.setattr(providers, "fetch_all", fake)


def test_pick_refetches_when_status_is_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "STATUS_PATH", tmp_path / "missing.json")
    calls = []
    _mock_fetch(monkeypatch, calls)
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path))
    assert rc == 0
    assert calls == [True]  # 取得は 1 回だけ
    # リセットまでの時間は実時刻との差なので、先頭と残りの表示だけ見る
    assert out.strip().startswith("Claude Max を使う · 週間の残り 70% · リセットまで ")
    assert not (tmp_path / "missing.json").exists()  # status.json は書かない


def test_pick_refetch_writes_no_history_or_periods(monkeypatch, capsys, tmp_path):
    # 取得し直すときは読み取り専用: 走行中の run と書き込みが重ならないよう
    # history.jsonl / periods.jsonl / last-good.json も作らない
    monkeypatch.setattr(cli, "STATUS_PATH", tmp_path / "missing.json")
    calls = []
    _mock_fetch(monkeypatch, calls)
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path))
    assert rc == 0
    assert calls == [True]  # 取得自体は行っている
    assert not (tmp_path / "history.jsonl").exists()
    assert not (tmp_path / "periods.jsonl").exists()
    assert not (tmp_path / "last-good.json").exists()


def test_pick_refetches_when_status_is_stale(monkeypatch, capsys, tmp_path):
    path = _status_file(tmp_path, NOW - timedelta(minutes=21))
    monkeypatch.setattr(cli, "STATUS_PATH", path)
    calls = []
    _mock_fetch(monkeypatch, calls)
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path))
    assert rc == 0
    assert calls == [True]
    assert "Claude Max" in out  # status.json の text ではなく計算し直した結果
    before = path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8") == before  # 書き換えない


def test_pick_accepts_status_just_under_20_minutes(monkeypatch, capsys, tmp_path):
    path = _status_file(tmp_path, NOW - timedelta(minutes=19))
    monkeypatch.setattr(cli, "STATUS_PATH", path)
    monkeypatch.setattr(providers, "fetch_all",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("取得しない")))
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path))
    assert rc == 0
    assert out.strip() == REC["text"]


# ------------------------------------------------------------- 推奨なし

def test_pick_json_none_when_nothing_usable(monkeypatch, capsys, tmp_path):
    # 新しい status.json に推奨が無い -> 取得し直しても全滅なら {"recommendation": null}
    path = _status_file(tmp_path, NOW, recommendation=None)
    monkeypatch.setattr(cli, "STATUS_PATH", path)

    def fake(now=None, enabled=None):
        return [ProviderUsage(pid, pid.title(), [], "HTTP 500", NOW)
                for pid in ("claude", "codex", "glm", "grok")]
    monkeypatch.setattr(providers, "fetch_all", fake)
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path, "--json"))
    assert rc == 0
    assert json.loads(out) == {"recommendation": None}


def test_pick_refetches_when_fresh_status_lacks_recommendation(monkeypatch, capsys, tmp_path):
    # 古いエンジンの status.json (recommendation のキー無し): 取得して計算する
    path = _status_file(tmp_path, NOW, recommendation=None)
    monkeypatch.setattr(cli, "STATUS_PATH", path)
    monkeypatch.setattr(providers, "fetch_all",
                        lambda now=None, enabled=None: _fetch_result())
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path))
    assert rc == 0
    assert "Claude Max" in out


def test_pick_none_message_when_computation_finds_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "STATUS_PATH", tmp_path / "missing.json")

    def fake(now=None, enabled=None):
        return [ProviderUsage(pid, pid.title(), [], "HTTP 500", NOW)
                for pid in ("claude", "codex", "glm", "grok")]
    monkeypatch.setattr(providers, "fetch_all", fake)
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path))
    assert rc == 0
    assert out.strip() == "いまおすすめのサービスはありません"


def test_pick_broken_status_json_is_refetched(monkeypatch, capsys, tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(cli, "STATUS_PATH", path)
    calls = []
    _mock_fetch(monkeypatch, calls)
    rc, out = _run(monkeypatch, capsys, _argv(tmp_path))
    assert rc == 0
    assert calls == [True]
    assert "Claude Max" in out
