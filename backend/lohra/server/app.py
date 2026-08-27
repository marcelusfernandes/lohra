"""OpenAI-compatible FastAPI app — POST /v1/chat/completions (+ /v1/models).

Relay mode: one stateless turn per request against the configured provider, so
an external OpenAI client reaches Lohra's provider. Auth is an optional Bearer
API key. Streaming runs the (blocking) turn in a worker thread and forwards
deltas as SSE. Tools/memory are intentionally OFF here — exposing fs/terminal
over HTTP would be remote code execution; an agentic mode is a guarded follow-up.
"""

from __future__ import annotations

import hmac
import queue
import threading
import time
from typing import Any, Iterator
from uuid import uuid4

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse
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


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


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


def create_openai_app(service: Any, *, api_key: str | None = None, models: tuple = ()) -> FastAPI:
    """Build the app. ``service`` exposes ``run(model, messages, ..., on_delta)``."""
    app = FastAPI(title="Lohra OpenAI-compatible server")

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
            return StreamingResponse(
                _stream(service, request, completion_id, created),
                media_type="text/event-stream",
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
            return StreamingResponse(
                _responses_stream(service, request, messages, response_id, created),
                media_type="text/event-stream",
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


def _stream(
    service: Any, request: ChatCompletionRequest, completion_id: str, created: int
) -> Iterator[str]:
    """Run the turn in a worker thread, forwarding deltas as SSE chunks.

    The blocking ``service.run`` fires ``on_delta`` from the worker; the
    generator drains a queue and frames each delta. A mid-stream failure is
    delivered as an error event (the HTTP status is already 200 by then).
    """
    deltas: queue.Queue = queue.Queue()
    done = object()
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["result"] = service.run(
                model=request.model,
                messages=request.messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                on_delta=deltas.put,
            )
        except Exception as exc:  # surfaced as an SSE error event below
            box["error"] = exc
        finally:
            deltas.put(done)

    threading.Thread(target=worker, daemon=True).start()

    yield sse_event(
        build_chunk(completion_id=completion_id, model=request.model, delta={"role": "assistant"}, created=created)
    )
    while True:
        item = deltas.get()
        if item is done:
            break
        yield sse_event(
            build_chunk(completion_id=completion_id, model=request.model, delta={"content": item}, created=created)
        )
    if "error" in box:
        yield sse_event({"error": {"message": str(box["error"]), "type": "upstream_error"}})
    else:
        yield sse_event(
            build_chunk(
                completion_id=completion_id,
                model=request.model,
                delta={},
                created=created,
                finish_reason=box["result"]["finish_reason"],
            )
        )
    yield build_done()


def _responses_stream(
    service: Any,
    request: ResponsesRequest,
    messages: list[dict],
    response_id: str,
    created: int,
) -> Iterator[str]:
    """Stream a Responses turn as typed SSE events: created -> delta* -> completed."""
    deltas: queue.Queue = queue.Queue()
    done = object()
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["result"] = service.run(
                model=request.model,
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_output_tokens,
                on_delta=deltas.put,
            )
        except Exception as exc:
            box["error"] = exc
        finally:
            deltas.put(done)

    threading.Thread(target=worker, daemon=True).start()

    seq = _Counter()
    # created -> output_item.added -> content_part.added so the SDK's stream
    # snapshot has an item+part to index when the deltas land.
    yield build_response_created_event(
        response_id=response_id, model=request.model, created=created, sequence_number=seq.next()
    )
    yield build_output_item_added_event(response_id=response_id, sequence_number=seq.next())
    yield build_content_part_added_event(response_id=response_id, sequence_number=seq.next())
    while True:
        item = deltas.get()
        if item is done:
            break
        yield build_text_delta_event(
            response_id=response_id, delta=item, sequence_number=seq.next()
        )
    if "error" in box:
        failed = build_response_object(
            response_id=response_id,
            model=request.model,
            content="",
            status="failed",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            created=created,
            error={"code": "server_error", "message": str(box["error"])},
        )
        yield build_response_failed_event(failed, sequence_number=seq.next())
    else:
        result = box["result"]
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


class _Counter:
    """Monotonic sequence_number source for Responses stream events."""

    def __init__(self) -> None:
        self._n = -1

    def next(self) -> int:
        self._n += 1
        return self._n
