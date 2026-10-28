"""routing CLI のテスト: exit コード (0/2/3)、enabled=false、QUOTA_GATE_FORCE、
--status / QUOTA_STATUS_PATH、--fallback-status / QUOTA_STATUS_FALLBACK、burn。

routing は絶対にサービスへ取得しない (status.json を読むだけ) ので、
全テストで providers.fetch_all を例外にして保証する。実際の I/O は
tmp_path と gate.BURN_PATH の差し替え先だけ。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from quotacanvas import cli, gate, providers

# ------------------------------------------------------------------ 下準備


@pytest.fixture(autouse=True)
def _no_fetch(monkeypatch):
    """routing が取得に走ったらテストを失敗させる。"""

    def _boom(*args, **kwargs):
        raise AssertionError("routing がサービスへ取得した")

    monkeypatch.setattr(providers, "fetch_all", _boom)


@pytest.fixture(autouse=True)
def _restore_language():
    """CLI が起動時に set_language するので、テスト後に元に戻す。"""
    from quotacanvas import i18n

    prev = i18n.current_language()
    yield
    i18n.set_language(prev)


@pytest.fixture(autouse=True)
def burn_path(monkeypatch, tmp_path):
    """gate.BURN_PATH を tmp に差し替える (本物の burn 状態は触らない・
    実機の使い切りモードが line / check の結果に漏れないようにする)。"""
    path = tmp_path / "routing-burn.json"
    monkeypatch.setattr(gate, "BURN_PATH", path)
    monkeypatch.setattr(gate, "OFF_PATH", tmp_path / "routing-off.json")
    return path


def _write_config(path, *, enabled=True, workers=None):
    lines = ['[display]', 'language = "ja"', "", "[poll]", 'interval = 300', ""]
    lines += ["[routing]", "enabled = %s" % ("true" if enabled else "false"), ""]
    if workers:
        lines.append("[routing.workers]")
        for name, ids in workers.items():
            quoted = ", ".join('"%s"' % i for i in ids)
            lines.append("%s = [%s]" % (name, quoted))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _win(wid, provider, status, remaining, *, fetched=None, stale_error=None):
    now = datetime.now().astimezone()
    entry = {
        "id": wid,
        "provider": provider,
        "key": wid.split(":", 1)[1],
        "part_of": None,
        "used": round(100.0 - remaining, 1),
        "remaining": remaining,
        "status": status,
        "resets_at": (now + timedelta(days=2)).isoformat(timespec="seconds"),
    }
    if fetched is not False:
        entry["fetched_at"] = (fetched or now).isoformat(timespec="seconds")
    if stale_error is not None:
        entry["stale_error"] = stale_error
    return entry


def _write_status(path, windows, *, errors=None, age_seconds=0):
    now = datetime.now().astimezone()
    updated = now - timedelta(seconds=age_seconds)
    data = {
        "updated_at": updated.isoformat(timespec="seconds"),
        "windows": windows,
        "errors": errors or [],
        "reset_credits": {},
        "cards": [],
        "recommendation": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _run(tmp_path, argv, *, status=None, config=None, env=None):
    """cli.main を実行して (rc, out, err) を返す。"""
    import io
    import sys

    argv = ["--config", str(config or _write_config(tmp_path / "cfg.toml"))] + list(argv)
    if status is not None:
        argv += ["--status", str(status)]
    monkeypatched = None
    old = {}
    if env:
        import os

        for key, value in env.items():
            old[key] = os.environ.get(key)
            os.environ[key] = value
        monkeypatched = os
    try:
        import contextlib

        buf_out, buf_err = io.StringIO(), io.StringIO()
        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = io.StringIO(), buf_out, buf_err
        try:
            rc = cli.main(list(argv))
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_stdin, old_stdout, old_stderr
        return rc, buf_out.getvalue(), buf_err.getvalue()
    finally:
        if monkeypatched is not None:
            for key, value in old.items():
                if value is None:
                    monkeypatched.environ.pop(key, None)
                else:
                    monkeypatched.environ[key] = value


# ------------------------------------------------------------------ routing line


def test_line_disabled_is_silent_and_exit0(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    cfg = _write_config(tmp_path / "cfg.toml", enabled=False)
    rc, out, err = _run(tmp_path, ["routing", "line"], status=status, config=cfg)
    assert (rc, out, err) == (0, "", "")


def test_line_enabled_prints_line_and_exit0(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, err = _run(tmp_path, ["routing", "line"], status=status)
    assert rc == 0 and err == ""
    assert re.fullmatch(r"quota: glm=boost\(70%(?:,~\d\d/\d\d \d\d:\d\d)?\) \(\d\d:\d\d時点\)\n", out), out


def test_line_json_outputs_result(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, _ = _run(tmp_path, ["routing", "line", "--json"], status=status)
    assert rc == 0
    data = json.loads(out)
    assert data["workers"][0]["name"] == "glm"
    assert data["workers"][0]["verdict"] == "boost"


def test_line_missing_status_prints_stale_line_exit0(tmp_path, capsys):
    rc, out, err = _run(tmp_path, ["routing", "line"], status=tmp_path / "none.json")
    assert rc == 0 and err == ""
    assert "stale" in out


def test_line_exception_failopen_exit0_stderr_one_line(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])

    def _boom(*args, **kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(gate, "evaluate", _boom)
    rc, out, err = _run(tmp_path, ["routing", "line"], status=status)
    assert rc == 0 and out == ""
    assert err == "routing: broken\n"


def test_line_never_fetches_even_without_status(tmp_path):
    # _no_fetch fixture が fetch_all を例外化済み。取得なしで完走する。
    rc, out, _ = _run(tmp_path, ["routing", "line"], status=tmp_path / "none.json")
    assert rc == 0 and "stale" in out


# ------------------------------------------------------------------ routing check


def test_check_stop_exit3_with_one_stderr_line(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "exhausted", 0.0)])
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"], status=status)
    assert rc == 3 and out == ""
    assert err.count("\n") == 1 and "stop" in err


def test_check_stop_json_exit3(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "exhausted", 0.0)])
    rc, out, _ = _run(tmp_path, ["routing", "check", "glm", "--json"], status=status)
    assert rc == 3
    assert json.loads(out)["verdict"] == "stop"


def test_check_reserve_exit0_with_note(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "over", 80.0)])
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"], status=status)
    assert rc == 0 and out == ""
    assert err.count("\n") == 1 and "reserve" in err


def test_check_unknown_missing_exit0_with_message(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    cfg = _write_config(tmp_path / "cfg.toml", workers={"glm": ["glm:5h"], "grok": ["grok:*"]})
    rc, out, err = _run(tmp_path, ["routing", "check", "grok"], status=status, config=cfg)
    assert rc == 0 and out == ""
    assert "unknown" in err and "判定できない" in err


def test_check_go_and_boost_are_silent(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0),
                           _win("codex:weekly", "codex", "on_track", 40.0)])
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"], status=status)
    assert (rc, out, err) == (0, "", "")
    rc, out, err = _run(tmp_path, ["routing", "check", "codex"], status=status)
    assert (rc, out, err) == (0, "", "")


def test_check_unknown_name_exit2(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, err = _run(tmp_path, ["routing", "check", "nosuch"], status=status)
    assert rc == 2


def test_check_disabled_is_silent_exit0(tmp_path):
    cfg = _write_config(tmp_path / "cfg.toml", enabled=False)
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"],
                        status=tmp_path / "none.json", config=cfg)
    assert (rc, out, err) == (0, "", "")


def test_check_force_env_exit0_silent(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "exhausted", 0.0)])
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"], status=status,
                        env={"QUOTA_GATE_FORCE": "1"})
    assert (rc, out, err) == (0, "", "")


def test_check_stale_status_reports_unknown_exit0(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "exhausted", 0.0)], age_seconds=3600)
    cfg = _write_config(tmp_path / "cfg.toml", workers={"glm": ["glm:5h"]})
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"], status=status, config=cfg)
    assert rc == 0 and "unknown" in err


# ------------------------------------------------------------------ status / env / fallback


def test_status_env_var_is_honored(tmp_path, monkeypatch):
    status = tmp_path / "env-status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    monkeypatch.setenv("QUOTA_STATUS_PATH", str(status))
    rc, out, _ = _run(tmp_path, ["routing", "line"])
    assert rc == 0 and out.startswith("quota: glm=boost(")


def test_fallback_used_when_primary_missing(tmp_path):
    primary = tmp_path / "primary.json"
    fallback = tmp_path / "fallback.json"
    _write_status(fallback, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, _ = _run(tmp_path, ["routing", "line", "--fallback-status", str(fallback)],
                      status=primary)
    assert rc == 0 and out.startswith("quota: glm=boost(")


def test_fallback_env_used_when_primary_missing(tmp_path):
    fallback = tmp_path / "fallback.json"
    _write_status(fallback, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, _ = _run(tmp_path, ["routing", "line"], status=tmp_path / "none.json",
                      env={"QUOTA_STATUS_FALLBACK": str(fallback)})
    assert rc == 0 and out.startswith("quota: glm=boost(")


def test_fallback_used_when_primary_too_old(tmp_path):
    primary = tmp_path / "primary.json"
    fallback = tmp_path / "fallback.json"
    _write_status(primary, [_win("glm:5h", "glm", "exhausted", 0.0)], age_seconds=3600)
    _write_status(fallback, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, _ = _run(tmp_path, ["routing", "line"], status=primary,
                      env={"QUOTA_STATUS_FALLBACK": str(fallback)})
    assert rc == 0 and out.startswith("quota: glm=boost(")


def test_fallback_ignored_while_primary_fresh(tmp_path):
    primary = tmp_path / "primary.json"
    fallback = tmp_path / "fallback.json"
    _write_status(primary, [_win("glm:5h", "glm", "spare", 70.0)])
    fallback.write_text("{ broken", encoding="utf-8")
    rc, out, _ = _run(tmp_path, ["routing", "line"], status=primary,
                      env={"QUOTA_STATUS_FALLBACK": str(fallback)})
    assert rc == 0 and out.startswith("quota: glm=boost(")


def test_both_stale_reports_stale(tmp_path):
    primary = tmp_path / "primary.json"
    fallback = tmp_path / "fallback.json"
    _write_status(primary, [_win("glm:5h", "glm", "spare", 70.0)], age_seconds=3600)
    _write_status(fallback, [_win("glm:5h", "glm", "spare", 70.0)], age_seconds=7200)
    rc, out, _ = _run(tmp_path, ["routing", "line"], status=primary,
                      env={"QUOTA_STATUS_FALLBACK": str(fallback)})
    assert rc == 0 and "stale" in out


# ------------------------------------------------------------------ routing burn


def test_burn_list_empty(tmp_path, burn_path):
    rc, out, _ = _run(tmp_path, ["routing", "burn"])
    assert rc == 0 and "なし" in out


def test_burn_on_default_until_2359_then_off(tmp_path, burn_path):
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "on"])
    assert rc == 0 and "glm" in out
    data = json.loads(burn_path.read_text(encoding="utf-8"))
    assert data["glm"][11:16] == "23:59"  # burn ファイルは {名前: ISO 期限}
    rc, out, _ = _run(tmp_path, ["routing", "burn"])
    assert "glm" in out and "23:59" in out
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "off"])
    assert rc == 0
    data = json.loads(burn_path.read_text(encoding="utf-8"))
    assert "glm" not in data


def test_burn_on_with_until(tmp_path, burn_path):
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "on", "--until", "22:30"])
    assert rc == 0
    data = json.loads(burn_path.read_text(encoding="utf-8"))
    assert data["glm"][11:16] == "22:30"


def test_burn_bad_until_exit2(tmp_path, burn_path):
    rc, out, err = _run(tmp_path, ["routing", "burn", "glm", "on", "--until", "25:00"])
    assert rc == 2 and "HH:MM" in err
    assert not burn_path.exists()


def test_burn_unknown_name_exit2(tmp_path, burn_path):
    rc, out, err = _run(tmp_path, ["routing", "burn", "nosuch", "on"])
    assert rc == 2 and not burn_path.exists()


def test_burn_expired_entries_purged_on_write(tmp_path, burn_path):
    now = datetime.now().astimezone()
    burn_path.parent.mkdir(parents=True, exist_ok=True)
    burn_path.write_text(json.dumps({
        "glm": (now - timedelta(days=2)).isoformat(timespec="seconds"),
        "grok": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    rc, out, _ = _run(tmp_path, ["routing", "burn"])
    assert rc == 0
    assert "glm" not in out and "grok" in out  # 表示には出ない
    # 書き込みが起きる操作 (off) のときに期限切れが掃除される
    rc, out, _ = _run(tmp_path, ["routing", "burn", "grok", "off"])
    assert rc == 0
    data = json.loads(burn_path.read_text(encoding="utf-8"))
    assert set(data) == set()


# ------------------------------------------------------------------ burn --json


def test_burn_json_list_empty(tmp_path):
    rc, out, _ = _run(tmp_path, ["routing", "burn", "--json"])
    assert rc == 0
    assert json.loads(out) == {"burn": []}


def test_burn_json_on_off_and_list_share_shape(tmp_path, burn_path):
    # on --json: 実行後の有効エントリを同じ形で出す
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "on", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [e["name"] for e in data["burn"]] == ["glm"]
    assert data["burn"][0]["until"][11:16] == "23:59"  # 既定は今日 23:59
    assert "glm" in json.loads(burn_path.read_text(encoding="utf-8"))
    # 一覧 --json も同じ形
    rc, out, _ = _run(tmp_path, ["routing", "burn", "--json"])
    assert json.loads(out) == data
    # off --json で消える
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "off", "--json"])
    assert rc == 0
    assert json.loads(out) == {"burn": []}
    assert json.loads(burn_path.read_text(encoding="utf-8")) == {}


def test_burn_json_list_sorted_by_name(tmp_path, burn_path):
    _run(tmp_path, ["routing", "burn", "glm", "on"])
    _run(tmp_path, ["routing", "burn", "codex", "on"])
    rc, out, _ = _run(tmp_path, ["routing", "burn", "--json"])
    assert rc == 0
    assert [e["name"] for e in json.loads(out)["burn"]] == ["codex", "glm"]


def test_burn_json_unknown_name_exit2_error_json(tmp_path, burn_path):
    rc, out, _ = _run(tmp_path, ["routing", "burn", "nosuch", "on", "--json"])
    assert rc == 2
    assert "error" in json.loads(out)
    assert not burn_path.exists()


def test_burn_off_allows_name_absent_from_workers(tmp_path, burn_path):
    # off は workers に無い名前でも、burn 状態にその名前があれば解除できる
    now = datetime.now().astimezone()
    burn_path.parent.mkdir(parents=True, exist_ok=True)
    burn_path.write_text(json.dumps({
        "legacy": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    cfg = _write_config(tmp_path / "cfg.toml", workers={"glm": ["glm:5h"]})
    rc, out, _ = _run(tmp_path, ["routing", "burn", "legacy", "off"], config=cfg)
    assert rc == 0 and "legacy" in out
    assert json.loads(burn_path.read_text(encoding="utf-8")) == {}


def test_burn_off_json_allows_burned_unknown_name(tmp_path, burn_path):
    # --json でも同じ (実行後の一覧を同じ形で出す)
    now = datetime.now().astimezone()
    burn_path.parent.mkdir(parents=True, exist_ok=True)
    burn_path.write_text(json.dumps({
        "legacy": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
        "glm": (now + timedelta(hours=2)).isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    cfg = _write_config(tmp_path / "cfg.toml", workers={"glm": ["glm:5h"]})
    rc, out, _ = _run(tmp_path, ["routing", "burn", "legacy", "off", "--json"],
                      config=cfg)
    assert rc == 0
    assert [e["name"] for e in json.loads(out)["burn"]] == ["glm"]
    assert set(json.loads(burn_path.read_text(encoding="utf-8"))) == {"glm"}


def test_burn_off_unknown_name_without_entry_exit2(tmp_path, burn_path):
    # burn 状態にも無い未知名の off は従来どおり exit 2
    cfg = _write_config(tmp_path / "cfg.toml", workers={"glm": ["glm:5h"]})
    rc, out, _ = _run(tmp_path, ["routing", "burn", "legacy", "off"], config=cfg)
    assert rc == 2 and not burn_path.exists()


def test_burn_on_still_rejects_unknown_name(tmp_path, burn_path):
    # on の既知チェックはそのまま (workers に無い名前は exit 2)
    cfg = _write_config(tmp_path / "cfg.toml", workers={"glm": ["glm:5h"]})
    rc, out, _ = _run(tmp_path, ["routing", "burn", "legacy", "on"], config=cfg)
    assert rc == 2 and not burn_path.exists()


def test_burn_without_json_output_unchanged(tmp_path):
    # --json なしのテキスト出力は従来どおり
    rc, out, _ = _run(tmp_path, ["routing", "burn"])
    assert rc == 0 and "なし" in out and "{" not in out
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "on"])
    assert rc == 0 and "23:59" in out and "{" not in out


# ------------------------------------------------------------------ その他


def test_routing_without_subcommand_prints_usage_exit2(tmp_path):
    import pytest as _pytest

    with _pytest.raises(SystemExit) as exc:  # required=True なので argparse が落とす
        _run(tmp_path, ["routing"])
    assert exc.value.code == 2


def test_routing_burn_promotes_stop_in_check_not_forced(tmp_path):
    """burn on にすると reserve / go が boost に上がる (stop は上がらない)。"""
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "over", 80.0)])
    gate.store_burn(gate.BURN_PATH, "glm", datetime.now().astimezone() + timedelta(hours=2))
    rc, out, _ = _run(tmp_path, ["routing", "check", "glm"], status=status)
    assert rc == 0  # reserve だが burn で boost → ノートも出ない
    assert out == "" and "reserve" not in (out or "")


# ------------------------------------------------------------------ レビュー対応分


def test_check_known_name_without_windows_is_unknown_exit0(tmp_path):
    """既知名 (KNOWN_PROVIDERS / workers のキー) は status に窓が無くても
    unknown (missing) で exit 0。引数の妥当性を status の中身に依存させない。"""
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, err = _run(tmp_path, ["routing", "check", "gemini"], status=status)
    assert rc == 0 and out == ""
    assert "gemini=unknown" in err and "missing" in err


def test_check_unknown_name_exit2_even_when_status_stale(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)], age_seconds=3600)
    rc, out, err = _run(tmp_path, ["routing", "check", "nosuch"], status=status)
    assert rc == 2


def test_check_json_missing_status_outputs_unknown_json(tmp_path):
    rc, out, err = _run(tmp_path, ["routing", "check", "glm", "--json"],
                        status=tmp_path / "none.json")
    assert rc == 0 and err == ""
    data = json.loads(out)
    assert (data["name"], data["verdict"], data["reason"], data["stale"]) \
        == ("glm", "unknown", "missing_status", True)


def test_check_json_old_status_reason_stale(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)], age_seconds=3600)
    rc, out, _ = _run(tmp_path, ["routing", "check", "glm", "--json"], status=status)
    assert rc == 0
    data = json.loads(out)
    assert (data["verdict"], data["reason"]) == ("unknown", "stale")


def test_check_broken_status_reason_broken(tmp_path):
    status = tmp_path / "status.json"
    status.write_text("{ broken", encoding="utf-8")
    rc, out, _ = _run(tmp_path, ["routing", "check", "glm", "--json"], status=status)
    assert rc == 0
    assert json.loads(out)["reason"] == "broken"
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"], status=status)
    assert rc == 0 and "broken" in err  # 人間向け行も detail に応じた reason


def test_check_old_status_note_says_stale(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)], age_seconds=3600)
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"], status=status)
    assert rc == 0 and "stale" in err


def test_line_empty_workers_prints_nothing(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [])
    rc, out, err = _run(tmp_path, ["routing", "line"], status=status)
    assert (rc, out, err) == (0, "", "")


def test_check_fallback_used_when_primary_stale(tmp_path):
    """check でも --fallback-status が効く (主が古いとき予備で判定)。"""
    primary = tmp_path / "primary.json"
    fallback = tmp_path / "fallback.json"
    _write_status(primary, [_win("glm:5h", "glm", "exhausted", 0.0)], age_seconds=3600)
    _write_status(fallback, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, err = _run(tmp_path, ["routing", "check", "glm",
                                   "--fallback-status", str(fallback)], status=primary)
    assert (rc, out, err) == (0, "", "")  # 予備の spare → boost → 無言


def test_check_fallback_env_stop_exit3(tmp_path):
    primary = tmp_path / "primary.json"
    fallback = tmp_path / "fallback.json"
    _write_status(primary, [_win("glm:5h", "glm", "spare", 70.0)], age_seconds=3600)
    _write_status(fallback, [_win("glm:5h", "glm", "exhausted", 0.0)])
    rc, out, err = _run(tmp_path, ["routing", "check", "glm"], status=primary,
                        env={"QUOTA_STATUS_FALLBACK": str(fallback)})
    assert rc == 3 and "stop" in err


def test_burn_works_when_routing_disabled(tmp_path, burn_path):
    """burn は状態ファイルの管理なので enabled=false でも動く (line / check は黙る)。"""
    cfg = _write_config(tmp_path / "cfg.toml", enabled=False)
    rc, out, _ = _run(tmp_path, ["routing", "burn"], config=cfg)
    assert rc == 0 and "なし" in out
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "on"], config=cfg)
    assert rc == 0
    assert "glm" in json.loads(burn_path.read_text(encoding="utf-8"))
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "off"], config=cfg)
    assert rc == 0
    assert "glm" not in json.loads(burn_path.read_text(encoding="utf-8"))


def test_cli_reuses_persisted_holds_without_advancing_state(tmp_path):
    path = tmp_path / "status.json"
    now = datetime.now().astimezone()
    reset = now + timedelta(hours=2)
    entry = _win("glm:5h", "glm", "over", 50)
    entry.update(length=18000, resets_at=reset.isoformat(),
                 exhausts_at=(reset - timedelta(minutes=25)).isoformat())
    data = _write_status(path, [entry])
    data["routing"] = {"holds": {"glm:5h": entry["resets_at"]}}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_text()
    for _ in range(2):
        rc, out, err = _run(tmp_path, ["routing", "line", "--json"], status=path)
        assert rc == 0 and not err
        assert json.loads(out)["workers"][0]["verdict"] == "reserve"
        rc, out, err = _run(tmp_path, ["routing", "check", "glm", "--json"], status=path)
        assert rc == 0 and not err
        assert json.loads(out)["verdict"] == "reserve"
        assert path.read_text() == before


def test_check_unexpected_exception_is_fail_open(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("problem\nmore")
    monkeypatch.setattr(cli, "_load_gate_status", fail)
    rc, out, err = _run(tmp_path, ["routing", "check", "glm", "--json"])
    assert rc == 0 and out == ""
    assert err.splitlines() == ["routing: problem more"]


def test_burn_rejects_past_iso_and_default_rolls_to_tomorrow(tmp_path, burn_path, monkeypatch):
    now = datetime(2026, 10, 26, 23, 59, 30).astimezone()
    monkeypatch.setattr(gate, "now_local", lambda: now)
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "on", "--until", (now - timedelta(seconds=1)).isoformat(), "--json"])
    assert rc == 2 and json.loads(out)["error"] and not burn_path.exists()
    rc, out, _ = _run(tmp_path, ["routing", "burn", "glm", "on", "--json"])
    assert rc == 0
    assert datetime.fromisoformat(json.loads(out)["burn"][0]["until"]) == (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)


def test_routing_mode_list_settings_and_legacy_burn_exclusivity(tmp_path, burn_path):
    def run(*args):
        rc, out, err = _run(tmp_path, ["routing", *args, "--json"])
        assert not err
        return rc, json.loads(out)

    rc, payload = run("mode")
    assert rc == 0 and {item["name"] for item in payload["modes"]} == set(gate.KNOWN_PROVIDERS)
    assert all(item["mode"] == "standard" and item["until"] is None for item in payload["modes"])
    rc, payload = run("mode", "grok", "off")
    assert rc == 0 and next(x for x in payload["modes"] if x["name"] == "grok")["mode"] == "off"
    rc, payload = run("mode", "grok", "burn", "--until", "23:59")
    entry = next(x for x in payload["modes"] if x["name"] == "grok")
    assert rc == 0 and entry["mode"] == "burn" and entry["until"] and not gate.load_off(gate.OFF_PATH)
    run("mode", "grok", "off")
    assert "grok" not in gate.load_burn(burn_path)
    run("burn", "grok", "on")
    assert "grok" not in gate.load_off(gate.OFF_PATH)
    rc, payload = run("mode", "grok", "standard")
    assert rc == 0 and next(x for x in payload["modes"] if x["name"] == "grok")["mode"] == "standard"
    assert "grok" not in gate.load_burn(burn_path)
    assert run("mode", "invalid", "off")[0] == 2
    assert run("mode", "grok", "off", "--until", "23:59")[0] == 2


def test_monitor_only_check_and_line_with_missing_or_stale_status(tmp_path):
    gate.store_off(gate.OFF_PATH, "grok")
    missing = tmp_path / "missing.json"
    rc, out, err = _run(tmp_path, ["routing", "check", "grok", "--json"], status=missing)
    assert rc == 3 and json.loads(out)["reason"] == "disabled" and not err
    rc, out, err = _run(tmp_path, ["routing", "line"], status=missing)
    assert rc == 0 and out == "quota: stale（status.json なし） grok=stop(?%,監視のみ)\n"
    stale = tmp_path / "stale.json"
    _write_status(stale, [_win("grok:weekly", "grok", "spare", 63)], age_seconds=7200)
    rc, out, err = _run(tmp_path, ["routing", "check", "grok", "--json"], status=stale)
    assert rc == 3 and json.loads(out)["remaining"] == 63 and json.loads(out)["reason"] == "disabled"
    rc, out, err = _run(tmp_path, ["routing", "line", "--json"], status=stale)
    assert json.loads(out)["workers"][0]["reason"] == "disabled"


def test_routing_mode_retired_name_can_be_listed_and_cleared(tmp_path):
    gate.store_off(gate.OFF_PATH, "retired")
    rc, out, _ = _run(tmp_path, ["routing", "mode", "--json"])
    assert any(x["name"] == "retired" and x["mode"] == "off" for x in json.loads(out)["modes"])
    rc, out, _ = _run(tmp_path, ["routing", "mode", "retired", "standard", "--json"])
    assert rc == 0 and "retired" not in gate.load_off(gate.OFF_PATH)


def test_monitor_only_unconfigured_worker_uses_provider_data(tmp_path):
    config = _write_config(tmp_path / "cfg.toml", workers={"glm": ["glm:5h"]})
    gate.store_off(gate.OFF_PATH, "grok")
    path = tmp_path / "status.json"
    window = _win("grok:weekly", "grok", "spare", 63)
    _write_status(path, [window])
    rc, out, _ = _run(tmp_path, ["routing", "check", "grok", "--json"], status=path, config=config)
    worker = json.loads(out)
    assert rc == 3 and (worker["reason"], worker["remaining"], worker["resets_at"], worker["windows"]) == (
        "disabled", 63, window["resets_at"], ["grok:weekly"])


@pytest.mark.parametrize("command", ["mode", "burn"])
def test_burn_transition_keeps_off_when_clear_fails(tmp_path, monkeypatch, command):
    gate.store_off(gate.OFF_PATH, "grok")
    def fail_clear(*args, **kwargs):
        raise OSError("simulated failure")
    monkeypatch.setattr(gate, "clear_off", fail_clear)
    rc, out, err = _run(tmp_path, ["routing", command, "grok", "burn" if command == "mode" else "on"])
    assert rc == 2 and not out and len(err.splitlines()) == 1
    assert "grok" in gate.load_off(gate.OFF_PATH)
    assert "grok" in gate.load_burn(gate.BURN_PATH)


def test_off_transition_keeps_off_when_burn_clear_fails(tmp_path, monkeypatch):
    gate.store_burn(gate.BURN_PATH, "grok", datetime.now().astimezone() + timedelta(hours=1))
    def fail_clear(*args, **kwargs):
        raise OSError("simulated failure")
    monkeypatch.setattr(gate, "clear_burn", fail_clear)
    rc, out, err = _run(tmp_path, ["routing", "mode", "grok", "off"])
    assert rc == 2 and not out and len(err.splitlines()) == 1
    assert "grok" in gate.load_off(gate.OFF_PATH)
    assert "grok" in gate.load_burn(gate.BURN_PATH)


@pytest.mark.parametrize("name", ["openrouter", "deepseek", "kimi"])
def test_balance_only_provider_off_is_excluded(tmp_path, name):
    config = _write_config(tmp_path / "cfg.toml", workers={name: [name + ":daily"]})
    gate.store_off(gate.OFF_PATH, name)
    rc, out, _ = _run(tmp_path, ["routing", "mode", "--json"], config=config)
    assert rc == 0 and all(item["name"] != name for item in json.loads(out)["modes"])
    rc, _, _ = _run(tmp_path, ["routing", "mode", name, "off", "--json"], config=config)
    assert rc == 2
    path = tmp_path / "status.json"
    _write_status(path, [_win(name + ":daily", name, "spare", 63)])
    rc, out, _ = _run(tmp_path, ["routing", "check", name, "--json"], status=path, config=config)
    assert rc == 0 and json.loads(out)["reason"] == "missing"
    rc, out, _ = _run(tmp_path, ["routing", "check", name, "--json"], status=tmp_path / "missing.json", config=config)
    assert rc == 0 and json.loads(out)["reason"] == "missing_status"
    gate.store_burn(gate.BURN_PATH, name, datetime.now().astimezone() + timedelta(hours=1))
    rc, _, _ = _run(tmp_path, ["routing", "burn", name, "off", "--json"], config=config)
    assert rc == 0 and name not in gate.load_burn(gate.BURN_PATH)
    rc, _, _ = _run(tmp_path, ["routing", "mode", name, "standard", "--json"], config=config)
    assert rc == 0 and name not in gate.load_off(gate.OFF_PATH)


def test_mode_excludes_balance_alias_but_includes_mixed_worker(tmp_path):
    config = _write_config(tmp_path / "cfg.toml", workers={"balance_alias": ["grok:weekly"]})
    rc, _, _ = _run(tmp_path, ["routing", "mode", "balance_alias", "off", "--json"], config=config)
    assert rc == 0
    config = _write_config(tmp_path / "cfg.toml", workers={
        "balance_alias": ["openrouter:daily", "kimi:daily"],
        "mixed": ["openrouter:daily", "grok:weekly"],
    })
    gate.store_burn(gate.BURN_PATH, "balance_alias", datetime.now().astimezone() + timedelta(hours=1))
    rc, out, _ = _run(tmp_path, ["routing", "mode", "--json"], config=config)
    assert rc == 0 and {x["name"] for x in json.loads(out)["modes"]} == {"mixed"}
    for mode in ("burn", "off"):
        rc, out, _ = _run(tmp_path, ["routing", "mode", "balance_alias", mode, "--json"], config=config)
        assert rc == 2 and json.loads(out)["error"]
    rc, _, _ = _run(tmp_path, ["routing", "mode", "balance_alias", "standard", "--json"], config=config)
    assert rc == 0 and "balance_alias" not in gate.load_off(gate.OFF_PATH)
    assert "balance_alias" not in gate.load_burn(gate.BURN_PATH)
    rc, out, _ = _run(tmp_path, ["routing", "burn", "balance_alias", "on", "--json"], config=config)
    assert rc == 2 and json.loads(out)["error"]
    gate.store_burn(gate.BURN_PATH, "balance_alias", datetime.now().astimezone() + timedelta(hours=1))
    rc, _, _ = _run(tmp_path, ["routing", "burn", "balance_alias", "off", "--json"], config=config)
    assert rc == 0 and "balance_alias" not in gate.load_burn(gate.BURN_PATH)
    rc, _, _ = _run(tmp_path, ["routing", "mode", "mixed", "off", "--json"], config=config)
    assert rc == 0


@pytest.mark.parametrize("command,action", [("mode", "burn"), ("mode", "off"), ("burn", "on"), ("burn", "off")])
@pytest.mark.parametrize("language", ["ja", "en"])
@pytest.mark.parametrize("as_json", [False, True])
def test_routing_state_write_error_is_reported(tmp_path, monkeypatch, command, action, language, as_json):
    config = _write_config(tmp_path / "cfg.toml")
    config.write_text(config.read_text().replace('language = "ja"', 'language = "%s"' % language))
    def fail(*args, **kwargs):
        raise OSError("write failed\nsecond line")
    monkeypatch.setattr(gate, "_write_burn" if action != "off" or command == "burn" else "_write_off", fail)
    args = ["routing", command, "grok", action] + (["--json"] if as_json else [])
    rc, out, err = _run(tmp_path, args, config=config)
    assert rc == 2
    message = json.loads(out)["error"] if as_json else err.strip()
    assert (not err if as_json else not out) and len(message.splitlines()) == 1
    assert "write failed second line" in message
    assert ("保存できません" if language == "ja" else "Unable to save") in message


@pytest.mark.parametrize("second_command", ["mode", "burn"])
def test_routing_state_transitions_are_serialized(tmp_path, monkeypatch, second_command):
    import threading
    from types import SimpleNamespace
    from quotacanvas.config import Config

    started, release, second_done = threading.Event(), threading.Event(), threading.Event()
    original = gate.store_off
    def pause_store(*args, **kwargs):
        original(*args, **kwargs)
        started.set()
        assert release.wait(5)
    monkeypatch.setattr(gate, "store_off", pause_store)
    monkeypatch.setattr(cli, "_print_json", lambda value: None)
    cfg = Config()
    first = SimpleNamespace(name="grok", mode="off", until=None, json=True)
    second = SimpleNamespace(name="grok", mode="burn", action="on", until=None, json=True)
    results = []
    def invoke(args, operation, done=None):
        try:
            results.append(cli._cmd_routing_state(args, cfg, operation))
        finally:
            if done:
                done.set()
    one = threading.Thread(target=invoke, args=(first, cli._cmd_routing_mode_locked))
    operation = cli._cmd_routing_mode_locked if second_command == "mode" else cli._cmd_routing_burn_locked
    two = threading.Thread(target=invoke, args=(second, operation, second_done))
    one.start()
    try:
        assert started.wait(5)
        two.start()
        assert not second_done.wait(0.2)
    finally:
        release.set()
        one.join(5)
        if two.ident is not None:
            two.join(5)
    assert not one.is_alive() and not two.is_alive() and results == [0, 0]
    assert "grok" in gate.load_burn(gate.BURN_PATH)
    assert "grok" not in gate.load_off(gate.OFF_PATH)
