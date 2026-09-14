"""OpenAI-compatible FastAPI app — POST /v1/chat/completions (+ /v1/models).

Relay mode: one stateless turn per request against the configured provider, so
an external OpenAI client reaches Lohra's provider. Auth is an optional Bearer
API key. Streaming runs the (blocking) turn in a worker thread and forwards
deltas as SSE. Tools/memory are intentionally OFF here — exposing fs/terminal
over HTTP would be remote code execution; an agentic mode is a guarded follow-up.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lohra import __version__
from lohra.server.format import (
    CompletionError,
    UpstreamError,
    build_chat_completion,
    build_chunk,
    build_done,
    build_models_list,
    split_messages,
    sse_event,
)
from lohra.server.responses import (
    build_content_part_added_event,
    build_output_item_added_event,
    build_response_completed_event,
    build_response_created_event,
    build_response_failed_event,
    build_response_object,
    build_text_delta_event,
    parse_responses_input,
)

from lohra.server.stream_bridge import StreamBridge, StreamLimits
from lohra.server.stream_response import OwnedStreamResponse
from lohra.server.stream_workers import StreamWorkers


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    # Sem o campo, o Pydantic DESCARTA {"include_usage": true} do cliente e o
    # stream nunca carrega usage — inclusive Lohra→serve→Lohra contava 0.
    stream_options: dict | None = None


class ResponsesRequest(BaseModel):
    model: str
    input: str | list[dict]
    instructions: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_output_tokens: int | None = None


def _error(status: int, message: str, error_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": error_type}}
    )


def create_openai_app(
    service: Any, *, api_key: str | None = None, models: tuple = (),
    stream_limits: StreamLimits | None = None,
) -> FastAPI:
    """Build an app with bounded SSE workers.

    Embedders keep ``run(model, messages, ..., on_delta)``. An optional
    ``run_cancellable(cancellation=..., ...)`` binds cooperative interruption;
    legacy services still get bounded delivery/admission, but no Agent signal.
    """
    workers = StreamWorkers(stream_limits or StreamLimits())

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await workers.shutdown()

    app = FastAPI(title="Lohra OpenAI-compatible server", lifespan=lifespan)
    app.state.stream_workers = workers

    def authorized(authorization: str | None) -> bool:
        if api_key is None:
            return True
        if not authorization or not authorization.startswith("Bearer "):
            return False
        token = authorization[len("Bearer ") :].strip()
        return hmac.compare_digest(token, api_key)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/v1/models")
    def list_models(authorization: str | None = Header(None)) -> Any:
        if not authorized(authorization):
            return _error(401, "missing or invalid API key", "authentication_error")
        return build_models_list(list(models), created=int(time.time()))

    @app.post("/v1/chat/completions")
    def chat_completions(
        request: ChatCompletionRequest, authorization: str | None = Header(None)
    ) -> Any:
        if not authorized(authorization):
            return _error(401, "missing or invalid API key", "authentication_error")
        # Validate up front so a malformed STREAM request still gets a 400 — once
        # a StreamingResponse starts, the status is committed to 200.
        try:
            split_messages(request.messages)
        except CompletionError as exc:
            return _error(400, str(exc))
        completion_id = f"chatcmpl-{uuid4().hex}"
        created = int(time.time())
        if request.stream:
            return OwnedStreamResponse(
                workers, service, dict(model=request.model, messages=request.messages,
                    temperature=request.temperature, max_tokens=request.max_tokens),
                lambda bridge: _stream(bridge, request, completion_id, created),
            )
        try:
            result = service.run(
                model=request.model,
                messages=request.messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
        except UpstreamError as exc:  # subclass of CompletionError — catch first
            return _error(502, str(exc), "upstream_error")
        except CompletionError as exc:
            return _error(400, str(exc))
        return JSONResponse(
            build_chat_completion(
                completion_id=completion_id,
                model=result["model"],
                content=result["content"],
                finish_reason=result["finish_reason"],
                usage=result["usage"],
                created=created,
            )
        )

    @app.post("/v1/responses")
    def responses(
        request: ResponsesRequest, authorization: str | None = Header(None)
    ) -> Any:
        if not authorized(authorization):
            return _error(401, "missing or invalid API key", "authentication_error")
        try:
            messages = parse_responses_input(request.input, request.instructions)
            split_messages(messages)  # validate before a stream commits to 200
        except CompletionError as exc:
            return _error(400, str(exc))
        response_id = f"resp_{uuid4().hex}"
        created = int(time.time())
        if request.stream:
            return OwnedStreamResponse(
                workers, service, dict(model=request.model, messages=messages,
                    temperature=request.temperature, max_tokens=request.max_output_tokens),
                lambda bridge: _responses_stream(bridge, request, response_id, created),
            )
        try:
            result = service.run(
                model=request.model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_output_tokens,
            )
        except UpstreamError as exc:
            return _error(502, str(exc), "upstream_error")
        except CompletionError as exc:
            return _error(400, str(exc))
        return JSONResponse(
            build_response_object(
                response_id=response_id,
                model=result["model"],
                content=result["content"],
                status="completed",
                usage=result["usage"],
                created=created,
            )
        )

    return app


async def _stream(
    bridge: StreamBridge, request: ChatCompletionRequest, completion_id: str, created: int
) -> AsyncIterator[str]:
    """Frame bounded deltas and a terminal receipt using the existing Chat wire."""
    yield sse_event(
        build_chunk(completion_id=completion_id, model=request.model, delta={"role": "assistant"}, created=created)
    )
    async for item in bridge.deltas():
        yield sse_event(
            build_chunk(completion_id=completion_id, model=request.model, delta={"content": item}, created=created)
        )
    receipt = bridge.receipt
    if receipt is None or receipt.disposition != "completed":
        yield sse_event({"error": {"message": _stream_error(bridge), "type": "upstream_error"}})
    else:
        result = receipt.result
        yield sse_event(
            build_chunk(
                completion_id=completion_id,
                model=request.model,
                delta={},
                created=created,
                finish_reason=result["finish_reason"],
            )
        )
        if (request.stream_options or {}).get("include_usage"):
            # Chunk final do protocolo OpenAI: choices vazio + usage. A wire
            # shape é INCLUSIVA (cached dentro de prompt), como no /v1/responses.
            usage = result.get("usage") or {}
            yield sse_event(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model,
                    "choices": [],
                    "usage": usage,
                }
            )
    yield build_done()


async def _responses_stream(
    bridge: StreamBridge, request: ResponsesRequest, response_id: str, created: int,
) -> AsyncIterator[str]:
    """Stream typed events; interruption has nullable or known-floor usage."""
    seq = _Counter()
    # created -> output_item.added -> content_part.added so the SDK's stream
    # snapshot has an item+part to index when the deltas land.
    yield build_response_created_event(
        response_id=response_id, model=request.model, created=created, sequence_number=seq.next()
    )
    yield build_output_item_added_event(response_id=response_id, sequence_number=seq.next())
    yield build_content_part_added_event(response_id=response_id, sequence_number=seq.next())
    async for item in bridge.deltas():
        yield build_text_delta_event(
            response_id=response_id, delta=item, sequence_number=seq.next()
        )
    receipt = bridge.receipt
    if receipt is None or receipt.disposition != "completed":
        usage = (receipt.usage if receipt is not None else None)
        if receipt is not None and receipt.disposition == "failed":
            # Preserve ordinary error formatting; broader usage normalization
            # belongs to #133, not request interruption.
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        failed = build_response_object(
            response_id=response_id,
            model=request.model,
            content="",
            status="failed",
            usage=usage,
            created=created,
            error={"code": "server_error", "message": _stream_error(bridge)},
        )
        yield build_response_failed_event(failed, sequence_number=seq.next())
    else:
        result = receipt.result
        yield build_response_completed_event(
            build_response_object(
                response_id=response_id,
                model=result["model"],
                content=result["content"],
                status="completed",
                usage=result["usage"],
                created=created,
            ),
            sequence_number=seq.next(),
        )


def _stream_error(bridge: StreamBridge) -> str:
    receipt = bridge.receipt
    if receipt is None:
        return "request cancelled; producer still draining and usage unresolved"
    return receipt.error_message or "request cancelled"


class _Counter:
    """Monotonic sequence_number source for Responses stream events."""

    def __init__(self) -> None:
        self._n = -1

    def next(self) -> int:
        self._n += 1
        return self._n
