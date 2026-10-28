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
                           ("invoke", "a" * 161), ("note", "a" * 121),
                           ("invoke", "bad\nline"), ("note", "tab\t"),
                           ("note", "control\x85"), ("note", "line\u2028break")]],
])
def test_invalid_models_raise_config_error(models):
    with pytest.raises(config.ConfigError):
        config.config_from_dict({"routing": {"models": models}})


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
