import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


INSTALL = Path(__file__).with_name("install.sh")


class InstallHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.settings = self.root / "custom" / "settings.json"
        self.settings.parent.mkdir()

    @property
    def command(self):
        return ('[ -n "$ORCA_PANE_KEY" ] && [ "${ORCA_GATE_HOOK:-}" != off ] || exit 0; '
                'exec "$HOME/.local/bin/orca-agent-gate" hook claude')

    @property
    def legacy_command(self):
        return str(self.home / ".local/bin/orca-agent-gate") + " hook claude"

    def run_install(self, *args, extra_env=None):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CLAUDE_SETTINGS"] = str(self.settings)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(INSTALL), *args], env=env, capture_output=True, text=True,
            preexec_fn=lambda: os.umask(0),
        )

    def backups(self):
        return list(self.settings.parent.glob("settings.json.bak-orca-gate-*"))

    def test_add_hook_is_idempotent_and_preserves_other_hooks_and_mode(self):
        original = {
            "other": 1,
            "hooks": {"UserPromptSubmit": [
                {"matcher": "x", "hooks": [{"type": "command", "command": "other-hook"}]},
                {"hooks": [{"type": "command", "command": "orca-hook"}]},
            ], "Stop": [{"hooks": [{"command": "stop-hook"}]}]},
        }
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        self.settings.chmod(0o640)
        result = self.run_install("--claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(updated["other"], 1)
        self.assertEqual(updated["hooks"]["UserPromptSubmit"][:2], original["hooks"]["UserPromptSubmit"])
        self.assertEqual(updated["hooks"]["Stop"], original["hooks"]["Stop"])
        added = updated["hooks"]["UserPromptSubmit"][2]
        self.assertEqual(added, {"hooks": [{"type": "command", "command": self.command, "timeout": 10}]})
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_bytes(), json.dumps(original).encode())
        self.assertEqual(self.backups()[0].stat().st_mode & 0o777, 0o600)
        before = self.settings.read_bytes()
        backups_before = self.backups()
        result = self.run_install("--claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.backups(), backups_before)

    def test_remove_only_matching_command_and_empty_group(self):
        self.settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"command": self.command}, {"command": "keep-in-same-group"}]},
            {"hooks": [{"command": self.legacy_command}]},
            {"hooks": [{"command": "$HOME/.local/bin/orca-agent-gate hook claude"}]},
            {"hooks": [{"command": "keep"}]},
        ]}}), encoding="utf-8")
        result = self.run_install("--remove-claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        entries = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
        self.assertEqual(entries, [
            {"hooks": [{"command": "keep-in-same-group"}]},
            {"hooks": [{"command": "keep"}]},
        ])
        self.assertEqual(len(self.backups()), 1)

    def test_remove_deletes_empty_group_and_hooks_key(self):
        self.settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"command": self.command}]},
        ]}}), encoding="utf-8")
        result = self.run_install("--remove-claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), {})

    def test_invalid_json_is_unchanged(self):
        self.settings.write_text("{broken", encoding="utf-8")
        before = self.settings.read_bytes()
        result = self.run_install("--claude-hook")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_dry_run_does_not_change_settings_or_create_files(self):
        self.settings.write_text('{"keep": true}', encoding="utf-8")
        before = self.settings.read_bytes()
        result = self.run_install("--claude-hook", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.backups(), [])
        self.assertEqual(list(self.settings.parent.iterdir()), [self.settings])
        self.settings.unlink()
        result = self.run_install("--claude-hook", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings.exists())
        self.settings.parent.rmdir()
        for args in (("--claude-hook", "--dry-run"),
                     ("--remove-claude-hook", "--dry-run"), ("--dry-run",)):
            result = self.run_install(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.settings.parent.exists())
            self.assertFalse((self.home / ".local").exists())

    def test_missing_settings_file_is_created(self):
        result = self.run_install("--claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], self.command)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_legacy_hooks_are_upgraded_and_then_idempotent(self):
        self.settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"command": self.legacy_command},
                       {"command": "$HOME/.local/bin/orca-agent-gate hook claude"},
                       {"command": "other"}]}]}}), encoding="utf-8")
        result = self.run_install("--claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        hooks = json.loads(self.settings.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"]
        self.assertEqual([hook["command"] for hook in hooks], [self.command, self.command, "other"])
        before = self.settings.read_bytes()
        result = self.run_install("--claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_hook_shell_skips_gate_outside_orca_and_when_disabled(self):
        gate = self.home / ".local/bin/orca-agent-gate"
        gate.parent.mkdir(parents=True)
        marker = self.root / "called"
        gate.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$MARKER"\n')
        gate.chmod(0o755)
        env = {"HOME": str(self.home), "MARKER": str(marker), "PATH": os.environ["PATH"]}
        for variables in ({}, {"ORCA_PANE_KEY": "pane", "ORCA_GATE_HOOK": "off"}):
            result = subprocess.run(["/bin/sh", "-c", self.command], env={**env, **variables})
            self.assertEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
        result = subprocess.run(["/bin/sh", "-c", self.command], env={**env, "ORCA_PANE_KEY": "pane"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), "hook claude\n")

    def test_concurrent_change_after_backup_is_preserved(self):
        self.settings.write_text('{"before": true}', encoding="utf-8")
        injector = self.root / "injector"
        injector.mkdir()
        (injector / "sitecustomize.py").write_text('''
import builtins, glob, os
real_open = builtins.open
count = 0
def replacement(path, mode="r", *args, **kwargs):
    global count
    if os.path.realpath(os.fspath(path)) == os.path.realpath(os.environ["CLAUDE_SETTINGS"]) and mode == "rb":
        count += 1
        if count == 2:
            assert glob.glob(path + ".bak-orca-gate-*")
            assert glob.glob(os.path.join(os.path.dirname(path), ".settings.json.*"))
            with real_open(path, "wb") as stream:
                stream.write(b'{"concurrent": true}')
    return real_open(path, mode, *args, **kwargs)
builtins.open = replacement
''', encoding="utf-8")
        result = self.run_install("--claude-hook", extra_env={"PYTHONPATH": str(injector)})
        self.assertEqual(result.returncode, 1)
        self.assertIn("設定が別のプロセスで更新されました。もう一度実行してください", result.stderr)
        self.assertEqual(self.settings.read_bytes(), b'{"concurrent": true}')
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_bytes(), b'{"before": true}')
        self.assertEqual(list(self.settings.parent.glob(".settings.json.*")), [])

    def test_concurrent_mode_change_after_backup_is_preserved(self):
        injector = self.root / "mode-injector"
        injector.mkdir()
        (injector / "sitecustomize.py").write_text('''
import builtins, glob, os
real_open = builtins.open
count = 0
def replacement(path, mode="r", *args, **kwargs):
    global count
    if os.path.realpath(os.fspath(path)) == os.path.realpath(os.environ["CLAUDE_SETTINGS"]) and mode == "rb":
        count += 1
        if count == 2:
            assert glob.glob(path + ".bak-orca-gate-*")
            assert glob.glob(os.path.join(os.path.dirname(path), ".settings.json.*"))
            os.chmod(path, 0o600)
    return real_open(path, mode, *args, **kwargs)
builtins.open = replacement
''', encoding="utf-8")
        for action, original in (
            ("--claude-hook", b'{}'),
            ("--remove-claude-hook", json.dumps({"hooks": {"UserPromptSubmit": [
                {"hooks": [{"command": self.command}]},
            ]}}).encode()),
        ):
            with self.subTest(action=action):
                self.settings.write_bytes(original)
                self.settings.chmod(0o644)
                inode = self.settings.stat().st_ino
                result = self.run_install(action, extra_env={"PYTHONPATH": str(injector)})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("設定が別のプロセスで更新されました", result.stderr)
                self.assertEqual(self.settings.read_bytes(), original)
                self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.settings.stat().st_ino, inode)
                self.assertEqual(list(self.settings.parent.glob(".settings.json.*")), [])

    def test_lock_contention_waits_and_leaves_settings_unchanged(self):
        original = b'{"before": true}'
        self.settings.write_bytes(original)
        lock = Path(str(self.settings) + ".orca-gate.lock")
        with lock.open("w") as stream:
            lock.chmod(0o644)
            fcntl.flock(stream, fcntl.LOCK_EX)
            start = time.monotonic()
            result = self.run_install("--claude-hook")
            elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("設定のロックを取得できませんでした", result.stderr)
        self.assertGreaterEqual(elapsed, 2.5)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)

    def test_hardlink_lock_does_not_change_shared_inode_mode(self):
        self.settings.write_bytes(b'{}')
        target = self.root / "shared-file"
        target.write_bytes(b"shared")
        target.chmod(0o644)
        lock = Path(str(self.settings) + ".orca-gate.lock")
        os.link(target, lock)

        result = self.run_install("--claude-hook")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), b"shared")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_symlink_lock_does_not_modify_target_or_settings(self):
        original = b'{"before": true}'
        self.settings.write_bytes(original)
        self.settings.chmod(0o640)
        target = self.root / "lock-target"
        target.write_bytes(b"lock target")
        target.chmod(0o644)
        lock = Path(str(self.settings) + ".orca-gate.lock")
        lock.symlink_to(target)

        result = self.run_install("--claude-hook")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(lock.is_symlink())
        self.assertEqual(target.read_bytes(), b"lock target")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.backups(), [])

    def test_non_regular_lock_is_rejected_without_changes(self):
        original = b'{"before": true}'
        self.settings.write_bytes(original)
        lock = Path(str(self.settings) + ".orca-gate.lock")
        os.mkfifo(lock, 0o644)
        lock.chmod(0o644)

        result = self.run_install("--claude-hook")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(self.backups(), [])

    def test_symlink_is_preserved_and_target_and_backup_are_updated(self):
        target = self.root / "target.json"
        original = b'{"preserve": true}'
        target.write_bytes(original)
        target.chmod(0o640)
        self.settings.symlink_to(target)
        result = self.run_install("--claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(json.loads(target.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], self.command)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        backups = list(self.root.glob("target.json.bak-orca-gate-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(str(target) + ".orca-gate.lock").stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(str(self.settings) + ".orca-gate.lock").exists())

    def test_backups_are_unique_for_rapid_successive_updates(self):
        self.settings.write_bytes(b'{}')
        result = self.run_install("--claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        added = self.settings.read_bytes()
        result = self.run_install("--remove-claude-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({backup.read_bytes() for backup in self.backups()}, {b'{}', added})
        self.assertEqual(len(self.backups()), 2)


if __name__ == "__main__":
    unittest.main()
