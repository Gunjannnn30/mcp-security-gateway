"""End-to-end check of the gateway proxy (manual, run once)."""
import json
import os
import subprocess
import sys
import time

ROOT = r"C:\Users\DELL\mcp-security-gateway"


def run_gateway(downstream_cmd, messages):
    proc = subprocess.Popen(
        [sys.executable, "src/gateway.py"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "MCP_DOWNSTREAM_CMD": downstream_cmd},
    )

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    results = []
    for m in messages:
        send(m)
        results.append(recv())
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return results


# Safe tool: weather should be ALLOWED.
safe_msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "weather", "arguments": {"city": "Delhi"}},
    },
]
print("=== SAFE TOOL (weather) ===")
for r in run_gateway("python tools/safe_weather.py", safe_msgs):
    print(json.dumps(r))

# Unsafe tool: file_reader should be BLOCKED by policy.
unsafe_msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "file_reader", "arguments": {"path": "C:/Windows/win.ini"}},
    },
    # poisoned response text should be BLOCKED (text detector)
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "poisoned", "arguments": {"query": "hi"}},
    },
]
print("\n=== UNSAFE TOOL (file_reader + poisoned) ===")
for r in run_gateway("python tools/unsafe_tool.py", unsafe_msgs):
    print(json.dumps(r))
