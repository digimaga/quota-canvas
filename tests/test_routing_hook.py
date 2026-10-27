"""`quota-canvas routing hook` のテスト: プラグイン (SessionStart フック) 用の
additionalContext を 1 行の JSON で出すコマンド。

- enabled=false・ワーカー 0 件・例外・設定ファイルが壊れているときは無出力で
  常に exit 0 (fail-open)
- enabled のときは 1 行の JSON (hookSpecificOutput.hookEventName / additionalContext)
- --event で hookEventName を変えられる
- status.json が無い・古いときは routing line と同じ stale の 1 行だけ入れる
  (パスやルールは入れない)
- ワーカー名は status.json 由来でも安全な形 (gate.WORKER_NAME_RE) だけ行に載せる
- ルール文言 (routing.hook.rules) は表示言語 (ja/en) に従い、禁止文を保つ
- plugins/.../scripts/session-start.sh はバイナリが無ければ無言、偽バイナリの
  stderr / 終了コードは飲み、stdout の JSON はそのまま通す

routing hook も line / check と同じく絶対にサービスへ取得しない (test_routing_cli
の autouse fixture を借りて保証する)。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quotacanvas import gate
from test_routing_cli import (  # noqa: F401  (fixtures / helpers の再利用)
    _no_fetch, _restore_language, _run, _win, _write_config, _write_status, burn_path,
)

SCRIPT = Path(__file__).resolve().parents[1] / "plugins" / "quota-canvas-routing" \
    / "scripts" / "session-start.sh"


def _hook(tmp_path, argv=(), **kwargs):
    return _run(tmp_path, ["routing", "hook", *argv], **kwargs)


def _en_config(path: Path) -> Path:
    path.write_text(
        '[display]\nlanguage = "en"\n\n[poll]\ninterval = 300\n\n[routing]\nenabled = true\n',
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------------------ 無出力系


def test_hook_disabled_is_silent_and_exit0(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    cfg = _write_config(tmp_path / "cfg.toml", enabled=False)
    rc, out, err = _hook(tmp_path, status=status, config=cfg)
    assert (rc, out, err) == (0, "", "")


def test_hook_no_workers_is_silent(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [])
    rc, out, err = _hook(tmp_path, status=status)
    assert (rc, out, err) == (0, "", "")


def test_hook_exception_is_silent_exit0(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])

    def _boom(*args, **kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(gate, "evaluate", _boom)
    rc, out, err = _hook(tmp_path, status=status)
    assert (rc, out, err) == (0, "", "")


def test_hook_never_fetches_even_without_status(tmp_path):
    # _no_fetch fixture が fetch_all を例外化済み。取得なしで完走する。
    rc, out, _ = _hook(tmp_path, status=tmp_path / "none.json")
    assert rc == 0


# ------------------------------------------------------------------ JSON の形


def test_hook_prints_one_json_line_session_start(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, err = _hook(tmp_path, status=status)
    assert rc == 0 and err == ""
    assert out.count("\n") == 1
    inner = json.loads(out)["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    ctx = inner["additionalContext"]
    assert ctx.startswith("quota: glm=boost(")
    # 判定の 1 行のあとに ja のルールが続く
    assert "上の quota 行は" in ctx
    assert ctx.index("quota: glm=boost(") < ctx.index("上の quota 行は")
    assert "quota-canvas routing line" in ctx


def test_hook_event_option_sets_hook_event_name(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, _ = _hook(tmp_path, ["--event", "CustomStart"], status=status)
    assert rc == 0
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "CustomStart"


def test_hook_env_status_path_is_honored(tmp_path, monkeypatch):
    status = tmp_path / "env-status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    monkeypatch.setenv("QUOTA_STATUS_PATH", str(status))
    rc, out, _ = _hook(tmp_path)
    assert rc == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] \
        .startswith("quota: glm=boost(")


def test_hook_fallback_used_when_primary_missing(tmp_path):
    fallback = tmp_path / "fallback.json"
    _write_status(fallback, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, _ = _hook(tmp_path, ["--fallback-status", str(fallback)],
                       status=tmp_path / "none.json")
    assert rc == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] \
        .startswith("quota: glm=boost(")


# ------------------------------------------------------------------ stale


def test_hook_missing_status_puts_stale_line(tmp_path):
    rc, out, err = _hook(tmp_path, status=tmp_path / "none.json")
    assert rc == 0 and err == ""
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "stale" in ctx and "quota:" in ctx


def test_hook_old_status_puts_stale_line(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)], age_seconds=3600)
    rc, out, _ = _hook(tmp_path, status=status)
    assert rc == 0
    assert "stale" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_hook_broken_status_puts_stale_line(tmp_path):
    status = tmp_path / "status.json"
    status.write_text("{ broken", encoding="utf-8")
    rc, out, _ = _hook(tmp_path, status=status)
    assert rc == 0
    assert "stale" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


# ------------------------------------------------------------------ 文言 ja/en


def test_hook_rules_follow_language(tmp_path):
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, _ = _hook(tmp_path, status=status)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "boost（余っている）" in ctx and "stop（使い切り）" in ctx
    rc, out, _ = _hook(tmp_path, status=status,
                       config=_en_config(tmp_path / "cfg-en.toml"))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "boost (headroom)" in ctx and "stop (used up)" in ctx
    # en の行サマリも en の書式 (as of ...)
    assert ctx.startswith("quota: glm=boost(")


def test_hook_rules_keep_guardrails(tmp_path):
    """禁止文が残っていること: このセッションは止めない / ユーザー指定に従う /
    burn・QUOTA_GATE_FORCE・--force・設定の変更はユーザーに言われたときだけ。"""
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0)])
    rc, out, _ = _hook(tmp_path, status=status)
    ja = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    for phrase in ("このセッション自身の作業は止めない",
                   "ユーザーが名指ししたワーカーにはそのまま従う",
                   "burn", "QUOTA_GATE_FORCE", "--force", "設定の変更"):
        assert phrase in ja, phrase
    rc, out, _ = _hook(tmp_path, status=status,
                       config=_en_config(tmp_path / "cfg-en.toml"))
    en = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    for phrase in ("never stops this session's own work",
                   "If the user names a worker, follow them",
                   "Burn mode", "QUOTA_GATE_FORCE", "--force", "config changes"):
        assert phrase in en, phrase


# ------------------------------------------------- ワーカー名の安全化 (point 1)


@pytest.mark.parametrize("bad", ['evil" ; rm', "two\nlines", "has space", "x" * 41])
def test_hook_drops_unsafe_provider_names_from_status(tmp_path, bad):
    """既定ワーカーの名前は status.json の windows[].provider の生の文字列。
    安全な形以外の名前は行に載せない (additionalContext に入らない)。"""
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0),
                           _win("bad:5h", bad, "exhausted", 0.0)])
    rc, out, err = _hook(tmp_path, status=status)
    assert rc == 0 and err == ""
    assert out.count("\n") == 1  # JSON は壊れない
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert bad not in ctx
    assert "evil" not in ctx  # 断片も載らない
    assert "glm=boost(" in ctx  # 正常なワーカーはそのまま


def test_hook_drops_only_unsafe_names_in_line(tmp_path):
    """line コマンドも同じフィルタ (format_line 共通)。"""
    status = tmp_path / "status.json"
    _write_status(status, [_win("glm:5h", "glm", "spare", 70.0),
                           _win("bad:5h", "bad name", "exhausted", 0.0)])
    rc, out, err = _run(tmp_path, ["routing", "line"], status=status)
    assert rc == 0 and err == ""
    assert "bad name" not in out and "glm=boost(" in out


# --------------------------------------------------------- 壊れた設定 (point 3)


def test_hook_broken_config_is_silent_exit0(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[routing]\nenabled = ===\n", encoding="utf-8")
    rc, out, err = _hook(tmp_path, config=cfg, status=tmp_path / "none.json")
    assert (rc, out, err) == (0, "", "")


def test_hook_invalid_config_is_silent_exit0(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[routing]\nenabled = "yes"\n', encoding="utf-8")  # 真偽値以外
    rc, out, err = _hook(tmp_path, config=cfg, status=tmp_path / "none.json")
    assert (rc, out, err) == (0, "", "")


# --------------------------------------------------------------- session-start.sh


def _run_sh(env_extra: dict) -> subprocess.CompletedProcess:
    import os

    env = {"HOME": "/tmp", "PATH": "/usr/bin:/bin"}
    env.update(env_extra)
    return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True,
                          text=True, timeout=30)


def _fake_bin(tmp_path: Path, body: str) -> str:
    fake = tmp_path / "fake-quota-canvas"
    fake.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    fake.chmod(0o755)
    return str(fake)


def test_script_missing_binary_silent_exit0(tmp_path):
    p = _run_sh({"HOME": str(tmp_path), "QUOTA_CANVAS_BIN": "/nonexistent"})
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""


def test_script_noisy_failing_bin_is_swallowed(tmp_path):
    """stderr に書いて exit 2 する偽バイナリ: stdout は空、exit 0。"""
    bin_path = _fake_bin(tmp_path, 'echo "boom" >&2\nexit 2\n')
    p = _run_sh({"HOME": str(tmp_path), "QUOTA_CANVAS_BIN": bin_path})
    assert p.returncode == 0 and p.stdout == ""


def test_script_fake_json_passes_through(tmp_path):
    payload = ('{"hookSpecificOutput": {"hookEventName": "SessionStart", '
               '"additionalContext": "quota: x=go"}}')
    bin_path = _fake_bin(tmp_path, "echo '%s'\n" % payload)
    p = _run_sh({"HOME": str(tmp_path), "QUOTA_CANVAS_BIN": bin_path})
    assert p.returncode == 0 and p.stdout == payload + "\n"
