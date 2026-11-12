"""Safe file reader MCP tool.

Reads files only from the allowed data/ directory.
Used to test that allowed file access passes, while access outside data/ is blocked.
"""

from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {
        "name": "safe_file_reader",
        "description": "Reads a file from the data directory only.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
]


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "safe-file-reader", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "safe_file_reader":
            path = args.get("path", "")
            # The tool itself only reads from data/ (gateway policy enforces this).
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
            full_path = os.path.abspath(os.path.join(data_dir, path))
            try:
                if not full_path.startswith(os.path.abspath(data_dir)):
                    text = "error: access outside data directory not allowed"
                else:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read(2000)
            except Exception as exc:  # noqa: BLE001
                text = f"error reading {path}: {exc}"
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown tool: {name}"}}
    return None


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
