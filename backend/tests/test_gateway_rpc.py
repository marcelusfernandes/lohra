"""Tests for JSON-RPC 2.0 framing (gateway WS protocol, spec §2)."""

import json

import pytest

from lohra.gateway.rpc import (
    JsonRpcError,
    error_response,
    ok_response,
    parse_request,
)


def test_parse_valid_request():
    req = parse_request('{"jsonrpc":"2.0","id":7,"method":"prompt.submit","params":{"text":"hi"}}')
    assert req.id == 7
    assert req.method == "prompt.submit"
    assert req.params == {"text": "hi"}


def test_parse_request_without_params_defaults_empty():
    req = parse_request('{"jsonrpc":"2.0","id":1,"method":"session.list"}')
    assert req.params == {}


def test_parse_malformed_json_raises():
    with pytest.raises(JsonRpcError) as exc:
        parse_request("{not json")
    assert exc.value.code == -32700  # parse error


def test_parse_missing_method_raises():
    with pytest.raises(JsonRpcError) as exc:
        parse_request('{"jsonrpc":"2.0","id":1}')
    assert exc.value.code == -32600  # invalid request


def test_ok_response_shape():
    frame = ok_response(7, {"status": "streaming"})
    assert frame == {"jsonrpc": "2.0", "id": 7, "result": {"status": "streaming"}}


def test_error_response_shape():
    frame = error_response(7, 4009, "session busy")
    assert frame["jsonrpc"] == "2.0"
    assert frame["id"] == 7
    assert frame["error"] == {"code": 4009, "message": "session busy"}


def test_error_response_serializable():
    json.dumps(error_response(None, -32601, "method not found"))  # must not raise
