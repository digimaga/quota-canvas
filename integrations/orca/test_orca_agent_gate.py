import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

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
        self.cli.write_text("#!/usr/bin/python3\nimport json, os, sys, time\nwith open(os.environ['TEST_OUTPUT'], 'w') as f:\n json.dump({'argv': sys.argv[1:], 'force': os.environ.get('ORCA_GATE_FORCE')}, f)\ntime.sleep(float(os.environ.get('TEST_HOLD', '0')))\n")
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


if __name__ == "__main__":
    unittest.main()
