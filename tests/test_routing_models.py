"""Model ledger configuration, verdicts, CLI, hook and status serialization."""
import json
from datetime import datetime

import pytest

from quotacanvas import cli, config, gate, i18n, status
from test_routing_cli import _run, _win, _write_config, _write_status, burn_path, _restore_language, _no_fetch  # noqa: F401


def _models_config(tmp_path, entries, *, enabled=True, workers=None):
    path = _write_config(tmp_path / "cfg.toml", enabled=enabled, workers=workers)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n" + entries)
    return path


def test_load_roundtrip_and_json_order(tmp_path):
    path = _models_config(tmp_path, '''
[routing.models.zed]
workers = ["codex", "unconfigured"]
roles = ["impl", "consult"]
edit = true
invoke = "example --model zed"
note = "特徴"
[routing.models.alpha]
workers = ["glm"]
roles = ["review"]
''')
    cfg = config.load_config(path)
    assert list(cfg.routing.models) == ["zed", "alpha"]
    assert cfg.routing.models["zed"] == config.RoutingModel(["codex", "unconfigured"], ["impl", "consult"], "example --model zed", True, "特徴")
    assert cfg.routing.models["alpha"].edit is False
    data = config.config_to_dict(cfg)
    data = json.loads(json.dumps(data, sort_keys=True))  # Swift JSONSerialization may reorder objects
    restored = config.config_from_dict(data)
    path.write_text(config.format_toml(restored), encoding="utf-8")
    assert config.load_config(path).routing.models == cfg.routing.models
    assert list(config.load_config(path).routing.models) == ["zed", "alpha"]


@pytest.mark.parametrize("models", [
    [], {"bad name": {"workers": ["codex"], "roles": ["impl"]}},
    {"a": 1}, {"a": {"workers": ["codex"], "roles": ["impl"], "extra": 1}},
    {"a": {"roles": ["impl"]}}, {"a": {"workers": [], "roles": ["impl"]}},
    {"a": {"workers": ["bad name"], "roles": ["impl"]}},
    {"a": {"workers": [1], "roles": ["impl"]}},
    {"a": {"workers": "codex", "roles": ["impl"]}},
    {"a": {"workers": ["codex"]}}, {"a": {"workers": ["codex"], "roles": []}},
    {"a": {"workers": ["codex"], "roles": ["invalid"]}},
    {"a": {"workers": ["codex"], "roles": [1]}},
    {"a": {"workers": ["codex"], "roles": "review"}},
    *[{"a": {"workers": ["codex"], "roles": ["impl"], field: value}}
      for field, value in [("edit", 1), ("invoke", 1), ("note", False),
                           ("enabled", 1), ("enabled", "false"),
                           ("invoke", "a" * 161), ("note", "a" * 121),
                           ("invoke", "bad\nline"), ("note", "tab\t"),
                           ("note", "control\x85"), ("note", "line\u2028break")]],
])
def test_invalid_models_raise_config_error(models):
    with pytest.raises(config.ConfigError):
        config.config_from_dict({"routing": {"models": models}})


def test_enabled_defaults_true_and_roundtrips(tmp_path):
    """enabled は TOML・config JSON・保存の往復で保たれ、省略時は true。"""
    path = _models_config(tmp_path, '''
[routing.models.on]
workers = ["codex"]
roles = ["impl"]
[routing.models.off]
workers = ["codex"]
roles = ["review"]
enabled = false
''')
    cfg = config.load_config(path)
    assert cfg.routing.models["on"].enabled is True
    assert cfg.routing.models["off"].enabled is False
    data = config.config_to_dict(cfg)
    models = data["routing"]["models"]
    assert models["on"]["enabled"] is True
    assert models["off"]["enabled"] is False
    # アプリ (JSONSerialization) がオブジェクトを並べ替えても往復する
    restored = config.config_from_dict(json.loads(json.dumps(data, sort_keys=True)))
    assert restored.routing.models == cfg.routing.models
    toml = config.format_toml(restored)
    off_block = toml.split('[routing.models."off"]', 1)[1].split("\n[", 1)[0]
    assert "enabled = false" in off_block
    on_block = toml.split('[routing.models."on"]', 1)[1].split("\n[", 1)[0]
    assert "enabled" not in on_block  # 既定 true は省略する
    path.write_text(toml, encoding="utf-8")
    again = config.load_config(path)
    assert again.routing.models["off"].enabled is False
    assert again.routing.models["on"].enabled is True


def test_text_limit_boundaries():
    cfg = config.config_from_dict({"routing": {"models": {"m": {
        "workers": ["unknown"], "roles": ["review"], "invoke": "i" * 160, "note": "n" * 120}}}})
    assert len(cfg.routing.models["m"].invoke) == 160


