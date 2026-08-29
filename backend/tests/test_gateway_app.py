"""Integration tests for the FastAPI dashboard app (WS + REST)."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.gateway.app import create_app
from lohra.gateway.manager import SessionManager
from lohra.providers import get_provider_profile
from lohra.state import SessionDB


class _FakeClient(ModelClient):
    def create(self, **kwargs):
        return {"content": [{"type": "text", "text": "hi from agent"}], "stop_reason": "end_turn", "usage": None}

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        raw = self.create(**kwargs)
        if on_text:
            on_text("hi from agent")
        return raw


def _factory(_session_id: str):
    return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"), client=_FakeClient())


@pytest.fixture
def client_and_manager():
    db = SessionDB(":memory:")
    manager = SessionManager(db, _factory)
    app = create_app(manager, token=None)  # insecure mode for most tests
    yield TestClient(app), manager
    db.close()


@pytest.fixture
def secured_client():
    db = SessionDB(":memory:")
    app = create_app(SessionManager(db, _factory), token="secret")
    yield TestClient(app)
    db.close()


def _drain_until(ws, type_name, limit=20):
    """Receive frames until one with params.type == type_name, returning all seen."""
    seen = []
    for _ in range(limit):
        frame = ws.receive_json()
        seen.append(frame)
        if frame.get("params", {}).get("type") == type_name:
            return seen
    raise AssertionError(f"{type_name} not seen; got {[f.get('params', {}).get('type') for f in seen]}")


# --- WebSocket ---


def test_ws_pushes_gateway_ready_on_connect(client_and_manager):
    client, _ = client_and_manager
    with client.websocket_connect("/api/ws") as ws:
        ready = ws.receive_json()
        assert ready["method"] == "event"
        assert ready["params"]["type"] == "gateway.ready"


def test_ws_auth_rejects_bad_token():
    db = SessionDB(":memory:")
    app = create_app(SessionManager(db, _factory), token="secret")
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/ws?token=wrong") as ws:
            ws.receive_json()
    assert exc.value.code == 4401
    db.close()


def test_ws_auth_accepts_good_token():
    db = SessionDB(":memory:")
    app = create_app(SessionManager(db, _factory), token="secret")
    client = TestClient(app)
    with client.websocket_connect("/api/ws?token=secret") as ws:
        assert ws.receive_json()["params"]["type"] == "gateway.ready"
    db.close()


def test_ws_session_create_then_prompt_stream(client_and_manager):
    client, _ = client_and_manager
    with client.websocket_connect("/api/ws") as ws:
        ws.receive_json()  # gateway.ready

        ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session.create", "params": {}})
        created = ws.receive_json()
        assert created["id"] == 1
        session_id = created["result"]["session_id"]
        info = ws.receive_json()
        assert info["params"]["type"] == "session.info"

        ws.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "prompt.submit", "params": {"session_id": session_id, "text": "oi"}}
        )
        ack = ws.receive_json()
        assert ack["id"] == 2
        assert ack["result"]["status"] == "streaming"

        seen = _drain_until(ws, "message.complete")
        types = [f["params"]["type"] for f in seen]
        assert types == ["message.start", "message.delta", "message.complete"]
        assert seen[-1]["params"]["payload"]["text"] == "hi from agent"


def test_ws_prompt_unknown_session_errors(client_and_manager):
    client, _ = client_and_manager
    with client.websocket_connect("/api/ws") as ws:
        ws.receive_json()
        ws.send_json(
            {"jsonrpc": "2.0", "id": 9, "method": "prompt.submit", "params": {"session_id": "nope", "text": "x"}}
        )
        resp = ws.receive_json()
        assert resp["error"]["code"] == -32602  # invalid params


def test_ws_unknown_method_errors(client_and_manager):
    client, _ = client_and_manager
    with client.websocket_connect("/api/ws") as ws:
        ws.receive_json()
        ws.send_json({"jsonrpc": "2.0", "id": 3, "method": "does.not.exist"})
        resp = ws.receive_json()
        assert resp["error"]["code"] == -32601  # method not found


# --- REST ---


def test_rest_status(client_and_manager):
    client, _ = client_and_manager
    body = client.get("/api/status").json()
    assert body["ok"] is True
    assert "version" in body


def test_rest_sessions_and_messages(client_and_manager):
    client, manager = client_and_manager
    session = manager.create_session(session_id="s1")
    session.submit("hello", lambda _f: None)

    listed = client.get("/api/sessions").json()["sessions"]
    assert any(s["id"] == "s1" for s in listed)

    msgs = client.get("/api/sessions/s1/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_rest_config(client_and_manager):
    client, _ = client_and_manager
    body = client.get("/api/config").json()
    assert body["auth_required"] is False


@pytest.mark.parametrize(
    "path",
    (
        "/api/status",
        "/api/sessions",
        "/api/sessions/missing/messages",
        "/api/config",
    ),
)
def test_secure_rest_requires_the_dashboard_session_header(secured_client, path):
    for kwargs in (
        {},
        {"headers": {"X-Lohra-Session-Token": "wrong"}},
        {"headers": {"Authorization": "Bearer secret"}},
        {"params": {"token": "secret"}},
    ):
        response = secured_client.get(path, **kwargs)
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}

    accepted = secured_client.get(path, headers={"X-Lohra-Session-Token": "secret"})
    assert accepted.status_code == 200


def test_secure_rest_middleware_covers_future_api_routes_and_allows_options():
    db = SessionDB(":memory:")
    app = create_app(SessionManager(db, _factory), token="secret")

    @app.get("/api/future")
    def future_route():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/api/future").status_code == 401
    assert (
        client.get("/api/future", headers={"X-Lohra-Session-Token": "secret"}).status_code
        == 200
    )
    assert client.options("/api/status").status_code != 401
    db.close()
