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
        for script, args, label in [
            ('install-agent.sh', (), 'com.digimaga.quota-canvas'),
            ('install-agent.sh', ('--no-screen',), 'com.digimaga.quota-canvas-headless'),
            ('install-auto-update.sh', (), 'com.digimaga.quota-canvas-autoupdate'),
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
                executable = data['ProgramArguments'][1 if script == 'install-auto-update.sh' else 0]
                self.assertTrue(Path(executable).resolve().is_relative_to(self.repo.resolve()))
                self.assertTrue(data['StandardOutPath'].startswith(str(self.home)))
                if args:
                    self.assertEqual(data['ProgramArguments'][1:], ['--no-screen', 'run'])
                elif script == 'install-agent.sh':
                    self.assertEqual(data['ProgramArguments'][1:], ['run'])

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
        (base / 'fail_pip').touch()
        self.assertNotEqual(self.run_script('auto-update.sh').returncode, 0)
        self.assertEqual((base / 'head').read_text(), 'new')
        self.assertEqual(self.launch_log(), '')
        (base / 'fail_pip').unlink()
        self.assertEqual(self.run_script('auto-update.sh').returncode, 0)
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 2)
        self.assertEqual(self.launch_log().count('kickstart'), 2)
        self.assertEqual(self.run_script('auto-update.sh').returncode, 0)
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 2)
        self.assertEqual(self.launch_log().count('kickstart'), 2)

    def test_update_retries_failed_restart(self):
        self.update_stubs()
        base = Path(self.env['TEST_DIR'])
        (base / 'fail_restart').touch()
        self.assertNotEqual(self.run_script('auto-update.sh').returncode, 0)
        (base / 'fail_restart').unlink()
        self.assertEqual(self.run_script('auto-update.sh').returncode, 0)
        self.assertEqual((base / 'pip.log').read_text().count('pip\n'), 2)
        self.assertEqual(self.launch_log().count('kickstart'), 3)

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