@pytest.mark.parametrize("verdicts,expected", [
    (["boost", "go", "reserve"], "reserve"), (["boost", "go"], "go"),
    (["boost", "unknown"], "unknown"), (["stop", "unknown"], "stop"),
])
def test_model_verdict(verdicts, expected):
    model = config.RoutingModel([str(i) for i in range(len(verdicts))], ["review"])
    result = gate.RoutingResult(workers=[gate.WorkerResult(str(i), v, "test") for i, v in enumerate(verdicts)])
    assert gate.model_verdict("model", model, result).verdict == expected
    model.workers.append("missing")
    assert gate.model_verdict("model", model, result).verdict == ("stop" if expected == "stop" else "unknown")


def test_model_verdict_stale_worker_is_unknown_but_stop_wins():
    """stale ワーカーは unknown と同じく判定不能に倒す (`or w.stale`)。
    確定した stop だけは stale より優先される。"""
    model = config.RoutingModel(["a", "b"], ["review"])
    result = gate.RoutingResult(workers=[
        gate.WorkerResult("a", "boost", "test", stale=True),
        gate.WorkerResult("b", "go", "test"),
    ])
    assert gate.model_verdict("m", model, result).verdict == "unknown"
    result.workers.insert(0, gate.WorkerResult("s", "stop", "test"))
    model.workers.append("s")
    assert gate.model_verdict("m", model, result).verdict == "stop"


def test_line_hook_check_and_stale(tmp_path):
    path = tmp_path / "status.json"
    _write_status(path, [_win("codex:weekly", "codex", "spare", 70), _win("glm:weekly", "glm", "exhausted", 0)])
    baseline = _write_config(tmp_path / "baseline.toml")
    _, first, _ = _run(tmp_path, ["routing", "line"], status=path, config=baseline)
    cfg = _models_config(tmp_path, '''
[routing.models.terra]
workers = ["codex"]
roles = ["impl"]
edit = true
invoke = "run terra"
note = "fast"
[routing.models.astra]
workers = ["codex", "missing"]
roles = ["review"]
[routing.models.stopped]
workers = ["glm"]
roles = ["review"]
''')
    rc, out, err = _run(tmp_path, ["routing", "line"], status=path, config=cfg)
    assert rc == 0 and not err and out.splitlines()[0] == first.rstrip()
    assert "- terra: boost[codex=boost] 編集可 impl | run terra | fast" in out
    assert "- astra: ?[codex=boost, missing=?] 読み取り review" in out
    assert "review: 使える 1（astra）。このモデルが実装した差分はレビュー役がいない" in out
    _, hook, _ = _run(tmp_path, ["routing", "hook"], status=path, config=cfg)
    assert json.loads(hook)["hookSpecificOutput"]["additionalContext"].startswith(out.rstrip())
    rc, out, _ = _run(tmp_path, ["routing", "check", "stopped", "--json"], status=path, config=cfg)
    assert rc == 3 and json.loads(out)["verdict"] == "stop"
    rc, out, _ = _run(tmp_path, ["routing", "check", "astra", "--json"], status=path, config=cfg)
    assert rc == 0 and json.loads(out)["verdict"] == "unknown"
    rc, out, _ = _run(tmp_path, ["routing", "line"], status=tmp_path / "missing.json", config=cfg)
    assert "astra: ?[codex=?, missing=?]" in out and "review: 使える 2" in out
    _, hook, _ = _run(tmp_path, ["routing", "hook"], status=tmp_path / "missing.json", config=cfg)
    assert json.loads(hook)["hookSpecificOutput"]["additionalContext"] == out.rstrip()


def test_disabled_model_is_hidden_from_ledger_and_review(tmp_path):
    """enabled = false のモデルは models: 行にも review 候補にも出さない。"""
    path = tmp_path / "status.json"
    _write_status(path, [_win("codex:weekly", "codex", "spare", 70)])
    cfg = _models_config(tmp_path, '''
[routing.models.active]
workers = ["codex"]
roles = ["review"]
[routing.models.hidden]
workers = ["codex"]
roles = ["review"]
enabled = false
''')
    _, out, _ = _run(tmp_path, ["routing", "line"], status=path, config=cfg)
    assert "- active:" in out
    assert "hidden" not in out
    assert "review: 使える 1（active）" in out
    _, hook, _ = _run(tmp_path, ["routing", "hook"], status=path, config=cfg)
    assert "hidden" not in hook
    assert "models:" in json.loads(hook)["hookSpecificOutput"]["additionalContext"]
    # status.json の model_lines (format_model_lines 経由) からも消える
    payload = status.build_status(config.load_config(cfg), [], [], datetime.now().astimezone())
    assert all("hidden" not in line for line in payload["routing"]["model_lines"])

    (tmp_path / "all").mkdir()
    cfg_all = _models_config(tmp_path / "all", '''
[routing.models.hidden]
workers = ["codex"]
roles = ["review"]
enabled = false
''')
    status_all = tmp_path / "all" / "status.json"
    _write_status(status_all, [_win("codex:weekly", "codex", "spare", 70)])
    _, out, _ = _run(tmp_path, ["routing", "line"], status=status_all, config=cfg_all)
    assert out.strip() and "models:" not in out


