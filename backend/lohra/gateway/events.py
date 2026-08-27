"""Dashboard WebSocket event vocabulary (server -> client).

This is the typed contract the Tauri/React renderer consumes. It is distinct
from the OpenAI-compatible SSE event set emitted by the /v1 server.

See docs/specs/04-gateway-protocol.md §2.4.
"""

from __future__ import annotations

# Canonical event names pushed as JSON-RPC `{"method":"event","params":{type,...}}`.
GATEWAY_EVENTS = (
    "gateway.ready",
    "session.info",
    "session.forked",
    "message.start",
    "message.delta",
    "message.complete",
    "thinking.delta",
    "reasoning.delta",
    "reasoning.available",
    "status.update",
    "tool.start",
    "tool.progress",
    "tool.complete",
    "tool.generating",
    "clarify.request",
    "approval.request",
    "sudo.request",
    "secret.request",
    "background.complete",
    "error",
    "skin.changed",
)


def event_frame(event_type: str, session_id: str | None, payload: dict | None) -> dict:
    """Build a server->client streaming event frame (JSON-RPC 2.0)."""
    if event_type not in GATEWAY_EVENTS:
        # forward-compatible: unknown types still dispatch on the client
        pass
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": event_type, "session_id": session_id, "payload": payload or {}},
    }
