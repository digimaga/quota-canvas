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
