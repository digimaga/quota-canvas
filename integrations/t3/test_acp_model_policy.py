import pathlib
import subprocess
import sys
import unittest

from acp_model_policy import Relay


def request(number, method, **params):
    return {"id": number, "method": f"session/{method}", "params": {"sessionId": "s", **params}}


class ModelPolicyTest(unittest.TestCase):
    def test_resume_tracks_legacy_model_and_custom_selector(self):
        relay = Relay({"allowed": {}, "alternate": {}})
        relay.handle_client(request(1, "load"))
        result = relay.transform_upstream({"id": 1, "result": {
            "models": {"currentModelId": "allowed", "availableModels": ["allowed", "blocked"]},
            "configOptions": [{"id": "engine", "category": "model", "options": ["allowed", "blocked"]}],
        }})["result"]
        self.assertEqual(result["models"]["availableModels"], ["allowed"])
        self.assertEqual(result["configOptions"][0]["options"], ["allowed"])
        self.assertIsNone(relay.handle_client(request(2, "prompt"))[1])
        self.assertIsNone(relay.handle_client(request(3, "prompt"))[1])
        self.assertIsNone(relay.handle_client(request(4, "set_config_option", configId="engine", value="blocked"))[0])
        self.assertIsNone(relay.handle_client(request(5, "set_config_option", configId="engine", value="alternate"))[1])
        relay.transform_upstream({"id": 5, "result": None})
        self.assertEqual(relay.sessions["s"], "alternate")
        relay.handle_client(request(6, "set_model", modelId="allowed"))
        relay.transform_upstream({"id": 6, "error": {"message": "failed"}})
        self.assertEqual(relay.sessions["s"], "alternate")

    def test_upstream_exit_is_reported_while_client_input_remains_open(self):
        relay = pathlib.Path(__file__).with_name("acp_model_policy.py")
        with subprocess.Popen(
            [sys.executable, str(relay), "--policy-json", '["allowed"]', "--",
             sys.executable, "-c", "raise SystemExit(17)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        ) as process:
            try:
                self.assertEqual(process.wait(timeout=5), 17)
            finally:
                if process.poll() is None:
                    process.kill()
