import fcntl
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

GATE = Path(__file__).with_name("orca-agent-gate").resolve()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.output = self.home / "called.json"
        self.cli = self.bin / "fake-cli"
        self.cli.write_text("#!/usr/bin/python3\nimport json, os, sys, time\nwith open(os.environ['TEST_OUTPUT'], 'w') as f:\n json.dump({'argv': sys.argv[1:], 'force': os.environ.get('ORCA_GATE_FORCE'), 'hook': os.environ.get('ORCA_GATE_HOOK')}, f)\ntime.sleep(float(os.environ.get('TEST_HOLD', '0')))\n")
        self.cli.chmod(0o755)
        self.check = self.bin / "check"
        self.check.write_text("#!/bin/sh\nprintf '%s\\n' \"$CHECK_MESSAGE\"\nexit \"$CHECK_EXIT\"\n")
        self.check.chmod(0o755)
        fake_ps = self.bin / "ps"
        fake_ps.write_text("#!/bin/sh\nprintf '%s\\n' 'Mon Jan  1 00:00:00 2024'\n")
        fake_ps.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + ":/usr/bin:/bin",
                        TEST_OUTPUT=str(self.output), ORCA_GATE_CHECK_CMD=str(self.check),
                        CHECK_EXIT="0", CHECK_MESSAGE="")
        for name in ("ORCA_GATE_HOOK", "ORCA_PANE_KEY", "QUOTA_STATUS_PATH", "ORCA_GATE_NOW", "OPENDESIGN_API_KEY"):
            self.env.pop(name, None)
        self.config = {"agents": {"codex": {"cli": str(self.cli)}, "devin": {"cli": str(self.cli)}}}
        self.save_config()

    def save_config(self):
        config = self.home / ".config/orca-agent-gate/config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(self.config))

    def run_gate(self, agent="codex", args=(), **env):
        result = subprocess.run([str(GATE), agent, *args], env=dict(self.env, **env),
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result

    def called(self):
        return json.loads(self.output.read_text())

    def test_stop(self):
        result = self.run_gate(CHECK_EXIT="3", CHECK_MESSAGE="quota stop")
        self.assertEqual(result.returncode, 3)
        self.assertIn("quota stop", result.stderr)
        self.assertIn("ORCA_GATE_FORCE=1", result.stderr)
        self.assertFalse(self.output.exists())

    def test_stop_when_detached_descendant_holds_output(self):
        self.check.write_text("#!/usr/bin/python3\nimport os, subprocess, sys\n"
                              "subprocess.Popen(['/bin/sleep', '3'], start_new_session=True)\n"
                              "print('quota stop', flush=True)\nsys.exit(3)\n")
        started = time.monotonic()
        result = self.run_gate()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("quota stop", result.stderr)
        self.assertFalse(self.output.exists())

    def test_stop_when_check_writes_large_output(self):
        self.check.write_text("#!/usr/bin/python3\nimport sys\n"
                              "sys.stdout.write('x' * 1000000)\nsys.stdout.flush()\nsys.exit(3)\n")
        started = time.monotonic()
        result = self.run_gate()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.output.exists())

    def test_stop_reason_at_end_of_large_output_is_shown(self):
        self.check.write_text("#!/usr/bin/python3\nimport sys\n"
                              "sys.stdout.write('x' * 1000000)\nsys.stderr.write('y' * 200000 + 'TAIL-REASON')\n"
                              "sys.exit(3)\n")
        result = self.run_gate()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("TAIL-REASON", result.stderr)
        self.assertIn("省略", result.stderr)
        self.assertLess(len(result.stderr), 200000)

    def test_go_passes_arguments(self):
        args = ["--resume", "session", "--", "prompt with spaces"]
        result = self.run_gate(args=args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()["argv"], args)
        self.assertEqual(len((self.home / ".local/state/orca-agent-gate/launch.log").read_text().splitlines()), 1)
        self.assertNotIn("prompt", (self.home / ".local/state/orca-agent-gate/launch.log").read_text())

    def test_reserve_continues(self):
        result = self.run_gate(CHECK_MESSAGE="reserve warning")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reserve warning", result.stderr)
        self.assertTrue(self.output.exists())

    def test_missing_check_continues(self):
        result = self.run_gate(ORCA_GATE_CHECK_CMD=str(self.bin / "missing"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("判定できない", result.stderr)
        self.assertTrue(self.output.exists())

    def test_check_absent_from_path_continues(self):
        env = dict(self.env, PATH=str(self.bin))
        env.pop("ORCA_GATE_CHECK_CMD")
        result = subprocess.run(["/usr/bin/python3", str(GATE), "codex"], env=env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("判定できない", result.stderr)
        self.assertTrue(self.output.exists())

    def test_path_lookup_skips_wrapper(self):
        real_bin = self.home / "real-bin"
        real_bin.mkdir()
        (self.bin / "codex").symlink_to(GATE)
        (real_bin / "codex").symlink_to(self.cli)
        del self.config["agents"]["codex"]["cli"]
        self.save_config()
        result = self.run_gate(args=["hello"], PATH=str(self.bin) + ":" + str(real_bin) + ":/usr/bin:/bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()["argv"], ["hello"])

    def test_force_bypasses_stop_and_is_removed(self):
        result = self.run_gate(CHECK_EXIT="3", ORCA_GATE_FORCE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("強制起動", result.stderr)
        self.assertIsNone(self.called()["force"])

    def test_command_code_model_schedule_and_force(self):
        self.config["agents"]["command-code"] = {"cli": str(self.cli)}
        self.save_config()
        glm = "z-ai/glm-5.3-flash"
        deepseek = "deepseek/deepseek-v4.1-flash"
        cases = [("2026-11-16T00:59", deepseek), ("2026-11-16T01:00", glm),
                 ("2026-11-16T03:59", glm), ("2026-11-16T04:00", deepseek),
                 ("2026-11-16T05:59", deepseek), ("2026-11-16T06:00", glm),
                 ("2026-11-16T09:59", glm), ("2026-11-16T10:00", deepseek),
                 ("2026-11-20T08:00", glm), ("2026-11-21T02:00", deepseek),
                 ("2026-11-22T08:00", deepseek)]
        for now, model in cases:
            with self.subTest(now=now):
                result = self.run_gate("command-code", ["--resume", "session"], ORCA_GATE_NOW=now,
                                       ORCA_GATE_FORCE="1", CHECK_EXIT="3")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["argv"], ["-m", model, "--resume", "session"])
                self.assertIn("model=" + model, (self.home / ".local/state/orca-agent-gate/launch.log")
                              .read_text().splitlines()[-1])
        result = self.run_gate("command-code", ORCA_GATE_NOW="2026-11-16T02:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()["argv"], ["-m", glm])

    def test_command_code_respects_explicit_model_and_config(self):
        self.config["agents"]["command-code"] = {"cli": str(self.cli)}
        self.save_config()
        log = self.home / ".local/state/orca-agent-gate/launch.log"
        for args in (["-m", "chosen"], ["-mchosen"], ["--model", "chosen"], ["--model=chosen"]):
            with self.subTest(args=args):
                result = self.run_gate("command-code", args, ORCA_GATE_NOW="2026-11-16T02:00")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["argv"], args)
                self.assertNotIn(" model=", log.read_text().splitlines()[-1])
        self.config["agents"]["command-code"]["auto_model"] = False
        self.save_config()
        result = self.run_gate("command-code", ["hello"], ORCA_GATE_NOW="2026-11-16T02:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()["argv"], ["hello"])
        self.assertNotIn(" model=", log.read_text().splitlines()[-1])
        result = self.run_gate("codex", ["hello"], ORCA_GATE_NOW="2026-11-16T02:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()["argv"], ["hello"])
        self.assertNotIn(" model=", log.read_text().splitlines()[-1])

    def test_command_code_subcommands_and_end_of_options(self):
        self.config["agents"]["command-code"] = {"cli": str(self.cli)}
        self.save_config()
        log = self.home / ".local/state/orca-agent-gate/launch.log"
        commands = ("info", "status", "help", "whoami", "update", "feedback", "taste",
                    "learn-taste", "mcp", "cloud", "skills", "acp", "mods", "login", "logout",
                    "config", "providers", "issue")
        for command in commands:
            with self.subTest(command=command):
                args = [command, "example"]
                result = self.run_gate("command-code", args, ORCA_GATE_NOW="2026-11-16T02:00")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["argv"], args)
                self.assertNotIn(" model=", log.read_text().splitlines()[-1])
        for args in (["--no-auto-update", "acp"], ["--name", "session", "config"],
                     ["--sessions", "session", "acp"],
                     ["--name=example", "providers"], ["--name", "acp", "issue"],
                     ["--no-auto-update", "--effort", "high", "acp"],
                     ["--", "acp"], ["--no-auto-update", "--", "acp"],
                     ["--name", "acp", "--", "config", "--model=ignored"]):
            with self.subTest(args=args):
                result = self.run_gate("command-code", args, ORCA_GATE_NOW="2026-11-16T02:00")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["argv"], args)
                self.assertNotIn(" model=", log.read_text().splitlines()[-1])
        for args in (["--name", "acp", "hello"], ["hello", "acp"],
                     ["hello", "--", "acp"], ["--", "hello", "acp"],
                     ["--", "--model", "acp"]):
            with self.subTest(args=args):
                result = self.run_gate("command-code", args, ORCA_GATE_NOW="2026-11-16T02:00")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["argv"], ["-m", "z-ai/glm-5.3-flash", *args])
                self.assertIn(" model=z-ai/glm-5.3-flash", log.read_text().splitlines()[-1])
        for arg in ("-mchosen", "-m", "--model", "--model=chosen"):
            with self.subTest(after_separator=arg):
                args = ["--", arg]
                result = self.run_gate("command-code", args, ORCA_GATE_NOW="2026-11-16T02:00")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["argv"], ["-m", "z-ai/glm-5.3-flash", *args])
                self.assertIn(" model=z-ai/glm-5.3-flash", log.read_text().splitlines()[-1])

    def test_command_code_option_values_are_not_model_flags(self):
        self.config["agents"]["command-code"] = {"cli": str(self.cli)}
        self.save_config()
        required = ("--session", "-n", "--name", "--max-turns", "--output-format",
                    "--tools-enable", "--effort", "--theme", "--config", "--permission-mode",
                    "--add-dir", "--base", "--mod", "--mod-option", "--skill")
        optional = ("-r", "--resume", "--sessions", "-p", "--print", "-w", "--worktree", "--cloud")
        model = ["-m", "z-ai/glm-5.3-flash"]
        for option in required:
            for value in ("-models", "--"):
                with self.subTest(option=option, value=value):
                    args = [option, value, "hello"]
                    result = self.run_gate("command-code", args, ORCA_GATE_NOW="2026-11-16T02:00")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.called()["argv"], model + args)
            with self.subTest(option=option, followed_by_model=True):
                args = [option, "-models", "-m", "chosen"]
                result = self.run_gate("command-code", args, ORCA_GATE_NOW="2026-11-16T02:00")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["argv"], args)
        for option in optional:
            for args, expected in (([option, "-models"], [option, "-models"]),
                                   ([option, "session", "hello"], model + [option, "session", "hello"])):
                with self.subTest(option=option, args=args):
                    result = self.run_gate("command-code", args, ORCA_GATE_NOW="2026-11-16T02:00")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.called()["argv"], expected)

    def test_command_code_invalid_now_uses_actual_utc(self):
        module = runpy.run_path(str(GATE))
        model = module["command_code_model"]
        with patch.dict(model.__globals__, {"datetime": unittest.mock.Mock(wraps=datetime)}) as scope:
            scope["datetime"].now.return_value = datetime(2026, 11, 16, 2, tzinfo=timezone.utc)
            for value in ("invalid", "2026-11-16T2:00", "2026-11-16T02:00Z", "2026-02-30T02:00"):
                with self.subTest(value=value), patch.dict(os.environ, {"ORCA_GATE_NOW": value}):
                    self.assertEqual(model(), "z-ai/glm-5.3-flash")
            scope["datetime"].now.assert_called_with(timezone.utc)

    def test_cap_and_dead_pid_cleanup(self):
        self.config["agents"]["codex"]["cap"] = 1
        self.save_config()
        directory = self.home / ".local/state/orca-agent-gate/codex"
        directory.mkdir(parents=True)
        own_start = "Mon Jan  1 00:00:00 2024"
        live = directory / str(os.getpid())
        live.write_text(own_start)
        dead = directory / "99999999"
        dead.write_text("old")
        result = self.run_gate()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("上限", result.stderr)
        self.assertFalse(dead.exists())
        self.assertFalse(self.output.exists())
        live.write_text("different start")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(live.exists())
        self.assertTrue(self.output.exists())

    def test_force_bypasses_cap(self):
        self.config["agents"]["codex"]["cap"] = 1
        self.save_config()
        directory = self.home / ".local/state/orca-agent-gate/codex"
        directory.mkdir(parents=True)
        live = directory / str(os.getpid())
        live.write_text("Mon Jan  1 00:00:00 2024")
        result = self.run_gate(ORCA_GATE_FORCE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.exists())
        self.assertEqual([path.name for path in directory.iterdir()], [str(os.getpid())])

    def test_concurrent_cap_is_atomic(self):
        self.config["agents"]["codex"]["cap"] = 1
        self.save_config()
        env = dict(self.env, TEST_HOLD="2")
        first = subprocess.Popen([str(GATE), "codex"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen([str(GATE), "codex"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        results = [first.communicate(timeout=15), second.communicate(timeout=15)]
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 3], results)

    def test_devin_auto_model_and_prompt(self):
        result = self.run_gate("devin", ["--resume", "session-id", "--", "hello --model bad"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()["argv"], ["--model", "swe-2-high", "--resume", "session-id", "--", "hello --model bad"])

    def test_devin_disallowed_model(self):
        result = self.run_gate("devin", ["--model", "not-allowed", "--", "prompt"])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.output.exists())

    def test_devin_allowed_and_forced_models(self):
        args = ["--model=swe-2-medium", "--", "prompt"]
        result = self.run_gate("devin", args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()["argv"], args)
        result = self.run_gate("devin", ["--model", "other"], ORCA_GATE_FORCE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called()["argv"], ["--model", "other"])

    def test_unknown_and_off_skip_check(self):
        self.config["agents"]["pi"] = {"cli": str(self.cli)}
        self.config["agents"]["codex"]["off"] = True
        self.save_config()
        for agent in ("pi", "codex"):
            result = self.run_gate(agent, CHECK_EXIT="3")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_agent_id_rejects_paths_and_invalid_names(self):
        for agent in (str(self.cli), "../devin", "codex\\other", "CODEX", ""):
            with self.subTest(agent=agent):
                result = self.run_gate(agent)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.output.exists())

    def test_worker_name_validation_at_launch(self):
        self.check.write_text("#!/bin/sh\ntouch \"$HOME/check-called\"\n")
        for worker in ("a" * 41, "off", "", "bad/name", "bad name", "é", ".", "..", "-worker", "-"):
            for disabled in (False, True):
                with self.subTest(worker=worker, disabled=disabled):
                    self.config["agents"]["codex"].update(worker=worker, off=disabled)
                    self.save_config()
                    result = self.run_gate()
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("worker", result.stderr)
                    self.assertFalse(self.output.exists())
                    self.assertFalse((self.home / "check-called").exists())
        for worker in ("a" * 40, "_worker", ".worker", "worker-name"):
            with self.subTest(worker=worker):
                self.config["agents"]["codex"].update(worker=worker, off=False)
                self.save_config()
                result = self.run_gate()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["hook"], worker)

    def test_off_requires_boolean(self):
        for value in ("false", "true", 1, None, [], {}):
            with self.subTest(value=value):
                self.config["agents"]["codex"]["off"] = value
                self.save_config()
                result = self.run_gate(CHECK_EXIT="3")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("真偽値", result.stderr)
        self.config["agents"]["codex"]["off"] = False
        self.save_config()
        self.assertEqual(self.run_gate(CHECK_EXIT="3").returncode, 3)

    def test_forwarding_script_cannot_reenter(self):
        forward = self.bin / "codex"
        forward.write_text('#!/bin/sh\nexec "$TEST_GATE" codex "$@"\n')
        forward.chmod(0o755)
        del self.config["agents"]["codex"]["cli"]
        self.save_config()
        result = self.run_gate(TEST_GATE=str(GATE))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("再入", result.stderr)
        self.assertFalse(self.output.exists())
        lines = (self.home / ".local/state/orca-agent-gate/launch.log").read_text().splitlines()
        self.assertEqual(sum("check=go" in line for line in lines), 1)

    def test_child_process_can_invoke_gate(self):
        self.cli.write_text("#!/usr/bin/python3\nimport os, subprocess, sys\n"
                            "if os.environ.get('NESTED'):\n sys.exit(0)\n"
                            "env = dict(os.environ, NESTED='1', CHECK_EXIT='3')\n"
                            "sys.exit(subprocess.run([os.environ['TEST_GATE'], 'codex'], env=env).returncode)\n")
        result = self.run_gate(TEST_GATE=str(GATE))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("残量の判定", result.stderr)
        self.assertNotIn("再入", result.stderr)

    def test_check_failure_continues(self):
        result = self.run_gate(CHECK_EXIT="7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("判定できない", result.stderr)
        self.assertTrue(self.output.exists())

    def test_timeout_kills_check_process_group(self):
        marker = self.home / "descendant-finished"
        self.check.write_text('#!/bin/sh\n(sleep 12; touch "$TEST_MARKER") &\nwait\n')
        result = self.run_gate(TEST_MARKER=str(marker))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("判定できない", result.stderr)
        self.assertTrue(self.output.exists())
        time.sleep(3)
        self.assertFalse(marker.exists())

    def test_corrupt_pid_entries_are_removed_individually(self):
        self.config["agents"]["codex"]["cap"] = 2
        self.save_config()
        directory = self.home / ".local/state/orca-agent-gate/codex"
        directory.mkdir(parents=True)
        (directory / "11111111").mkdir()
        (directory / "22222222").write_bytes(b"\xff")
        (directory / "33333333").write_text("not a start time")
        (directory / "²").write_text("invalid")
        (directory / "99999999999999999999").write_text("invalid")
        (directory / str(os.getpid())).write_text("Mon Jan  1 00:00:00 2024")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("11111111", "22222222", "33333333", "²", "99999999999999999999"):
            self.assertFalse((directory / name).exists())
        self.assertIn("不正な PID", result.stderr)

    def test_devin_acp_and_missing_model_values(self):
        for args, expected in [(["acp"], ["acp", "--model", "swe-2-high"]),
                               (["acp", "--model", "swe-2-medium"], ["acp", "--model", "swe-2-medium"]),
                               (["--resume", "session-id"], ["--model", "swe-2-high", "--resume", "session-id"])]:
            with self.subTest(args=args):
                result = self.run_gate("devin", args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.called()["argv"], expected)
        for args in (["--model"], ["--model", "--resume"], ["acp", "--model="], ["--model", "--"]):
            with self.subTest(args=args):
                for force in ("0", "1"):
                    result = self.run_gate("devin", args, ORCA_GATE_FORCE=force)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("値が必要", result.stderr)

    def test_board_counts_only_fresh_valid_data(self):
        self.config["agents"]["codex"].update(cap=3, board_keys=["swe2", "swe2f"])
        self.save_config()
        board = self.home / "board.json"
        directory = self.home / ".local/state/orca-agent-gate/codex"
        directory.mkdir(parents=True)
        (directory / str(os.getpid())).write_text("Mon Jan  1 00:00:00 2024")
        cases = [({"total": {"swe2": 1, "swe2f": 1}, "updated_at": time.time()}, 3),
                 ({"total": {"swe2": 1}, "updated_at": time.time()}, 0),
                 ({"total": {"swe2": 2}, "updated_at": time.time() - 601}, 0),
                 ({"total": {"swe2": 2}, "updated_at": time.time() + 60}, 0),
                 ({"total": {"swe2": "2"}, "updated_at": time.time()}, 0)]
        for data, expected in cases:
            with self.subTest(data=data):
                board.write_text(json.dumps(data))
                result = self.run_gate(ORCA_GATE_BOARD=str(board))
                self.assertEqual(result.returncode, expected, result.stderr)
        board.write_text("{")
        self.assertEqual(self.run_gate(ORCA_GATE_BOARD=str(board)).returncode, 0)
        board.unlink()
        self.assertEqual(self.run_gate(ORCA_GATE_BOARD=str(board)).returncode, 0)

    def test_board_sums_valid_values_even_when_other_keys_are_invalid(self):
        self.config["agents"]["codex"].update(cap=2, board_keys=["valid", "unknown", "invalid", "boolean"])
        self.save_config()
        board = self.home / "board.json"
        board.write_text(json.dumps({"total": {"valid": 2, "unknown": None, "invalid": "3", "boolean": True},
                                     "updated_at": time.time()}))
        result = self.run_gate(ORCA_GATE_BOARD=str(board))
        self.assertEqual(result.returncode, 3, result.stderr)

    def test_shared_worker_uses_strictest_cap_and_union_of_keys(self):
        self.config["agents"]["codex"].update(worker="shared", cap=4, board_keys=["one"])
        self.config["agents"]["devin"].update(worker="shared", cap=2, board_keys=["two", "one"])
        self.save_config()
        board = self.home / "board.json"
        board.write_text(json.dumps({"total": {"one": 1, "two": 1}, "updated_at": time.time()}))
        for agent in ("codex", "devin"):
            with self.subTest(agent=agent):
                result = self.run_gate(agent, ORCA_GATE_BOARD=str(board))
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("上限", result.stderr)
        del self.config["agents"]["codex"]["cap"]
        self.save_config()
        self.assertEqual(self.run_gate("codex", ORCA_GATE_BOARD=str(board)).returncode, 3)

    def test_missing_cli_exits_two(self):
        result = self.run_gate("unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("見つかりません", result.stderr)
        self.assertFalse(self.output.exists())


class RuntimeGateTests(unittest.TestCase):
    save_config = GateTests.save_config
    run_gate = GateTests.run_gate
    called = GateTests.called
    def setUp(self):
        GateTests.setUp(self)
        self.status = self.home / "Library/Application Support/quota-canvas/status.json"

    def write_status(self, verdict="go", worker="claude", age=0, path=None, **fields):
        path = path or self.status
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {"name": worker, "verdict": verdict, "reason": "test", "remaining": 42, "resets_at": None}
        item.update(fields)
        path.write_text(json.dumps({"updated_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
                                    "routing": {"workers": [item]}}))

    def configure_opendesign(self, code):
        self.cli.write_text("#!/usr/bin/python3\nimport json, os\n"
                            "print(json.dumps({'generated': os.environ.get('OPENDESIGN_API_KEY') == 'dummy-generated-key', "
                            "'existing': os.environ.get('OPENDESIGN_API_KEY') == 'dummy-existing-key', "
                            "'present': 'OPENDESIGN_API_KEY' in os.environ}))\n")
        self.config["agents"]["opencode"] = {
            "cli": str(self.cli), "worker": None, "standalone": True,
            "opendesign_key": {"worker": "opendesign", "command": ["/usr/bin/python3", "-c", code]},
        }
        self.save_config()

    def assert_opendesign_result(self, result, reason, generated=False, existing=False, present=False):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"generated": generated, "existing": existing, "present": present})
        log = (self.home / ".local/state/orca-agent-gate/launch.log").read_text()
        self.assertIn("opendesign=" + reason, log.splitlines()[-1])
        for value in ("dummy-generated-key", "dummy-existing-key", "dummy-command-marker"):
            self.assertNotIn(value, log)
            self.assertNotIn(value, result.stderr)

    def test_standalone_option_prepends_flag_once(self):
        self.cli.write_text("#!/usr/bin/python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        self.config["agents"]["opencode"] = {"cli": str(self.cli), "worker": None, "standalone": True}
        self.save_config()
        cases = [([], ["--standalone"]), (["run", "x"], ["--standalone", "run", "x"]),
                 (["run", "--standalone", "x"], ["run", "--standalone", "x"]),
                 (["--server", "http://127.0.0.1:1"], ["--server", "http://127.0.0.1:1"]),
                 (["--server=http://127.0.0.1:1"], ["--server=http://127.0.0.1:1"]),
                 (["run", "--", "--server"], ["--standalone", "run", "--", "--server"]),
                 (["-s", "run", "mini"], ["--standalone", "-s", "run", "mini"]),
                 (["--prompt", "models", "."], ["--standalone", "--prompt", "models", "."]),
                 (["/tmp/project"], ["--standalone", "/tmp/project"]),
                 (["models"], ["models"]), (["--standalone", "x"], ["--standalone", "x"]),
                 (["config"], ["--standalone", "config"]), (["debug", "config"], ["debug", "config"]),
                 (["--no-standalone"], ["--no-standalone"]), (["--standalone=false"], ["--standalone=false"]),
                 (["service", "stop"], ["service", "stop"]),
                 (["--completions", "bash"], ["--completions", "bash"]), (["--help"], ["--help"]),
                 (["--version"], ["--version"]), (["run", "-h"], ["run", "-h"]), (["--wizard"], ["--wizard"])]
        for args, expected in cases:
            result = self.run_gate("opencode", args)
            self.assertEqual((result.returncode, json.loads(result.stdout)), (0, expected), result.stderr)
        for value in (False, "yes"):
            self.config["agents"]["opencode"]["standalone"] = value
            self.save_config()
            result = self.run_gate("opencode", ["run"])
            self.assertEqual(json.loads(result.stdout), ["run"])
        self.assertIn("standalone", result.stderr)

    def test_opendesign_key_on_and_default_off(self):
        self.configure_opendesign("print('dummy-generated-key\\nignored') # dummy-command-marker")
        self.write_status("go", worker="opendesign")
        self.assert_opendesign_result(self.run_gate("opencode"), "on", generated=True, present=True)
        del self.config["agents"]["opencode"]["opendesign_key"]
        self.save_config()
        before = (self.home / ".local/state/orca-agent-gate/launch.log").read_text().splitlines()
        result = self.run_gate("opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["present"])
        after = (self.home / ".local/state/orca-agent-gate/launch.log").read_text().splitlines()
        self.assertNotIn("opendesign=", after[len(before)])

    def test_opendesign_stop_and_allow(self):
        self.configure_opendesign("print('dummy-generated-key') # dummy-command-marker")
        self.write_status("stop", worker="opendesign")
        self.assert_opendesign_result(self.run_gate("opencode"), "off(stop)")
        self.assertEqual(self.run_gate("allow", ["opendesign", "5"]).returncode, 0)
        self.assert_opendesign_result(self.run_gate("opencode"), "on", generated=True, present=True)

    def test_opendesign_existing_key_is_preserved(self):
        self.configure_opendesign("raise Exception('dummy-command-marker')")
        self.write_status("stop", worker="opendesign")
        result = self.run_gate("opencode", OPENDESIGN_API_KEY="dummy-existing-key")
        self.assert_opendesign_result(result, "off(already-set)", existing=True, present=True)

    def test_existing_key_is_hidden_from_check_and_passed_to_cli(self):
        self.configure_opendesign("raise Exception('should not run')")
        self.config["agents"]["opencode"]["worker"] = "opencode"
        self.save_config()
        check_output = self.home / "check-env.json"
        self.check.write_text("#!/usr/bin/python3\nimport json, os\n"
                              "with open(os.environ['CHECK_OUTPUT'], 'w') as f:\n"
                              " json.dump('OPENDESIGN_API_KEY' in os.environ, f)\n")
        result = self.run_gate("opencode", OPENDESIGN_API_KEY="dummy-existing-key",
                               CHECK_OUTPUT=str(check_output))
        self.assertFalse(json.loads(check_output.read_text()))
        self.assert_opendesign_result(result, "off(already-set)", existing=True, present=True)

    def test_key_command_does_not_inherit_parent_key(self):
        gate = runpy.run_path(str(GATE))
        settings = {"opendesign_key": {"worker": "opendesign", "command": [
            "/usr/bin/python3", "-c", "import os, sys; sys.exit(1) if 'OPENDESIGN_API_KEY' in os.environ else print('dummy-generated-key')"]}}
        target_env = {}
        with patch.dict(os.environ, {"OPENDESIGN_API_KEY": "dummy-existing-key"}):
            self.assertEqual(gate["opendesign_key"](settings, target_env, "private"), "on")
        self.assertEqual(target_env["OPENDESIGN_API_KEY"], "dummy-generated-key")

    def test_opendesign_output_and_key_limits(self):
        for code in ("print('dummy-generated-key'); print('x' * 8192)",
                     "print('x' * 1025)", "print('é' * 1025)"):
            with self.subTest(code=code):
                self.configure_opendesign(code)
                self.assert_opendesign_result(self.run_gate("opencode"), "off(command-failed)")
        for code in ("print('x' * 1024)", "print('x'); print('y' * 8189)"):
            with self.subTest(code=code):
                self.configure_opendesign(code)
                self.assert_opendesign_result(self.run_gate("opencode"), "on", present=True)

    def test_opendesign_fifo_status_does_not_block_launch(self):
        self.configure_opendesign("print('dummy-generated-key')")
        self.status.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.status)
        result = subprocess.run([str(GATE), "opencode"], env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=20)
        self.assert_opendesign_result(result, "on", generated=True, present=True)

    def test_opendesign_deeply_nested_status_does_not_block_launch(self):
        self.configure_opendesign("print('dummy-generated-key')")
        self.status.parent.mkdir(parents=True, exist_ok=True)
        self.status.write_text("[" * 200000 + "]" * 200000)
        result = subprocess.run([str(GATE), "opencode"], env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=20)
        self.assert_opendesign_result(result, "on", generated=True, present=True)

    def test_opendesign_fifo_allow_file_does_not_block_launch(self):
        self.configure_opendesign("print('dummy-generated-key')")
        self.write_status("stop", worker="opendesign")
        allow = self.home / ".local/state/orca-agent-gate/allow"
        allow.mkdir(parents=True, exist_ok=True)
        os.mkfifo(allow / "opendesign")
        result = subprocess.run([str(GATE), "opencode"], env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=20)
        self.assert_opendesign_result(result, "off(stop)")

    def test_opendesign_read_error_reaps_child(self):
        gate = runpy.run_path(str(GATE))
        settings = {"opendesign_key": {"worker": "opendesign", "command": [
            "/usr/bin/python3", "-c", "import time; print('x', flush=True); time.sleep(30)"]}}
        procs = []
        real_popen = subprocess.Popen
        def spy(*args, **kwargs):
            procs.append(real_popen(*args, **kwargs))
            return procs[-1]
        for target in ("register", "read"):
            with self.subTest(target=target):
                procs.clear()
                env = {}
                if target == "register":
                    patcher = patch("selectors.DefaultSelector.register", side_effect=OSError("dummy"))
                else:
                    real_read = os.read
                    def failing_read(fd, size):
                        if procs and fd == procs[0].stdout.fileno():
                            raise OSError("dummy")
                        return real_read(fd, size)
                    patcher = patch.object(os, "read", failing_read)
                with patch.object(subprocess, "Popen", spy), patcher:
                    started = time.monotonic()
                    self.assertEqual(gate["opendesign_key"](settings, env, "private"), "off(command-failed)")
                self.assertLess(time.monotonic() - started, 10)
                self.assertIsNotNone(procs[0].returncode)
                self.assertNotIn("OPENDESIGN_API_KEY", env)

    def test_opendesign_command_failures(self):
        for code, reason in (("import sys; print('dummy-generated-key', file=sys.stderr); sys.exit(1)", "command-failed"),
                             ("print('')", "empty"),
                             ("import time; time.sleep(30)", "timeout")):
            with self.subTest(reason=reason):
                self.configure_opendesign(code + " # dummy-command-marker")
                result = self.run_gate("opencode")
                self.assert_opendesign_result(result, "off(" + reason + ")")

    def hook_call(self, agent="claude", pane="pane-1", payload=None, **env):
        return subprocess.run([str(GATE), "hook", agent], env=dict(self.env, ORCA_PANE_KEY=pane, **env),
                              input=json.dumps(payload or {"session_id": "session-1"}),
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_worker_name_launch_verdict_and_hook_agree(self):
        for worker in ("a" * 40, "_worker", ".worker", "worker-name"):
            with self.subTest(worker=worker):
                self.config["agents"]["devin"]["worker"] = worker
                self.save_config()
                self.write_status("stop", worker=worker)
                launch = self.run_gate("devin")
                self.assertEqual(launch.returncode, 0, launch.stderr)
                self.assertEqual(self.called()["hook"], worker)
                verdict = self.run_gate("verdict", [worker])
                self.assertEqual(verdict.returncode, 0, verdict.stderr)
                self.assertEqual(json.loads(verdict.stdout)["verdict"], "stop")
                hook = self.hook_call("devin", ORCA_GATE_HOOK=worker)
                self.assertEqual(hook.returncode, 2, hook.stderr)
                self.assertIn(worker, hook.stderr)

        for worker in ("off", "a" * 41, "bad name", "-worker", "-"):
            with self.subTest(invalid=worker):
                self.assertEqual(self.run_gate("verdict", [worker]).returncode, 2)
                self.assertEqual(self.hook_call("devin", ORCA_GATE_HOOK=worker).returncode, 0)
                self.config["agents"]["devin"]["worker"] = worker
                self.save_config()
                hook = self.hook_call("devin")
                self.assertEqual((hook.returncode, hook.stdout, hook.stderr), (0, "", ""))

    def test_verdict_values_and_missing_worker(self):
        for verdict in ("stop", "reserve", "go", "boost", "unknown"):
            with self.subTest(verdict=verdict):
                self.write_status(verdict)
                result = self.run_gate("verdict", ["claude"])
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual((data["verdict"], data["remaining"], data["source"], data["stale"]),
                                 (verdict, 42, "local", False))
                self.assertIs(data["allowed"], False)
                self.assertNotIn("allow_until", data)
        self.assertEqual(json.loads(self.run_gate("verdict", ["codex"]).stdout)["verdict"], "unknown")
        self.assertEqual(self.run_gate("verdict", ["../bad"]).returncode, 2)

    def test_verdict_output_is_truncated_for_huge_reason(self):
        self.write_status("stop", reason="x" * 500000)
        result = self.run_gate("verdict", ["claude"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(len(result.stdout), 4096)
        data = json.loads(result.stdout)
        self.assertEqual((data["worker"], data["verdict"], data["allowed"]), ("claude", "stop", False))

    def test_extreme_allow_expiry_is_treated_as_no_allow(self):
        self.write_status("stop")
        path = self.home / ".local/state/orca-agent-gate/allow/claude"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("9999-12-31T23:59:59-23:59\n")
        data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
        self.assertEqual((data["verdict"], data["allowed"]), ("stop", False))
        self.assertNotIn("allow_until", data)
        self.assertEqual(self.hook_call().returncode, 2)
        result = self.run_gate("codex")
        self.assertNotIn("Traceback", result.stderr)

    def test_opendesign_key_only_for_private_server(self):
        marker = self.home / "key-command-ran"
        self.configure_opendesign("open({!r}, 'w').close(); print('dummy-generated-key')".format(str(marker)))
        self.write_status("go", worker="opendesign")
        for args in (["serve", "--service"], ["service", "start"], ["--server", "http://127.0.0.1:1"],
                     ["--server=http://127.0.0.1:1", "run"], ["--no-standalone"], ["--help"], ["models"]):
            self.assert_opendesign_result(self.run_gate("opencode", args), "off(shared-server)")
            self.assertFalse(marker.exists(), args)
        self.config["agents"]["opencode"]["standalone"] = False
        self.save_config()
        self.assert_opendesign_result(self.run_gate("opencode", ["run"]), "off(shared-server)")
        self.assertFalse(marker.exists())
        self.assert_opendesign_result(self.run_gate("opencode", ["serve", "--service"],
                                                    OPENDESIGN_API_KEY="dummy-existing-key"), "off(shared-server)")
        self.assertFalse(marker.exists())
        self.assert_opendesign_result(self.run_gate("opencode", ["run", "--standalone"]), "on",
                                      generated=True, present=True)
        self.assertTrue(marker.exists())

    def test_extreme_allow_expiry_keeps_opencode_launch_running(self):
        self.configure_opendesign("print('dummy-generated-key')")
        self.write_status("stop", worker="opendesign")
        allow = self.home / ".local/state/orca-agent-gate/allow/opendesign"
        allow.parent.mkdir(parents=True, exist_ok=True)
        allow.write_text("9999-12-31T23:59:59-23:59\n")
        result = self.run_gate("opencode")
        self.assertNotIn("Traceback", result.stderr)
        self.assert_opendesign_result(result, "off(stop)")

    def test_verdict_non_string_reason_is_stringified(self):
        self.write_status("stop", reason=12345)
        data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
        self.assertEqual((data["verdict"], data["allowed"], data["reason"]), ("stop", False, "12345"))

    def test_verdict_non_finite_and_falsy_fields_keep_stop(self):
        self.status.parent.mkdir(parents=True, exist_ok=True)
        self.status.write_text('{"updated_at": "%s", "routing": {"workers": [{"name": "claude", '
                               '"verdict": "stop", "reason": 0, "remaining": 1e999, "resets_at": null}]}}'
                               % datetime.now(timezone.utc).isoformat())
        result = self.run_gate("verdict", ["claude"])
        self.assertNotIn("Infinity", result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual((data["verdict"], data["allowed"], data["reason"], data["remaining"]), ("stop", False, "0", None))

    def test_stale_remote_override_and_burn_expiry(self):
        self.write_status(age=1201)
        data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
        self.assertEqual((data["verdict"], data["stale"], data["source"]), ("unknown", True, "none"))
        remote = self.status.parent / "remote/status.json"
        self.write_status("boost", path=remote, reason="burn",
                          burn_until=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat())
        data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
        self.assertEqual((data["verdict"], data["reason"], data["source"]), ("reserve", "burn_expired", "remote"))
        data = json.loads(self.run_gate("verdict", ["claude"], QUOTA_STATUS_PATH=str(self.status)).stdout)
        self.assertEqual((data["stale"], data["source"]), (True, "none"))
        data = json.loads(self.run_gate("verdict", ["claude"], QUOTA_STATUS_PATH=str(remote)).stdout)
        self.assertEqual(data["source"], "env")

    def test_expired_burn_only_changes_boost(self):
        for verdict in ("stop", "reserve", "go", "boost", "unknown"):
            with self.subTest(verdict=verdict):
                self.write_status(verdict, reason="burn",
                                  burn_until=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat())
                data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
                expected = ("reserve", "burn_expired") if verdict == "boost" else (verdict, "burn")
                self.assertEqual((data["verdict"], data["reason"]), expected)

    def test_invalid_verdict_falls_back_but_missing_reason_does_not(self):
        remote = self.status.parent / "remote/status.json"
        self.write_status("stop", path=remote)
        for item in ({"name": "claude"}, {"name": "claude", "verdict": "bad"},
                     {"name": "claude", "verdict": None}, {"name": "claude", "verdict": []}):
            with self.subTest(item=item):
                self.status.parent.mkdir(parents=True, exist_ok=True)
                self.status.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(),
                                                   "routing": {"workers": [item]}}))
                data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
                self.assertEqual((data["verdict"], data["source"]), ("stop", "remote"))
        self.write_status("go")
        local = json.loads(self.status.read_text())
        del local["routing"]["workers"][0]["reason"]
        self.status.write_text(json.dumps(local))
        data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
        self.assertEqual((data["verdict"], data["source"], data["reason"]), ("go", "local", ""))

    def test_hook_does_not_wait_for_state_lock(self):
        self.write_status("stop")
        directory = self.home / ".local/state/orca-agent-gate/hook"
        directory.mkdir(parents=True)
        with (directory / "pane-1.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = subprocess.run([str(GATE), "hook", "claude"],
                                    env=dict(self.env, ORCA_PANE_KEY="pane-1"), input="{}",
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((directory / "pane-1.json").exists())

    def test_hook_outside_orca_and_stop_transition(self):
        self.write_status("stop")
        result = self.hook_call(pane="")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        for verdict, code in (("go", 0), ("stop", 2), ("go", 0)):
            self.write_status(verdict)
            result = self.hook_call()
            self.assertEqual(result.returncode, code, result.stderr)
            if code == 2:
                self.assertIn("送信していません", result.stderr)
                self.assertIn("orca-agent-gate allow claude", result.stderr)
        log = (self.home / ".local/state/orca-agent-gate/hook.log").read_text()
        self.assertEqual(len(log.splitlines()), 1)
        self.assertNotIn("session-1", log)

    def test_reserve_warns_once_per_transition(self):
        for verdict, warning in (("reserve", True), ("reserve", False), ("go", False), ("reserve", True)):
            self.write_status(verdict)
            result = self.hook_call()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(bool(result.stdout), warning)
            if warning:
                self.assertIn("reserve", json.loads(result.stdout)["systemMessage"])

    def test_unknown_defaults_and_config_overrides(self):
        self.assertEqual(self.hook_call("claude").returncode, 0)
        self.assertEqual(self.hook_call("devin").returncode, 2)
        self.config["agents"]["devin"]["hook_block_unknown"] = False
        self.config["agents"]["claude"] = {"hook_block_unknown": True}
        self.save_config()
        self.assertEqual(self.hook_call("devin").returncode, 0)
        self.assertEqual(self.hook_call("claude").returncode, 2)
        self.config["agents"]["claude"]["hook"] = False
        self.save_config()
        self.assertEqual(self.hook_call("claude").returncode, 0)

    def test_allow_active_expired_and_off(self):
        self.write_status("stop")
        self.assertEqual(self.run_gate("allow", ["claude", "5"]).returncode, 0)
        data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
        self.assertIs(data["allowed"], True)
        until = datetime.fromisoformat(data["allow_until"])
        self.assertEqual(until.utcoffset(), timedelta(0))
        self.assertGreater(until, datetime.now(timezone.utc))
        first = self.hook_call()
        self.assertEqual(first.returncode, 0)
        self.assertIn("一時許可中", json.loads(first.stdout)["systemMessage"])
        self.assertEqual(self.hook_call().stdout, "")
        path = self.home / ".local/state/orca-agent-gate/allow/claude"
        path.write_text((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
        self.assertIs(data["allowed"], False)
        self.assertNotIn("allow_until", data)
        self.assertEqual(self.hook_call().returncode, 2)
        self.assertEqual(self.run_gate("allow", ["claude", "off"]).returncode, 0)
        self.assertFalse(path.exists())
        self.assertEqual(self.run_gate("allow", ["claude", "721"]).returncode, 2)

    def test_hook_override_and_cleanup(self):
        self.write_status("reserve", worker="codex")
        result = self.hook_call(ORCA_GATE_HOOK="codex")
        self.assertIn("reserve", json.loads(result.stdout)["systemMessage"])
        self.assertEqual(self.hook_call(ORCA_GATE_HOOK="codex").stdout, "")
        directory = self.home / ".local/state/orca-agent-gate/hook"
        old = directory / "old.json"
        old.write_text("{}")
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        self.hook_call(ORCA_GATE_HOOK="codex")
        self.assertFalse(old.exists())
        self.assertEqual(self.hook_call(ORCA_GATE_HOOK="off").stdout, "")
        self.write_status("stop")
        result = self.hook_call(pane="", ORCA_GATE_HOOK="claude")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_unknown_allow_and_target_settings(self):
        self.assertEqual(self.run_gate("allow", ["swe2"]).returncode, 0)
        result = self.hook_call("devin")
        self.assertEqual(result.returncode, 0)
        self.assertIn("一時許可中", json.loads(result.stdout)["systemMessage"])
        self.config["agents"]["devin"]["off"] = True
        self.save_config()
        self.assertEqual(self.hook_call("devin").stdout, "")
        self.config["agents"]["devin"]["off"] = False
        self.config["agents"]["devin"]["worker"] = None
        self.save_config()
        self.assertEqual(self.hook_call("devin").stdout, "")
        self.assertEqual(self.hook_call("devin", ORCA_GATE_HOOK="off").returncode, 0)

    def test_state_write_failure_does_not_change_decision(self):
        # A directory at the state-file location forces an I/O error without using real state.
        for agent, worker, verdict, expected in (("claude", "claude", "go", 0),
                                                  ("devin", "swe2", "go", 0),
                                                  ("devin", "swe2", "stop", 2)):
            with self.subTest(agent=agent, verdict=verdict):
                self.write_status(verdict, worker=worker)
                directory = self.home / ".local/state/orca-agent-gate/hook"
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / (agent + ".json")
                path.mkdir(exist_ok=True)
                result = self.hook_call(agent, pane=agent)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_log_write_failure_does_not_change_decision(self):
        directory = self.home / ".local/state/orca-agent-gate/hook.log"
        directory.mkdir(parents=True)
        for verdict, expected in (("stop", 2), ("reserve", 0), ("go", 0)):
            with self.subTest(verdict=verdict):
                self.write_status(verdict, worker="swe2")
                result = self.hook_call("devin")
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_malformed_and_missing_routing_fall_back(self):
        self.status.parent.mkdir(parents=True, exist_ok=True)
        remote = self.status.parent / "remote/status.json"
        self.write_status("go", path=remote)
        now = datetime.now(timezone.utc)
        for contents in ("{", json.dumps({"updated_at": now.isoformat(), "routing": []}),
                         json.dumps({"updated_at": now.isoformat(), "routing": {"workers": {}}}),
                         json.dumps({"updated_at": now.isoformat(), "routing": {"workers": [{"name": "other"}]}}),
                         json.dumps({"updated_at": (now + timedelta(minutes=6)).isoformat(),
                                     "routing": {"workers": [{"name": "claude", "verdict": "stop"}]}})):
            with self.subTest(contents=contents):
                self.status.write_text(contents)
                data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
                self.assertEqual((data["verdict"], data["source"]), ("go", "remote"))

    def test_stale_worker_is_unknown(self):
        self.write_status("stop", stale=True)
        data = json.loads(self.run_gate("verdict", ["claude"]).stdout)
        self.assertEqual((data["verdict"], data["stale"], data["source"]), ("unknown", True, "local"))
        self.assertEqual(self.hook_call().returncode, 0)
        self.config["agents"]["claude"] = {"hook_block_unknown": True}
        self.save_config()
        self.assertEqual(self.hook_call().returncode, 2)

    def test_launch_hook_environment(self):
        self.assertEqual(self.run_gate().returncode, 0)
        self.assertEqual(self.called()["hook"], "codex")
        self.assertEqual(self.run_gate(ORCA_GATE_FORCE="1").returncode, 0)
        self.assertEqual(self.called()["hook"], "off")
        self.assertEqual(self.run_gate(ORCA_GATE_HOOK="off").returncode, 0)
        self.assertEqual(self.called()["hook"], "off")
        self.assertEqual(self.run_gate(ORCA_GATE_HOOK="other").returncode, 0)
        self.assertEqual(self.called()["hook"], "codex")
        self.assertEqual(self.run_gate(ORCA_GATE_HOOK="other", ORCA_GATE_FORCE="1").returncode, 0)
        self.assertEqual(self.called()["hook"], "off")
        for setting in ({"worker": None}, {"off": True}):
            with self.subTest(setting=setting):
                self.config["agents"]["codex"] = {"cli": str(self.cli), **setting}
                self.save_config()
                for inherited, expected in (("off", "off"), ("other", None), ("", None)):
                    with self.subTest(inherited=inherited):
                        self.assertEqual(self.run_gate(ORCA_GATE_HOOK=inherited).returncode, 0)
                        self.assertEqual(self.called()["hook"], expected)
                self.assertEqual(self.run_gate().returncode, 0)
                self.assertIsNone(self.called()["hook"])


if __name__ == "__main__":
    unittest.main()
