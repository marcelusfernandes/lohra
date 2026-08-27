"""JSON-RPC 2.0 framing for the dashboard WebSocket (spec §2).

Three frame types share the wire: client->server requests, server->client
responses (result or error), and server->client events (see events.py). These
helpers parse requests and build response/error frames; they never raise into
the socket loop except via JsonRpcError, which the caller turns into an error
frame.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# JSON-RPC standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application-level codes (mirrors the spec's WS vocabulary).
SESSION_BUSY = 4009


class JsonRpcError(Exception):
    """A framed error: carries a JSON-RPC code and an optional request id."""

    def __init__(self, code: int, message: str, request_id: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True)
class JsonRpcRequest:
    id: Any
    method: str
    params: dict[str, Any]


def parse_request(raw: str) -> JsonRpcRequest:
    """Parse a client->server request frame; raise JsonRpcError on malformed input."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise JsonRpcError(PARSE_ERROR, f"parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise JsonRpcError(INVALID_REQUEST, "request must be a JSON object")
    request_id = data.get("id")
    method = data.get("method")
    if not method or not isinstance(method, str):
        raise JsonRpcError(INVALID_REQUEST, "missing or invalid 'method'", request_id)
    params = data.get("params") or {}
    if not isinstance(params, dict):
        raise JsonRpcError(INVALID_PARAMS, "'params' must be an object", request_id)
    return JsonRpcRequest(id=request_id, method=method, params=params)


def ok_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