def test_disabled_model_check_exits_3_without_judging(tmp_path):
    path = tmp_path / "status.json"
    _write_status(path, [_win("codex:weekly", "codex", "spare", 70)])
    cfg = _models_config(tmp_path, '''
[routing.models.active]
workers = ["codex"]
roles = ["impl"]
[routing.models.hidden]
workers = ["codex"]
roles = ["review"]
enabled = false
''')
    rc, out, err = _run(tmp_path, ["routing", "check", "hidden"], status=path, config=cfg)
    assert rc == 3 and out == ""
    assert err.count("\n") == 1 and "hidden" in err and "使わない" in err
    assert _run(tmp_path, ["routing", "check", "hidden"], status=path, config=cfg,
                env={"QUOTA_GATE_FORCE": "1"}) == (0, "", "")
    # 既定 true は従来どおり判定する (codex は boost なので無言 exit 0)
    assert _run(tmp_path, ["routing", "check", "active"], status=path, config=cfg) == (0, "", "")


def test_disabled_model_check_json_prints_stop_verdict(tmp_path):
    path = tmp_path / "status.json"
    _write_status(path, [_win("codex:weekly", "codex", "spare", 70)])
    cfg = _models_config(tmp_path, '''
[routing.models.hidden]
workers = ["codex"]
roles = ["review"]
enabled = false
''')
    rc, out, err = _run(tmp_path, ["routing", "check", "hidden", "--json"], status=path, config=cfg)
    assert rc == 3 and err == ""
    data = json.loads(out)
    assert data["name"] == "hidden" and data["verdict"] == "stop" and data["reason"] == "model_disabled"


def test_disabled_model_named_like_worker_or_provider_still_judges(tmp_path):
    path = tmp_path / "status.json"
    _write_status(path, [_win("codex:weekly", "codex", "spare", 70)])
    cfg = _models_config(tmp_path, '''
[routing.models.codex]
workers = ["codex"]
roles = ["review"]
enabled = false
''')
    # KNOWN_PROVIDERS の codex と衝突 → ワーカー判定 (boost) が走り無言 exit 0
    assert _run(tmp_path, ["routing", "check", "codex"], status=path, config=cfg) == (0, "", "")


def test_all_models_disabled_gives_empty_model_lines():
    result = gate.RoutingResult(workers=[gate.WorkerResult("codex", "boost", "spare")])
    models = {"a": config.RoutingModel(["codex"], ["impl"], enabled=False)}
    assert gate.format_model_lines(models, result, "ja") == []
    models["b"] = config.RoutingModel(["codex"], ["impl"])
    lines = gate.format_model_lines(models, result, "ja")
    assert any(line.startswith("- b") for line in lines) and not any(line.startswith("- a") for line in lines)


def test_zero_review_and_worker_name_priority(tmp_path):
    path = tmp_path / "status.json"
    _write_status(path, [_win("codex:weekly", "codex", "spare", 70), _win("glm:weekly", "glm", "exhausted", 0)])
    cfg = _models_config(tmp_path, '''
[routing.models.codex]
workers = ["glm"]
roles = ["review"]
''')
    _, out, _ = _run(tmp_path, ["routing", "line"], status=path, config=cfg)
    assert "review: 警告 レビューに使えるモデルがいない（全員 stop）" in out
    rc, out, _ = _run(tmp_path, ["routing", "check", "codex", "--json"], status=path, config=cfg)
    assert rc == 0 and json.loads(out)["verdict"] == "boost"


def test_status_model_lines_and_disabled(tmp_path):
    cfg_path = _models_config(tmp_path, '''
[routing.models.a]
workers = ["missing"]
roles = ["review"]
''')
    cfg = config.load_config(cfg_path)
    i18n.set_language("ja")
    now = datetime.now().astimezone()
    payload = status.build_status(cfg, [], [], now)
    routing = payload["routing"]
    assert "\n" not in routing["line"]
    assert routing["model_lines"] == gate.format_model_lines(cfg.routing.models, gate.evaluate(payload, cfg.routing, {}, now), "ja")
    path = tmp_path / "output.json"
    status.write_status(payload, path)
    assert json.loads(path.read_text())["routing"]["model_lines"] == routing["model_lines"]
    cfg.routing.enabled = False
    assert "routing" not in status.build_status(cfg, [], [], now)
    cfg_path.write_text(cfg_path.read_text().replace("enabled = true", "enabled = false"), encoding="utf-8")
    for action in ("line", "hook", "check"):
        args = ["routing", action] + (["a"] if action == "check" else [])
        assert _run(tmp_path, args, config=cfg_path) == (0, "", "")


