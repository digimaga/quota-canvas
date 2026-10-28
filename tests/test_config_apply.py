import copy
import json
import subprocess
import sys

import pytest

from quotacanvas.config import (ConfigError, apply_config_changes, config_from_dict, config_to_dict,
                                load_config, merge_config_changes, save_config)


def test_merge_add_remove_null_lists_and_unchanged_concurrent():
    old = {'section': {'keep': 1, 'delete': 3, 'null': 1, 'array': [1, 2]}}
    edited = {'section': {'keep': 1, 'null': None, 'array': [2, 1]}}
    latest = {'section': {'keep': 9, 'delete': 3, 'null': 1, 'array': [1, 2], 'added': 4}}
    assert merge_config_changes(old, edited, latest) == {'section': {'keep': 9, 'null': None, 'array': [2, 1], 'added': 4}}
    assert latest['section']['delete'] == 3


def test_apply_preserves_known_concurrent_settings_and_worker_order(tmp_path):
    path = tmp_path / 'config.toml'
    original = config_to_dict(load_config(path))
    latest = copy.deepcopy(original)
    latest['display']['paused'] = True
    save_config(config_from_dict(latest), path)
    edited = copy.deepcopy(original)
    edited['display']['brightness'] = 25
    edited['routing']['workers'] = [{'name': 'b', 'windows': ['glm:5h']}, {'name': 'a', 'windows': ['codex:weekly']}]
    edited['routing'].pop('worker_order')
    result = config_to_dict(apply_config_changes(original, edited, path))
    assert result['display']['paused'] is True
    assert result['display']['brightness'] == 25
    assert result['routing']['worker_order'] == ['b', 'a']
    assert config_to_dict(load_config(path)) == result


def test_invalid_apply_does_not_overwrite(tmp_path):
    path = tmp_path / 'config.toml'
    original = config_to_dict(load_config(path))
    save_config(load_config(path), path)
    before = path.read_bytes()
    edited = copy.deepcopy(original)
    edited['display']['brightness'] = 1000
    with pytest.raises(ConfigError):
        apply_config_changes(original, edited, path)
    assert path.read_bytes() == before


def test_parallel_cli_edits_are_merged(tmp_path):
    path = tmp_path / 'config.toml'
    original = config_to_dict(load_config(path))
    commands = []
    for key, value in [('paused', True), ('brightness', 17)]:
        edited = copy.deepcopy(original)
        edited['display'][key] = value
        commands.append(subprocess.Popen([sys.executable, '-m', 'quotacanvas.cli', '--config', str(path), 'config', '--apply-json'],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        commands[-1].stdin.write(json.dumps({'original': original, 'edited': edited}))
        commands[-1].stdin.close()
    for process in commands:
        process.wait()
        assert process.returncode == 0, process.stderr.read()
    current = load_config(path)
    assert current.display.paused is True and current.display.brightness == 17


def test_apply_cli_invalid_input_preserves_config(monkeypatch, capsys, tmp_path):
    import io
    from quotacanvas import cli
    path = tmp_path / "config.toml"
    path.write_text("[display]\nbrightness = 50\n", encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr("sys.stdin", io.StringIO('{"original": {}, "edited": {"display": {"brightness": 101}}}'))
    assert cli.main(["--config", str(path), "config", "--apply-json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert path.read_bytes() == before


def test_apply_cli_deletes_optional_value_and_preserves_null(tmp_path):
    path = tmp_path / 'config.toml'
    initial = config_from_dict({'providers': {'glm': {'color': '#123456'}},
                                'routing': {'short_over_margin_minutes': 5}})
    save_config(initial, path)
    original = config_to_dict(initial)
    edited = copy.deepcopy(original)
    del edited['providers']['glm']['color']
    edited['routing']['short_over_margin_minutes'] = None
    result = config_to_dict(apply_config_changes(original, edited, path))
    assert 'color' not in result['providers']['glm']
    assert result['routing']['short_over_margin_minutes'] is None


def test_new_object_keys_follow_edited_order():
    merged = merge_config_changes({}, {'routing': {'workers': {'z': [], 'a': []}}}, {})
    assert list(merged['routing']['workers']) == ['z', 'a']
