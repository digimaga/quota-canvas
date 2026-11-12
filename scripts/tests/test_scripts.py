#!/usr/bin/env python3
"""Isolated regression checks; never contact launchd or a remote repository."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / 'repo & <sample>'
        shutil.copytree(ROOT / 'scripts', self.repo / 'scripts')
        self.home = base / 'home & <sample>'
        self.home.mkdir()
        self.bin = base / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), TEST_DIR=str(base),
                        PATH=f'{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin')
        self.stub('launchctl', '''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_DIR/launchctl.log"
if [[ "$1" == print ]]; then exit 0; fi
if [[ "$1" == kickstart && -f "$TEST_DIR/fail_restart" ]]; then exit 1; fi
''')

    def stub(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def run_script(self, name, *args):
        return subprocess.run(['bash', str(self.repo / 'scripts' / name), *args],
                              env=self.env, capture_output=True, text=True)

    def launch_log(self):
        path = Path(self.env['TEST_DIR']) / 'launchctl.log'
        return path.read_text() if path.exists() else ''

    def test_plists_escape_paths_and_headless_arguments(self):
        logdir = self.home / 'Library/Logs/quota-canvas'
        agent_keys = {'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30}
        binary = self.repo / '.venv/bin/quota-canvas'
        updater = self.repo / 'scripts/auto-update.sh'
        for script, args, label, arguments, keys, env_vars, logs in [
            ('install-agent.sh', (), 'com.digimaga.quota-canvas',
             [str(binary), 'run'], agent_keys, None, ('out.log', 'err.log')),
            ('install-agent.sh', ('--no-screen',), 'com.digimaga.quota-canvas-headless',
             [str(binary), '--no-screen', 'run'], agent_keys, None,
             ('headless.out.log', 'headless.err.log')),
            ('install-auto-update.sh', (), 'com.digimaga.quota-canvas-autoupdate',
             ['/bin/bash', str(updater)], {'RunAtLoad': True, 'StartInterval': 600},
             {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},
             ('autoupdate.log', 'autoupdate.log')),
        ]:
            with self.subTest(label=label):
                result = self.run_script(script, *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                plist = self.home / 'Library/LaunchAgents' / f'{label}.plist'
                self.assertEqual(subprocess.run(['/usr/bin/plutil', '-lint', str(plist)],
                                                capture_output=True).returncode, 0)
                with plist.open('rb') as f:
                    data = plistlib.load(f)
                self.assertEqual(data['Label'], label)
                program = data['ProgramArguments']
                self.assertEqual(len(program), len(arguments))
                for got, want in zip(program, arguments):
                    if want.startswith('/'):
                        self.assertEqual(Path(got).resolve(), Path(want).resolve())
                        self.assertTrue(Path(got).resolve().is_relative_to(
                            self.repo.resolve()) or got == want)
                    else:
                        self.assertEqual(got, want)
                for key, value in keys.items():
                    self.assertEqual(data[key], value)
                if env_vars is None:
                    self.assertNotIn('EnvironmentVariables', data)
                else:
                    self.assertEqual(data['EnvironmentVariables'], env_vars)
                self.assertEqual(data['StandardOutPath'], str(logdir / logs[0]))
                self.assertEqual(data['StandardErrorPath'], str(logdir / logs[1]))
                self.assertTrue(str(logdir).startswith(str(self.home)))

    def test_installer_uninstall_boots_out_and_removes_plist(self):
        for script, args, label in [
            ('install-agent.sh', (), 'com.digimaga.quota-canvas'),
            ('install-agent.sh', ('--no-screen',), 'com.digimaga.quota-canvas-headless'),
            ('install-auto-update.sh', (), 'com.digimaga.quota-canvas-autoupdate'),
        ]:
            with self.subTest(label=label):
                self.assertEqual(self.run_script(script, *args).returncode, 0)
                plist = self.home / 'Library/LaunchAgents' / f'{label}.plist'
                self.assertTrue(plist.exists())
                before = self.launch_log()
                result = self.run_script(script, *args, '--uninstall')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'removed {label}', result.stdout)
                self.assertFalse(plist.exists())
                added = self.launch_log()[len(before):]
                self.assertIn(f'bootout gui/{os.getuid()}/{label}', added)

    def test_invalid_plist_does_not_stop_or_replace_agent(self):
        for script, label in [('install-agent.sh', 'com.digimaga.quota-canvas'),
                              ('install-auto-update.sh', 'com.digimaga.quota-canvas-autoupdate')]:
            with self.subTest(script=script):
                directory = self.home / 'Library/LaunchAgents'
                directory.mkdir(parents=True, exist_ok=True)
                plist = directory / f'{label}.plist'
                plist.write_text('existing')
                self.stub('plutil', '#!/bin/bash\nexit 1\n')
                self.assertNotEqual(self.run_script(script).returncode, 0)
                self.assertEqual(plist.read_text(), 'existing')
                self.assertEqual(self.launch_log(), '')

    def update_stubs(self):
        base = Path(self.env['TEST_DIR'])
        (base / 'head').write_text('old')
        (base / 'remote').write_text('new')
        self.stub('git', '''#!/bin/bash
case "$1" in
 branch) echo main ;;
 status|fetch) ;;
 rev-parse) if [[ "$2" == HEAD ]]; then cat "$TEST_DIR/head"; else cat "$TEST_DIR/remote"; fi ;;
 merge-base|cat-file) exit 0 ;;
 merge) cp "$TEST_DIR/remote" "$TEST_DIR/head" ;;
 diff) if [[ ! -f "$TEST_DIR/no_dependency_change" ]]; then echo pyproject.toml; fi ;;
 *) exit 1 ;;
esac
''')
        pip = self.repo / '.venv/bin/pip'
        pip.parent.mkdir(parents=True)
        pip.write_text('''#!/bin/bash
printf 'pip\\n' >> "$TEST_DIR/pip.log"
[[ ! -f "$TEST_DIR/fail_pip" ]]
''')
        pip.chmod(0o755)

    def test_update_retries_failed_install_after_head_advanced(self):
        self.update_stubs()
        base = Path(self.env['TEST_DIR'])
        state_dir = self.home / 'Library/Application Support/quota-canvas'
        (base / 'fail_pip').touch()
        first = self.run_script('auto-update.sh')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('warn: reinstall failed', first.stdout)
        self.assertEqual((base / 'head').read_text(), 'new')
        pending = list(state_dir.glob('*.reinstall-pending'))
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.launch_log().count('kickstart'), 2)
        (base / 'fail_pip').unlink()
        second = self.run_script('auto-update.sh')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 2)
        self.assertFalse(pending[0].exists())
        self.assertEqual(self.launch_log().count('kickstart'), 4)
        third = self.run_script('auto-update.sh')
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 2)
        self.assertEqual(self.launch_log().count('kickstart'), 4)

    def test_update_retries_failed_restart(self):
        self.update_stubs()
        base = Path(self.env['TEST_DIR'])
        (base / 'fail_restart').touch()
        self.assertNotEqual(self.run_script('auto-update.sh').returncode, 0)
        (base / 'fail_restart').unlink()
        self.assertEqual(self.run_script('auto-update.sh').returncode, 0)
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 2)
        # 失敗した回も含めて両方の agent に kickstart を試みる (2 + 2)
        self.assertEqual(self.launch_log().count('kickstart'), 4)

    def test_pending_reinstall_is_kept_until_restart_succeeds(self):
        self.update_stubs()
        base = Path(self.env['TEST_DIR'])
        state_dir = self.home / 'Library/Application Support/quota-canvas'
        (base / 'fail_pip').touch()
        first = self.run_script('auto-update.sh')
        self.assertEqual(first.returncode, 0, first.stderr)
        pending = list(state_dir.glob('*.reinstall-pending'))
        self.assertEqual(len(pending), 1)

        # SHA は一致したまま・pip は成功・kickstart だけ失敗 → pending を残す
        (base / 'fail_pip').unlink()
        (base / 'fail_restart').touch()
        second = self.run_script('auto-update.sh')
        self.assertNotEqual(second.returncode, 0)
        self.assertTrue(pending[0].exists())
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 2)

        # 次回は再インストールと再起動をやり直し、成功して初めて pending を消す
        (base / 'fail_restart').unlink()
        third = self.run_script('auto-update.sh')
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertFalse(pending[0].exists())
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 3)
        self.assertEqual(self.launch_log().count('kickstart'), 6)

        fourth = self.run_script('auto-update.sh')
        self.assertEqual(fourth.returncode, 0, fourth.stderr)
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 3)
        self.assertEqual(self.launch_log().count('kickstart'), 6)

    def test_update_distinguishes_unloaded_agents_from_launchctl_errors(self):
        self.update_stubs()
        self.stub('launchctl', '''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_DIR/launchctl.log"
if [[ "$1" == print ]]; then
  if [[ "$2" == *-headless ]]; then echo "Could not find service" >&2; exit 113; fi
  echo "some launchctl failure" >&2; exit 1
fi
exit 0
''')
        result = self.run_script('auto-update.sh')
        # 「未ロード」以外の print 失敗は再起動できたか分からないので失敗にする
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('is not loaded', result.stdout)  # 未ロードは skip のまま
        self.assertIn('launchctl print', result.stdout)
        self.assertEqual(self.launch_log().count('kickstart'), 0)
        state_dir = self.home / 'Library/Application Support/quota-canvas'
        self.assertEqual(list(state_dir.glob('*.sha')), [])  # 適用済みにしない

    def test_pending_is_kept_when_launchctl_print_fails(self):
        self.update_stubs()
        base = Path(self.env['TEST_DIR'])
        state_dir = self.home / 'Library/Application Support/quota-canvas'
        (base / 'fail_pip').touch()
        self.assertEqual(self.run_script('auto-update.sh').returncode, 0)
        pending = list(state_dir.glob('*.reinstall-pending'))
        self.assertEqual(len(pending), 1)

        # SHA は一致・pip は成功・print が「未ロード」以外で失敗 → pending を残す
        (base / 'fail_pip').unlink()
        self.stub('launchctl', '''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_DIR/launchctl.log"
[[ "$1" == print ]] && { echo "launchctl: operation not permitted" >&2; exit 1; }
exit 0
''')
        result = self.run_script('auto-update.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(pending[0].exists())
        self.assertEqual(self.launch_log().count('kickstart'), 2)  # 初回の分だけ

    def prepare_setup(self):
        (self.repo / '.venv/bin').mkdir(parents=True)
        python = self.repo / '.venv/bin/python'
        python.write_text('#!/bin/bash\nexit 1\n')  # pip モジュールを持たない venv
        python.chmod(0o755)
        self.stub('brew', '#!/bin/bash\nexit 0\n')

    def test_setup_uses_uv_when_venv_has_no_pip(self):
        self.prepare_setup()
        self.stub('uv', '''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_DIR/uv.log"
''')
        result = self.run_script('setup.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (Path(self.env['TEST_DIR']) / 'uv.log').read_text()
        self.assertIn('pip install -q --python .venv/bin/python -e .', log)

    def test_setup_prefers_existing_pip(self):
        self.prepare_setup()
        pip = self.repo / '.venv/bin/pip'
        pip.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_DIR/pip.log"
''')
        pip.chmod(0o755)
        self.stub('uv', '#!/bin/bash\nexit 1\n')
        result = self.run_script('setup.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (Path(self.env['TEST_DIR']) / 'pip.log').read_text()
        self.assertIn('install -q --upgrade pip', log)
        self.assertIn('install -q -e .', log)
        self.assertFalse((Path(self.env['TEST_DIR']) / 'uv.log').exists())

    def test_setup_without_pip_or_uv_fails_with_hint(self):
        self.prepare_setup()
        homes = [self.home / '.local/bin/uv', self.home / '.cargo/bin/uv']
        if any(path.exists() for path in homes) or \
                any(Path(path).exists() for path in ('/opt/homebrew/bin/uv', '/usr/local/bin/uv')):
            self.skipTest('uv is installed on this machine')
        result = self.run_script('setup.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uv', result.stderr)
        self.assertIn('pip', result.stderr)

    def test_update_skips_install_when_dependencies_unchanged(self):
        self.update_stubs()
        base = Path(self.env['TEST_DIR'])
        self.assertEqual(self.run_script('auto-update.sh').returncode, 0)
        (base / 'remote').write_text('later')
        (base / 'no_dependency_change').touch()
        self.assertEqual(self.run_script('auto-update.sh').returncode, 0)
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 1)
        self.assertEqual(self.launch_log().count('kickstart'), 4)

    def test_hooks_run_non_executable_script(self):
        root = Path(self.env['TEST_DIR']) / 'plugin & sample'
        shutil.copytree(ROOT / 'plugins/quota-canvas-routing', root)
        (root / 'scripts/session-start.sh').chmod(0o644)
        self.stub('uname', '#!/bin/bash\necho Darwin\n')
        self.stub('quota-canvas', '#!/bin/bash\nprintf "hook-ok\\n"\n')
        for name, var in [('claude', 'CLAUDE_PLUGIN_ROOT'), ('codex', 'PLUGIN_ROOT')]:
            with self.subTest(name=name):
                data = json.loads((root / 'hooks' / f'{name}.json').read_text())
                command = data['hooks']['SessionStart'][0]['hooks'][0]['command']
                result = subprocess.run(['bash', '-c', command], env=dict(self.env, **{var: str(root)}),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'hook-ok\n')

    def test_documented_gate_allows_non_stop_errors_with_errexit(self):
        readme = (ROOT / 'plugins/quota-canvas-routing/README.md').read_text()
        code = readme.split('```sh\n')[-1].split('\n```')[0].replace('<名前>', 'sample')
        self.stub('quota-canvas', '#!/bin/bash\nexit "$TEST_RC"\n')
        for rc in (0, 1, 2, 3):
            with self.subTest(rc=rc):
                result = subprocess.run(['bash', '-e', '-c', code + '\necho started'],
                                        env=dict(self.env, TEST_RC=str(rc)), capture_output=True, text=True)
                self.assertEqual(result.returncode, 3 if rc == 3 else 0)
                self.assertEqual(result.stdout, '' if rc == 3 else 'started\n')


if __name__ == '__main__':
    unittest.main()
