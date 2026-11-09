import copy
import json
import os
import subprocess
import sys
import time

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
    sync = tmp_path / 'sync'
    sync.mkdir()
    original = config_to_dict(load_config(path))
    # 各子プロセスで config_lock と load_config をラップする。
    #  - apply が latest を読んだ直後に ready-<pid> を立て、go-<pid> が
    #    来るまで書き込み前に止まる (gate_load)。
    #  - ロック取得前に別 fd で LOCK_NB を試し、取れなければ
    #    blocked-<pid> を立てる (gate_lock)。本物の config_lock 自体は
    #    そのまま使うので flock が無ければ直列化も無い。
    # 親は 1 本目が ready になってから 2 本目を起動する。flock が効いて
    # いれば 2 本目は必ず blocked になり、flock が無ければ両者が同じ
    # latest を読んでから書き込むので必ず片方の編集が消える。
    preamble = (
        "import os, sys, time\n"
        "from contextlib import contextmanager\n"
        "from pathlib import Path\n"
        "import quotacanvas.config as _c\n"
        "sync = Path(os.environ['QCTEST_SYNC'])\n"
        "cfg_target = Path(os.environ['QCTEST_CONFIG'])\n"
        "pid = str(os.getpid())\n"
        "real_lock, real_load = _c.config_lock, _c.load_config\n"
        "lock_path = cfg_target.with_name('.' + cfg_target.name + '.lock')\n"
        "gated = [False]\n"
        "def gate_load(path):\n"
        "    result = real_load(path)\n"
        "    if not gated[0] and Path(path) == cfg_target:\n"
        "        gated[0] = True\n"
        "        (sync / ('ready-' + pid)).touch()\n"
        "        end = time.monotonic() + 30\n"
        "        while not (sync / ('go-' + pid)).exists():\n"
        "            if time.monotonic() > end:\n"
        "                raise TimeoutError('go marker did not appear')\n"
        "            time.sleep(0.005)\n"
        "    return result\n"
        "def probe_free():\n"
        "    lock_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    with lock_path.open('a+b') as fh:\n"
        "        fh.seek(0)\n"
        "        if os.name == 'nt':\n"
        "            import msvcrt\n"
        "            try:\n"
        "                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)\n"
        "            except OSError:\n"
        "                return False\n"
        "            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)\n"
        "            return True\n"
        "        import fcntl\n"
        "        try:\n"
        "            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "        except OSError:\n"
        "            return False\n"
        "        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)\n"
        "        return True\n"
        "@contextmanager\n"
        "def gate_lock(target):\n"
        "    if not probe_free():\n"
        "        (sync / ('blocked-' + pid)).touch()\n"
        "    with real_lock(target):\n"
        "        yield\n"
        "_c.config_lock, _c.load_config = gate_lock, gate_load\n"
        "from quotacanvas.cli import main\n"
        "sys.exit(main())\n"
    )
    env = dict(os.environ, QCTEST_SYNC=str(sync), QCTEST_CONFIG=str(path))
    children = []

    def spawn(key, value):
        edited = copy.deepcopy(original)
        edited['display'][key] = value
        proc = subprocess.Popen([sys.executable, '-c', preamble,
                                 '--config', str(path), 'config', '--apply-json'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        proc.stdin.write(json.dumps({'original': original, 'edited': edited}))
        proc.stdin.close()
        children.append(proc)

    def markers(kind):
        return {p.name[len(kind) + 1:] for p in sync.glob(kind + '-*')}

    def wait_until(pred, what):
        end = time.monotonic() + 30
        while not pred():
            assert time.monotonic() < end, "timeout waiting for " + what
            time.sleep(0.005)

    spawn('paused', True)
    wait_until(lambda: markers('ready'), 'first child to finish its read')
    first = markers('ready')
    spawn('brightness', 17)
    # 2 本目がロックで待つ (blocked) か、そのまま読了する (ready) かを待つ。
    wait_until(lambda: len(markers('ready')) == 2 or markers('blocked') - first,
               'second child to become ready or blocked')
    if len(markers('ready')) == 2:
        for pid in markers('ready'):
            (sync / ('go-' + pid)).touch()
    else:
        for pid in first:
            (sync / ('go-' + pid)).touch()
        wait_until(lambda: len(markers('ready')) == 2, 'second child to finish its read')
        for pid in markers('ready') - first:
            (sync / ('go-' + pid)).touch()
    for process in children:
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
