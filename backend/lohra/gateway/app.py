"""FastAPI dashboard app: WS JSON-RPC + REST (spec §1-3).

create_app(manager, token) builds the ASGI app. The WebSocket at /api/ws is
the heart: token-authenticated, pushes gateway.ready, then dispatches requests.
prompt.submit runs the agent in a worker thread and streams event frames back
over an asyncio queue (one turn at a time per socket — concurrent mid-turn
interrupt is a follow-up).
"""

from __future__ import annotations

import asyncio
import hmac
import threading
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from lohra import __version__
from lohra.gateway.events import event_frame
from lohra.gateway.manager import SessionManager
from lohra.gateway.rpc import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    SESSION_BUSY,
    JsonRpcError,
    JsonRpcRequest,
    error_response,
    ok_response,
    parse_request,
)

WS_AUTH_FAILED = 4401


def _session_info_frame(session) -> dict:
    agent = session.agent
    payload = {
        "model": agent.model,
        "tools": [d["function"]["name"] for d in agent.tool_definitions],
        "running": session.busy,
        "version": __version__,
    }
    return event_frame("session.info", session.session_id, payload)


def create_app(manager: SessionManager, *, token: str | None = None) -> FastAPI:
    """Build the dashboard app. token=None means insecure mode (auth disabled)."""
    app = FastAPI(title="Lohra")

    def authorized(supplied: str | None) -> bool:
        if token is None:
            return True  # insecure/local mode
        return supplied is not None and hmac.compare_digest(supplied, token)

    # --- REST ---

    @app.get("/api/status")
    def status() -> dict:
        return {"ok": True, "version": __version__, "sessions": len(manager.list_sessions())}

    @app.get("/api/sessions")
    def sessions() -> dict:
        return {"sessions": manager.list_sessions()}

    @app.get("/api/sessions/{session_id}/messages")
    def messages(session_id: str) -> dict:
        return {"messages": manager.history(session_id)}

    @app.get("/api/config")
    def config() -> dict:
        return {"version": __version__, "auth_required": token is not None}

    # --- WebSocket ---

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        if not authorized(websocket.query_params.get("token")):
            await websocket.close(code=WS_AUTH_FAILED)
            return
        await websocket.send_json(event_frame("gateway.ready", None, {"skin": {"name": "lohra"}}))
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    request = parse_request(raw)
                except JsonRpcError as exc:
                    await websocket.send_json(error_response(exc.request_id, exc.code, exc.message))
                    continue
                await _dispatch(websocket, manager, request)
        except WebSocketDisconnect:
            return

    return app


async def _dispatch(websocket: WebSocket, manager: SessionManager, request: JsonRpcRequest) -> None:
    method, params, rid = request.method, request.params, request.id

    if method == "session.create":
        session = manager.create_session(
            session_id=params.get("session_id"), title=params.get("title"), cwd=params.get("cwd")
        )
        await websocket.send_json(ok_response(rid, {"session_id": session.session_id}))
        await websocket.send_json(_session_info_frame(session))
        return

    if method == "session.list":
        await websocket.send_json(ok_response(rid, {"sessions": manager.list_sessions()}))
        return

    if method == "session.history":
        await websocket.send_json(ok_response(rid, {"messages": manager.history(params.get("session_id", ""))}))
        return

    if method == "session.interrupt":
        session = manager.get(params.get("session_id", ""))
        if session is not None:
            session.interrupt()
        await websocket.send_json(ok_response(rid, {"ok": session is not None}))
        return

    if method == "prompt.submit":
        await _stream_prompt(websocket, manager, request)
        return

    await websocket.send_json(error_response(rid, METHOD_NOT_FOUND, f"unknown method: {method}"))


async def _stream_prompt(websocket: WebSocket, manager: SessionManager, request: JsonRpcRequest) -> None:
    session = manager.get(request.params.get("session_id", ""))
    if session is None:
        await websocket.send_json(error_response(request.id, INVALID_PARAMS, "unknown session_id"))
        return
    if session.busy:
        await websocket.send_json(error_response(request.id, SESSION_BUSY, "session busy"))
        return

    await websocket.send_json(ok_response(request.id, {"status": "streaming"}))

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    done = object()
    text = request.params.get("text", "")

    def emit(frame: Any) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, frame)
        except RuntimeError:
            pass  # event loop closed during shutdown — drop the frame

    def worker() -> None:
        try:
            session.submit(text, emit)
        finally:
            emit(done)

    threading.Thread(target=worker, daemon=True).start()
    try:
        while True:
            frame = await queue.get()
            if frame is done:
                break
            await websocket.send_json(frame)
    except Exception:
        # Client likely disconnected mid-stream — stop the agent at its next
        # boundary so the worker doesn't run on with no consumer holding the lock.
        session.interrupt()
        raise