def test_status_matches_cli_model_lines(tmp_path):
    from datetime import timedelta
    from quotacanvas.models import ProviderUsage, Window
    from quotacanvas.pacing import compute_pace

    cfg_path = _models_config(tmp_path, '''
[routing.models.example]
workers = ["codex"]
roles = ["review"]
''')
    cfg = config.load_config(cfg_path)
    i18n.set_language("ja")
    now = datetime.now().astimezone()
    window = Window("codex", "weekly", "Weekly", 10.0, now + timedelta(days=3), timedelta(days=7))
    usage = ProviderUsage("codex", "Codex", [window], None, now)
    payload = status.build_status(cfg, [(usage, compute_pace(window, [], now))], [usage], now)
    path = tmp_path / "status.json"
    status.write_status(payload, path)
    _, out, _ = _run(tmp_path, ["routing", "line"], status=path, config=cfg_path)
    assert out.splitlines() == [payload["routing"]["line"], *payload["routing"]["model_lines"]]


def test_stale_status_preserves_monitor_only_stop(tmp_path):
    path = tmp_path / "status.json"
    _write_status(path, [_win("codex:weekly", "codex", "spare", 70)], age_seconds=3600)
    cfg = _models_config(tmp_path, '''
[routing.models.reviewer]
workers = ["codex", "missing"]
roles = ["review"]
''')
    gate.OFF_PATH.write_text(json.dumps(["codex"]), encoding="utf-8")
    rc, out, _ = _run(tmp_path, ["routing", "check", "reviewer", "--json"], status=path, config=cfg)
    assert rc == 3 and json.loads(out)["reason"] == "disabled"
    _, out, _ = _run(tmp_path, ["routing", "line"], status=path, config=cfg)
    assert "reviewer: stop[codex=stop, missing=?]" in out
    assert "review: 警告" in out


def test_english_ledger(tmp_path):
    cfg = _models_config(tmp_path, '''
[routing.models.reviewer]
workers = ["missing"]
roles = ["review"]
''')
    cfg.write_text(cfg.read_text().replace('language = "ja"', 'language = "en"'), encoding="utf-8")
    _, out, _ = _run(tmp_path, ["routing", "line"], status=tmp_path / "missing.json", config=cfg)
    assert "read-only review" in out and "review: 1 available (reviewer)" in out


def test_reject_names_with_trailing_newline():
    assert not gate.safe_worker_name("codex\n")
    for routing in (
        {"models": {"m\n": {"workers": ["codex"], "roles": ["impl"]}}},
        {"models": {"m": {"workers": ["codex\n"], "roles": ["impl"]}}},
        {"workers": {"codex\n": ["codex:weekly"]}},
        {"workers": [{"name": "codex\n", "windows": ["codex:weekly"]}]},
    ):
        with pytest.raises(config.ConfigError):
            config.config_from_dict({"routing": routing})


@pytest.mark.parametrize("command", ["line", "hook"])
def test_model_ledger_is_silent_without_first_line(tmp_path, command):
    path = tmp_path / "status.json"
    _write_status(path, [])
    cfg = _models_config(tmp_path, '''
[routing.models.reviewer]
workers = ["missing"]
roles = ["review"]
''')
    assert _run(tmp_path, ["routing", command], status=path, config=cfg) == (0, "", "")


@pytest.mark.parametrize("command", ["line", "hook"])
def test_stale_ledger_is_silent_without_first_line(tmp_path, monkeypatch, command):
    cfg = _models_config(tmp_path, '''
[routing.models.reviewer]
workers = ["missing"]
roles = ["review"]
''')
    monkeypatch.setattr(cli, "_stale_line_with_disabled", lambda *args: "")
    assert _run(tmp_path, ["routing", command], status=tmp_path / "missing.json", config=cfg) == (0, "", "")


def test_model_with_impl_and_review_roles_is_a_review_candidate(tmp_path):
    """roles に impl と review を両方書いたモデルは、実装にもレビューにも使える（メニューバーアプリの役割チェックの既定）。"""
    path = tmp_path / "status.json"
    _write_status(path, [_win("codex:weekly", "codex", "spare", 70)])
    cfg = _models_config(tmp_path, '''
[routing.models.both]
workers = ["codex"]
roles = ["impl", "review"]
[routing.models.impl_only]
workers = ["codex"]
roles = ["impl"]
''')
    _, out, _ = _run(tmp_path, ["routing", "line"], status=path, config=cfg)
    assert "- both:" in out and "- impl_only:" in out
    assert "review: 使える 1（both）" in out
