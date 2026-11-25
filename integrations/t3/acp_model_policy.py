#!/usr/bin/env python3
"""A dependency-free ACP JSON-RPC relay that enforces an allowed model catalog.

Example:
  python3 acp_model_policy.py --policy-json '{"models":{"deepseek/x":{"label":"DeepSeek","reasoningEfforts":["high"]}}}' -- command acp

The process after ``--`` is the real ACP server.  Messages are newline-delimited
JSON-RPC.  The relay filters advertised catalogs and refuses disallowed model
switches before they reach that server.
"""

import argparse
import json
import signal
import subprocess
import sys
import threading


BLOCKED = -32001


def option_id(value):
    """Return a model identifier from the common ACP option shapes."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    for key in ("modelId", "value", "id", "model"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def normalize_policy(raw):
    """Accept a list of IDs or a mapping of ID to optional presentation data."""
    shared = raw.get("sharedOptions", []) if isinstance(raw, dict) else []
    models = raw.get("models", raw.get("allowedModels", raw)) if isinstance(raw, dict) else raw
    if isinstance(models, list):
        result = dict((model, {}) for model in models if isinstance(model, str))
        if not result:
            raise ValueError("policy has no allowed model IDs")
        return result, set(shared if isinstance(shared, list) else [])
    if not isinstance(models, dict):
        raise ValueError("policy must contain a model ID list or model mapping")
    result = {}
    for model, metadata in models.items():
        if isinstance(model, str):
            result[model] = metadata if isinstance(metadata, dict) else {}
    if not result:
        raise ValueError("policy has no allowed model IDs")
    return result, set(shared if isinstance(shared, list) else [])


class Relay(object):
    def __init__(self, policy, shared_options=None):
        self.policy = policy
        self.shared_options = set(shared_options or [])
        self.pending = {}
        self.sessions = {}
        self.model_option_ids = set(["model", "model_id", "modelId"])

    def allowed(self, model):
        return self.metadata_for(model) is not None

    def metadata_for(self, model):
        """Match both the picker ID and an adapter's forwarded upstream ID."""
        if not isinstance(model, str):
            return None
        if model in self.policy:
            return self.policy[model]
        for metadata in self.policy.values():
            upstream = metadata.get("upstreamIds", metadata.get("upstreamId"))
            if isinstance(upstream, str) and model == upstream:
                return metadata
            if isinstance(upstream, list) and model in upstream:
                return metadata
        return None

    def session_id(self, message):
        for container in (message.get("params"), message.get("result")):
            if isinstance(container, dict):
                for key in ("sessionId", "id"):
                    value = container.get(key)
                    if isinstance(value, str):
                        return value
        return None

    def remember_request(self, message):
        request_id = message.get("id")
        if request_id is None or "method" not in message:
            return message
        self.pending[self.key(request_id)] = {
            "method": message.get("method"), "session": self.session_id(message),
            "requested_model": self.request_model(message) if self.is_model_switch(message) else None,
        }
        return message

    @staticmethod
    def key(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def request_params(message):
        params = message.get("params")
        return params if isinstance(params, dict) else {}

    def request_model(self, message):
        params = self.request_params(message)
        for key in ("modelId", "model", "value"):
            value = params.get(key)
            model = option_id(value)
            if model:
                return model
        return None

    def blocked_request(self, message):
        method = message.get("method")
        session = self.session_id(message)
        if method == "session/prompt":
            return session is None or not self.allowed(self.sessions.get(session))
        return self.is_model_switch(message) and not self.allowed(self.request_model(message))

    def is_model_switch(self, message):
        method = message.get("method")
        if method == "session/set_model":
            return True
        if method == "session/set_config_option":
            return self.request_params(message).get("configId") in self.model_option_ids
        return False

    def reject(self, message):
        if "id" not in message:
            return None
        return {"jsonrpc": "2.0", "id": message.get("id"), "error": {
            "code": BLOCKED, "message": "Model is not permitted by this ACP policy."
        }}

    def decorate_model(self, item):
        if not isinstance(item, dict):
            return item
        model = option_id(item)
        metadata = self.metadata_for(model)
        if metadata is None:
            return item
        result = dict(item)
        label = metadata.get("label")
        if isinstance(label, str):
            result["label"] = label
            if "name" in result:
                result["name"] = label
        efforts = metadata.get("reasoningEfforts")
        if isinstance(efforts, list) and efforts:
            normalized = []
            for effort in efforts:
                normalized.append(effort if isinstance(effort, dict) else {"value": effort, "label": str(effort)})
            meta = dict(result.get("_meta") or {})
            meta["supportsReasoningEffort"] = True
            meta["reasoningEfforts"] = normalized
            current = meta.get("reasoningEffort")
            choices = [entry.get("value") for entry in normalized if isinstance(entry, dict)]
            default = metadata.get("reasoningEffort", metadata.get("defaultReasoningEffort"))
            meta["reasoningEffort"] = current if current in choices else (default if default in choices else choices[0])
            result["_meta"] = meta
        return result

    def filter_options(self, value, model_category=False):
        if isinstance(value, list):
            filtered = [self.filter_options(item, model_category) for item in value]
            return [item for item in filtered if item is not None]
        if not isinstance(value, dict):
            return value if not model_category or self.allowed(option_id(value)) else None
        category = value.get("category")
        config_id = value.get("id")
        if category == "model" and isinstance(config_id, str):
            self.model_option_ids.add(config_id)
        if config_id in self.shared_options:
            # T3 treats model_config as model-specific.  A shared speed picker
            # must remain an ordinary config option so all catalog entries keep it.
            result = dict(value)
            result.pop("category", None)
            return result
        is_model = model_category or category == "model" or config_id in self.model_option_ids
        if "options" in value and isinstance(value["options"], list):
            result = dict(value)
            result["options"] = self.filter_options(value["options"], is_model)
            return result
        if "choices" in value and isinstance(value["choices"], list):
            result = dict(value)
            result["choices"] = self.filter_options(value["choices"], is_model)
            return result
        if is_model:
            model = option_id(value)
            return self.decorate_model(value) if self.allowed(model) else None
        return value

    def model_select(self, options):
        """Find the model selector, including a grouped configOptions tree."""
        if not isinstance(options, list):
            return None
        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("category") == "model" or option.get("id") in self.model_option_ids:
                return option
            nested = self.model_select(option.get("options")) or self.model_select(option.get("choices"))
            if nested is not None:
                return nested
        return None

    def synthesize_legacy_models(self, payload):
        """Give older T3 normalizers the legacy per-model metadata shape."""
        if not isinstance(payload, dict) or "models" in payload or "availableModels" in payload:
            return payload
        selector = self.model_select(payload.get("configOptions"))
        if not isinstance(selector, dict):
            return payload
        options = selector.get("options", selector.get("choices", []))
        if not isinstance(options, list):
            return payload
        available = []
        for option in options:
            if isinstance(option, dict) and option_id(option):
                decorated = self.decorate_model(option)
                available.append({
                    "modelId": option_id(option),
                    "name": decorated.get("label", decorated.get("name", option_id(option))),
                    "_meta": decorated.get("_meta", {}),
                })
        if not available:
            return payload
        current = option_id(selector.get("value", selector.get("currentValue")))
        result = dict(payload)
        result["models"] = {"currentModelId": current, "availableModels": available}
        return result

    def filter_legacy(self, value, is_entry=False):
        if isinstance(value, list):
            return [item for item in (self.filter_legacy(item, True) for item in value) if item is not None]
        if not isinstance(value, dict):
            return value if not is_entry or self.allowed(option_id(value)) else None
        model = option_id(value)
        if model is not None:
            return self.decorate_model(value) if self.allowed(model) else None
        result = {}
        for key, item in value.items():
            if key in ("availableModels", "models"):
                result[key] = self.filter_legacy(item)
            else:
                result[key] = item
        return result

    def observe_model(self, value, session):
        if not session or not isinstance(value, dict):
            return False
        selector = self.model_select(value.get("configOptions")) or {}
        models = value.get("models") or value.get("modelState")
        model = option_id(selector.get("currentValue", selector.get("value"))) or option_id(value.get("currentModelId"))
        if model is None and isinstance(models, dict):
            model = option_id(models.get("currentModelId"))
        if model is None:
            return False
        self.sessions[session] = model
        return True

    def transform_upstream(self, message):
        if not isinstance(message, dict):
            return message
        # Upstream requests may use an ID too; only a response has no method.
        pending = self.pending.pop(self.key(message.get("id")), None) if "id" in message and "method" not in message else None
        payload_key = "result" if "result" in message else "params"
        payload = message.get(payload_key)
        result = dict(message)
        session = self.session_id(message) or (pending or {}).get("session")
        observed = False
        if isinstance(payload, dict):
            transformed = self.transform_payload(payload)
            # ACP notifications commonly carry the changed options inside update.
            update = transformed.get("update")
            if isinstance(update, dict):
                transformed["update"] = self.transform_payload(update)
            observed = self.observe_model(transformed, session)
            if isinstance(update, dict):
                observed = self.observe_model(transformed["update"], session) or observed
            result[payload_key] = transformed
        if not observed and pending and "result" in message:
            requested = pending.get("requested_model")
            if session and self.allowed(requested):
                # Some v1 ACP servers acknowledge set_model with an empty result.
                self.sessions[session] = requested
        return result

    def transform_payload(self, payload):
        """Apply the same filtering to nested config_option_update payloads."""
        transformed = dict(payload)
        for key in ("configOptions",):
            if isinstance(transformed.get(key), list):
                transformed[key] = self.filter_options(transformed[key])
        for key in ("availableModels", "models", "modelState"):
            if key in transformed:
                filtered = self.filter_legacy(transformed[key])
                if filtered is not None:
                    transformed[key] = filtered
        return self.synthesize_legacy_models(transformed)

    def handle_client(self, message):
        if not isinstance(message, dict):
            return message, None
        if self.blocked_request(message):
            return None, self.reject(message)
        method = message.get("method")
        if method in ("session/close", "session/delete"):
            session = self.session_id(message)
            if session:
                self.sessions.pop(session, None)
        return self.remember_request(message), None


def json_line(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\n"


def run(policy, command):
    relay = Relay(policy[0], policy[1])
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=1,
                             universal_newlines=True)
    write_lock = threading.Lock()
    state_lock = threading.Lock()

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)

    def emit(line):
        with write_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def forward_client():
        try:
            with child.stdin:
                for line in sys.stdin:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        child.stdin.write(line)
                        child.stdin.flush()
                        continue
                    with state_lock:
                        forward, rejection = relay.handle_client(value)
                    if rejection is not None:
                        emit(json_line(rejection))
                    elif forward is not None:
                        child.stdin.write(json_line(forward))
                        child.stdin.flush()
        except BrokenPipeError:
            pass  # The main thread drains any last upstream messages and exits.

    threading.Thread(target=forward_client, daemon=True).start()
    try:
        for line in child.stdout:
            try:
                value = json.loads(line)
            except ValueError:
                emit(line)
                continue
            with state_lock:
                result = relay.transform_upstream(value)
            emit(json_line(result))
    finally:
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return child.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enforce an ACP allowed-model policy")
    parser.add_argument("--policy-json", required=True, help="JSON list or mapping of permitted model IDs")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Real ACP command after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("real ACP command is required after --")
    try:
        policy = normalize_policy(json.loads(args.policy_json))
    except (TypeError, ValueError) as exc:
        parser.error("invalid policy: %s" % exc)
    return run(policy, command)


if __name__ == "__main__":
    sys.exit(main())
