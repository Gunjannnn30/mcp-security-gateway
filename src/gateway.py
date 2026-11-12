"""MCP Security Gateway.

A lightweight security layer placed between an AI agent and its MCP tools.
It proxies MCP JSON-RPC messages over stdio and, on every tool call, runs:

1. Policy-based permission check          (what the tool is allowed to do)
2. Text scan of description + arguments   (what the tool claims / hidden instructions)
3. Forward the call to the downstream tool
4. Runtime behaviour observation          (what the tool actually does)
5. Text scan of the tool response         (manipulative content returned)
6. ML anomaly scoring                     (how unusual the behaviour is)

Then a combined risk decision (allow / warn / block) is made and logged.

Usage:
    python src/gateway.py                 # proxies the default safe weather tool
    MCP_DOWNSTREAM_CMD="python tools/unsafe_tool.py" python src/gateway.py
"""

from __future__ import annotations

import json
import os
import psutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Make the src package importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from decision import ALLOW, BLOCK, WARN, DecisionEngine  # noqa: E402
from logger import SecurityLogger  # noqa: E402
from monitor import RuntimeMonitor, fnmatch  # noqa: E402
from anomaly import AnomalyDetector  # noqa: E402
from policy import PolicyEngine  # noqa: E402
from text_detector import TextDetector  # noqa: E402

DEFAULT_DOWNSTREAM = ["python", os.path.join("tools", "safe_weather.py")]
PROTOCOL_VERSION = "2024-11-05"
GATEWAY_NAME = "mcp-security-gateway"
GATEWAY_VERSION = "0.1.0"


def log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class DownstreamClient:
    """Spawns the downstream MCP tool per request, runs a small JSON-RPC
    exchange (initialize -> optional notification -> request), and observes
    the tool process at runtime via psutil. Spawning per request keeps pipes
    short-lived and avoids cross-platform pipe hangs."""

    def __init__(self, cmd: List[str]) -> None:
        self.cmd = cmd
        self._msg_id = 0
        self.events: List[Any] = []

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _observe(self, proc: "psutil.Process", watch_paths: List[str]) -> None:
        try:
            for conn in proc.connections(kind="inet"):
                if conn.status == psutil.CONN_ESTABLISHED and conn.raddr:
                    self.events.append(
                        RuntimeEvent(
                            time.time(),
                            "network_connect",
                            f"{conn.raddr.ip}:{conn.raddr.port}",
                            proc.pid,
                        )
                    )
        except psutil.Error:
            pass
        try:
            for f in proc.open_files():
                path = f.path
                if watch_paths == ["*"] or any(fnmatch(path, wp) for wp in watch_paths):
                    self.events.append(
                        RuntimeEvent(time.time(), "file_access", path, proc.pid)
                    )
        except psutil.Error:
            pass

    def _monitor(self, proc: subprocess.Popen, stop: threading.Event, watch_paths: List[str]) -> None:
        while not stop.is_set():
            try:
                p = psutil.Process(proc.pid)
                self._observe(p, watch_paths)
                for child in p.children(recursive=True):
                    self._observe(child, watch_paths)
            except psutil.Error:
                pass
            stop.wait(0.15)

    def exchange(
        self,
        exchanges: List[tuple],
        *,
        watch_paths: Optional[List[str]] = None,
        timeout: float = 30.0,
    ) -> List[Optional[Dict[str, Any]]]:
        """Run a sequence of JSON-RPC exchanges against a fresh downstream
        process. Each exchange is (method, params, expect_response).
        Returns one response per expect_response=True exchange (None otherwise).
        Runtime events are collected into self.events."""
        self.events = []
        watch_paths = watch_paths or []
        proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stop = threading.Event()
        mon = threading.Thread(target=self._monitor, args=(proc, stop, watch_paths), daemon=True)
        mon.start()

        responses: List[Optional[Dict[str, Any]]] = []
        try:
            for method, params, expect_response in exchanges:
                payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
                if expect_response:
                    payload["id"] = self._next_id()
                assert proc.stdin
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
                if expect_response:
                    assert proc.stdout
                    line = proc.stdout.readline()
                    responses.append(json.loads(line) if line and line.strip() else None)
                else:
                    responses.append(None)
        finally:
            stop.set()
            mon.join(timeout=5)
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return responses

    def initialize_and_list(self) -> List[Dict[str, Any]]:
        init_params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": GATEWAY_NAME, "version": GATEWAY_VERSION},
        }
        responses = self.exchange(
            [
                ("initialize", init_params, True),
                ("initialized", {}, False),
                ("tools/list", {}, True),
            ]
        )
        list_resp = responses[2] or {}
        return (list_resp.get("result") or {}).get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any], watch_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        init_params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": GATEWAY_NAME, "version": GATEWAY_VERSION},
        }
        responses = self.exchange(
            [
                ("initialize", init_params, True),
                ("initialized", {}, False),
                ("tools/call", {"name": name, "arguments": arguments}, True),
            ],
            watch_paths=watch_paths,
        )
        return responses[2] or {}

    def close(self) -> None:
        pass  # each exchange spawns and tears down its own process


