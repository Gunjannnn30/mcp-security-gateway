"""HTTP + SSE transport for the MCP Security Gateway.

The gateway core speaks JSON-RPC over stdio. This module wraps it so an AI
agent (or any HTTP client) can talk to the gateway over plain HTTP with
Server-Sent Events (SSE) for the responses.

Endpoints:
  POST /mcp          - send a JSON-RPC message to the gateway (gets a reply)
  GET  /mcp/sse      - SSE stream that holds a session open (keep-alive)
  GET  /health       - simple health check

Usage:
    python src/http_server.py                 # HTTP+SSE on :8000
    MCP_DOWNSTREAM_CMD="python tools/unsafe_tool.py" python src.http_server.py

Requires: starlette, uvicorn (already installed as transitive deps of mcp)
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

# Make src importable when run as a script.
import sys as _sys
import os as _os
_src_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)))
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

from gateway import Gateway  # noqa: E402

# In-memory sessions: session_id -> {gateway}
SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSIONS_LOCK = anyio.Lock()


def _downstream_cmd() -> list[str]:
    cmd = os.environ.get("MCP_DOWNSTREAM_CMD")
    return cmd.split() if cmd else ["python", os.path.join("tools", "safe_weather.py")]


async def _get_gateway(session_id: str) -> Gateway:
    """Get or create (and prime) a gateway for a session."""
    async with SESSIONS_LOCK:
        entry = SESSIONS.get(session_id)
        if entry is None:
            gateway = Gateway(_downstream_cmd())
            # Prime once per session: initialize + list downstream tools.
            await anyio.to_thread.run_sync(gateway.run_setup)
            SESSIONS[session_id] = {"gateway": gateway}
        return SESSIONS[session_id]["gateway"]


def handle_message(gateway: Gateway, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Process one JSON-RPC message through the gateway and return the reply."""
    return gateway.handle(message)


# --- Starlette routes ---


async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def mcp_post(request: Request) -> JSONResponse:
    """Receive a JSON-RPC message over HTTP and run it through the gateway."""
    body = await request.json()
    session_id = request.query_params.get("session_id") or str(uuid.uuid4())

    gateway = await _get_gateway(session_id)
    reply = handle_message(gateway, body)

    if reply is None:
        # Notification (no response needed). Acknowledge.
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "status": "accepted"})

    return JSONResponse(reply)


async def mcp_sse(request: Request) -> StreamingResponse:
    """SSE stream that holds a session open with keep-alive comments."""
    session_id = request.query_params.get("session_id") or str(uuid.uuid4())
    await _get_gateway(session_id)  # ensure session exists

    async def event_stream() -> Any:
        yield f": session {session_id}\n\n"
        while True:
            await anyio.sleep(30)
            yield f": keep-alive {session_id}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/mcp", mcp_post, methods=["POST", "OPTIONS"]),
        Route("/mcp/sse", mcp_sse, methods=["GET"]),
    ]
)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    print(f"[http] MCP Security Gateway HTTP+SSE on http://0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