import threading  # noqa: E402  (used by DownstreamClient)



def extract_operation_resource(
    tool_name: str,
    arguments: Dict[str, Any],
    allowed_ops: Dict[str, list] | None = None,
) -> Tuple[str, str]:
    """Heuristically map tool call arguments to (operation, resource).

    network  -> url / endpoint
    file     -> path / file / filename
    process  -> command / cmd / script

    When no keyword matches, fall back to the tool's first declared allowed
    operation (e.g. a weather tool declares network use; a poisoned tool
    declares "other"), since the target resource often isn't in the arguments.
    """
    allowed_ops = allowed_ops or {}
    args = json.dumps(arguments).lower()
    if "command" in args or "cmd" in args or "script" in args:
        return ("process", arguments.get("command") or arguments.get("cmd") or "")
    if "path" in args or "file" in args or "filename" in args:
        return (
            "file",
            arguments.get("path")
            or arguments.get("file")
            or arguments.get("filename")
            or "",
        )
    if "url" in args or "endpoint" in args or "api" in args:
        return (
            "network",
            arguments.get("url") or arguments.get("endpoint") or "",
        )
    # Undetermined: fall back to the tool's own declared allowance.
    for op, resources in allowed_ops.items():
        if resources:
            return (op, "")
    return ("other", "")


def anomaly_features(
    tool_name: str,
    call_count: int,
    arguments: Dict[str, Any],
    response_text: str,
    events: List[Any],
) -> Dict[str, float]:
    net = sum(1 for e in events if getattr(e, "event_type", "") == "network_connect")
    file_acc = sum(1 for e in events if getattr(e, "event_type", "") == "file_access")
    proc_spawn = sum(1 for e in events if getattr(e, "event_type", "") == "process_spawn")
    return {
        "call_frequency": float(call_count),
        "request_size": float(len(json.dumps(arguments))),
        "response_size": float(len(response_text)),
        "network_connections": float(net),
        "file_accesses": float(file_acc),
        "process_spawns": float(proc_spawn),
    }


class Gateway:
    def __init__(self, downstream_cmd: List[str]) -> None:
        self.downstream_cmd = downstream_cmd
        self.client = DownstreamClient(downstream_cmd)
        self.policy = PolicyEngine()
        self.text_detector = TextDetector()
        self.monitor = RuntimeMonitor()
        self.anomaly = AnomalyDetector()
        self.decision = DecisionEngine(
            anomaly_threshold=0.3,
            text_malicious_action="block",
        )
        self.logger = SecurityLogger()
        self.tools_cache: List[Dict[str, Any]] = []
        self.call_counts: Dict[str, int] = {}
        # Anomaly training state: collect normal behaviour samples, then train.
        self.normal_samples: List[Dict[str, float]] = []
        self.anomaly_trained = False
        self.ANOMALY_TRAIN_EVERY = 10  # retrain after this many normal samples

    def run_setup(self) -> None:
        """Prime the gateway: initialize + discover downstream tools.

        Used by the HTTP/SSE transport which drives messages one at a time
        instead of the blocking stdin loop.
        """
        self.tools_cache = self.client.initialize_and_list()
        log_stderr(f"[gateway] discovered {len(self.tools_cache)} downstream tool(s)")

    def run(self) -> None:
        log_stderr(f"[gateway] starting {GATEWAY_NAME} v{GATEWAY_VERSION}")
        self.run_setup()

        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = message.get("method")
        msg_id = message.get("id")

        # Notifications have no id; acknowledge silently.
        if msg_id is None:
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": GATEWAY_NAME, "version": GATEWAY_VERSION},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": self.tools_cache}}
        if method == "tools/call":
            return self._handle_call(message)
        # Forward anything else transparently to a fresh downstream instance.
        responses = self.client.exchange([(method, message.get("params"), True)])
        return responses[0] or {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}

    def _handle_call(self, message: Dict[str, Any]) -> Dict[str, Any]:
        msg_id = message.get("id")
        params = message.get("params") or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        # Resolve the downstream tool description (for text scanning).
        description = ""
        for t in self.tools_cache:
            if t.get("name") == tool_name:
                description = t.get("description", "") or ""
                break

        # ---- Pre-execution checks ----
        policy = self.policy.get_policy(tool_name)
        allowed_ops = (policy.get("allow") or {}) if policy else {}
        operation, resource = extract_operation_resource(
            tool_name, arguments, allowed_ops
        )
        policy_allowed, policy_reason = self.policy.check(tool_name, operation, resource)

        text_suspicious, text_reason = self.text_detector.scan_tool_description(description)
        args_suspicious, args_reason = self.text_detector.scan(json.dumps(arguments))

        if not policy_allowed:
            self._log(
                tool=tool_name,
                operation=operation,
                decision=BLOCK,
                reason=policy_reason,
                policy_result=policy_reason,
            )
            return self._error_response(msg_id, f"blocked: {policy_reason}")

        if text_suspicious or args_suspicious:
            reason = text_reason or args_reason
            self._log(
                tool=tool_name,
                operation=operation,
                decision=BLOCK,
                reason=f"text: {reason}",
                policy_result="allowed",
                text_result=reason,
            )
            return self._error_response(msg_id, f"blocked: {reason}")

        # ---- Execute downstream tool while observing runtime behaviour ----
        self.call_counts[tool_name] = self.call_counts.get(tool_name, 0) + 1
        call_count = self.call_counts[tool_name]

        watch_paths = ["*"] if operation == "file" else []
        response = self.client.call_tool(tool_name, arguments, watch_paths=watch_paths)
        events = list(self.client.events)

        response_text = ""
        if isinstance(response, dict):
            result = response.get("result") or {}
            content = result.get("content") or []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        response_text += c.get("text", "")

        # ---- Post-execution checks ----
        resp_text_suspicious, resp_text_reason = self.text_detector.scan_tool_response(response_text)
        features = anomaly_features(tool_name, call_count, arguments, response_text, events)
        anomaly_anomalous, anomaly_score = self.anomaly.is_anomalous(features)
        runtime_suspicious = any(
            getattr(e, "event_type", "") == "file_access"
            and operation != "file"
            for e in events
        )
        runtime_detail = "; ".join(
            f"{getattr(e,'event_type','?')}:{getattr(e,'detail','')}" for e in events[:5]
        ) or "none"

        decision, reason, signals = self.decision.decide(
            policy_allowed=True,
            policy_reason=policy_reason,
            runtime_suspicious=runtime_suspicious,
            runtime_detail=runtime_detail,
            anomaly_anomalous=anomaly_anomalous,
            anomaly_score=anomaly_score,
            text_suspicious=resp_text_suspicious,
            text_reason=resp_text_reason,
        )

        self._log(
            tool=tool_name,
            operation=operation,
            decision=decision,
            reason=reason,
            policy_result=policy_reason,
            runtime_event=runtime_detail,
            anomaly_score=anomaly_score,
            text_result=resp_text_reason,
            signals=",".join(signals),
        )

        # ---- Wire anomaly training into the run loop ----
        # Only learn from legitimate, allowed behaviour so the detector knows
        # what "normal" looks like for this tool.
        if decision == ALLOW and not anomaly_anomalous:
            self.normal_samples.append(features)
            # Keep the sample list bounded so memory stays small.
            if len(self.normal_samples) > 500:
                self.normal_samples = self.normal_samples[-500:]
            # Retrain the model every N normal samples so it adapts to normal
            # behaviour over time (matches the synopsis: "learns normal tool
            # behaviour").
            if (
                not self.anomaly_trained
                and len(self.normal_samples) >= self.ANOMALY_TRAIN_EVERY
            ):
                self.anomaly.train(self.normal_samples)
                self.anomaly_trained = True
                log_stderr(
                    f"[gateway] anomaly detector trained on "
                    f"{len(self.normal_samples)} normal samples"
                )
            elif self.anomaly_trained and len(self.normal_samples) % self.ANOMALY_TRAIN_EVERY == 0:
                # Periodic refresh to keep up with slow drift in normal usage.
                self.anomaly.train(self.normal_samples)
                log_stderr(
                    f"[gateway] anomaly detector refreshed on "
                    f"{len(self.normal_samples)} normal samples"
                )

        # Return the tool result annotated with the security decision.
        annotated = dict(response) if isinstance(response, dict) else {}
        annotated["_security"] = {
            "decision": decision,
            "reason": reason,
            "signals": signals,
        }
        return {"jsonrpc": "2.0", "id": msg_id, "result": annotated}

    def _log(
        self,
        *,
        tool: str,
        operation: str,
        decision: str,
        reason: str,
        policy_result: str = "",
        runtime_event: str = "",
        anomaly_score: float | None = None,
        text_result: str = "",
        **extra: Any,
    ) -> None:
        self.logger.security_event(
            tool=tool,
            operation=operation,
            decision=decision,
            reason=reason,
            policy_result=policy_result,
            runtime_event=runtime_event,
            anomaly_score=anomaly_score,
            text_result=text_result,
            extra=extra if extra else None,
        )

    @staticmethod
    def _error_response(msg_id: Any, message: str) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": message},
        }


def main() -> None:
    cmd = os.environ.get("MCP_DOWNSTREAM_CMD")
    downstream_cmd = cmd.split() if cmd else DEFAULT_DOWNSTREAM
    Gateway(downstream_cmd).run()


if __name__ == "__main__":
    main()
