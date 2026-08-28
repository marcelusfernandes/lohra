"""Model clients — thin wrappers that turn transport kwargs into a raw response.

The transport produces request kwargs; the client owns the provider SDK and
makes the blocking call. Keeping client construction here (not on the
ProviderProfile, which is declarative) matches spec §3.

Per-request client lifecycle and the FD-ownership rule for interruptible calls
(spec §5) layer on top of this in the interruptible caller — a client is never
closed from a foreign (interrupt) thread.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable

from lohra.providers.errors import ProviderCallFailed

if TYPE_CHECKING:
    from lohra.providers.base import ProviderProfile

# Streaming delta callbacks (spec §6): visible text and chain-of-thought.
TextCallback = Callable[[str], None]


class ModelClient(ABC):
    """Owns one provider SDK client and makes blocking API calls."""

    @abstractmethod
    def create(self, **kwargs: Any) -> Any:
        """Make a blocking request from transport kwargs; return the raw response."""

    def stream(
        self,
        *,
        on_text: TextCallback | None = None,
        on_reasoning: TextCallback | None = None,
        **kwargs: Any,
    ) -> Any:
        """Stream a response, firing delta callbacks; return the raw final response.

        Default: no incremental delivery — delegate to ``create``. SDK-backed
        clients override to fire ``on_text`` / ``on_reasoning`` per delta.
        """
        return self.create(**kwargs)

    def generate_image(
        self, *, prompt: str, model: str, size: str | None = None, n: int = 1
    ) -> list[str]:
        """Generate image(s) and return them as base64 strings.

        Default: unsupported — only image-capable clients (OpenAI Images API)
        override this. The agent loop turns the raised error into a clean tool
        result, so a text-only provider fails gracefully.
        """
        raise RuntimeError("this provider does not support image generation")

    def close(self) -> None:  # pragma: no cover - default no-op
        """Release transport resources. Overridden by SDK-backed clients."""


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a dict or an attribute-style SDK object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def assemble_streamed_response(
    chunks: Any,
    *,
    on_text: TextCallback | None = None,
    on_reasoning: TextCallback | None = None,
) -> dict:
    """Fold chat-completions stream chunks into a non-streaming response shape.

    Returns ``{"choices": [{"message", "finish_reason"}], "usage": <usage|None>}`` so the
    transport's ``normalize_response`` reads it unchanged. Tolerates the quirks of
    OpenAI-compatible servers: a delta may omit ``index`` (key by id, else the
    most recent slot) and a tool call may arrive incomplete (dropped here so it
    can't 400 the next request).
    """
    content_parts: list[str] = []
    slots: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []  # slot keys in arrival order (index may be absent)
    finish_reason: str | None = None
    usage: Any = None
    for chunk in chunks:
        # Under stream_options.include_usage the LAST chunk carries the usage
        # with EMPTY choices — read it before the choices guard skips it.
        if _field(chunk, "usage") is not None:
            usage = _field(chunk, "usage")
        choices = _field(chunk, "choices") or ()
        if not choices:
            continue
        delta = _field(choices[0], "delta")
        text = _field(delta, "content")
        if text:
            content_parts.append(text)
            if on_text:
                on_text(text)
        reasoning = _field(delta, "reasoning_content")
        if reasoning and on_reasoning:
            on_reasoning(reasoning)
        for call in _field(delta, "tool_calls") or ():
            index = _field(call, "index")
            key = index if index is not None else _field(call, "id")
            if key is None:
                key = order[-1] if order else 0
            if key not in slots:
                slots[key] = {"id": None, "name": "", "args": ""}
                order.append(key)
            slot = slots[key]
            function = _field(call, "function")
            if _field(call, "id"):
                slot["id"] = _field(call, "id")
            if _field(function, "name"):
                slot["name"] = _field(function, "name")
            if _field(function, "arguments"):
                slot["args"] += _field(function, "arguments")
        if _field(choices[0], "finish_reason"):
            finish_reason = _field(choices[0], "finish_reason")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
    # Only emit fully-formed tool calls — a slot missing an id or name would 400
    # the next request (and orphan its tool result).
    tool_calls = [
        {
            "id": slots[k]["id"],
            "type": "function",
            "function": {"name": slots[k]["name"], "arguments": slots[k]["args"]},
        }
        for k in order
        if slots[k]["id"] and slots[k]["name"]
    ]
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": finish_reason}], "usage": usage}


class AnthropicClient(ModelClient):
    """Wraps the official ``anthropic`` SDK (optional dependency)."""

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "the anthropic SDK is not installed; run `pip install lohra[anthropic]`"
            ) from exc
        client_kwargs: dict[str, Any] = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**client_kwargs)

    def create(self, **kwargs: Any) -> Any:
        return self._client.messages.create(**kwargs)

    def stream(
        self,
        *,
        on_text: TextCallback | None = None,
        on_reasoning: TextCallback | None = None,
        **kwargs: Any,
    ) -> Any:  # pragma: no cover - exercised against the live SDK (Phase 1 E2E)
        with self._client.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type != "content_block_delta":
                    continue
                delta = event.delta
                if delta.type == "text_delta" and on_text:
                    on_text(delta.text)
                elif delta.type == "thinking_delta" and on_reasoning:
                    on_reasoning(delta.thinking)
            return stream.get_final_message()

    def close(self) -> None:  # pragma: no cover - thin SDK delegation
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class OpenAIClient(ModelClient):
    """Wraps the ``openai`` SDK — also drives any OpenAI-compatible endpoint.

    A custom ``base_url`` points the same client at openrouter / deepseek / groq
    / together / ollama; only the credentials and URL differ per provider.
    """

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "the openai SDK is not installed; run `pip install lohra[openai]`"
            ) from exc
        client_kwargs: dict[str, Any] = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**client_kwargs)

    def create(self, **kwargs: Any) -> Any:
        return self._client.chat.completions.create(**kwargs)

    def generate_image(
        self, *, prompt: str, model: str, size: str | None = None, n: int = 1
    ) -> list[str]:
        request: dict[str, Any] = {"model": model, "prompt": prompt, "n": n}
        if size:
            request["size"] = size
        response = self._client.images.generate(**request)
        data = _field(response, "data") or []
        return [b64 for item in data if (b64 := _field(item, "b64_json"))]

    def stream(
        self,
        *,
        on_text: TextCallback | None = None,
        on_reasoning: TextCallback | None = None,
        **kwargs: Any,
    ) -> Any:  # pragma: no cover - SDK iterator; assembly is tested via the helper
        try:
            # Ask for the final usage chunk; without this the stream never
            # carries token counts and every streamed turn accounts as 0.
            chunks = self._client.chat.completions.create(
                stream=True, stream_options={"include_usage": True}, **kwargs
            )
        except Exception as exc:  # noqa: BLE001 — see the guard below
            # Retry ONLY a stream_options rejection (older compat servers).
            # Anything else (timeout, auth, 429, 5xx) may have reached the
            # server — re-sending would double generation and billing.
            if "stream_options" not in str(exc):
                raise
            chunks = self._client.chat.completions.create(stream=True, **kwargs)
        return assemble_streamed_response(chunks, on_text=on_text, on_reasoning=on_reasoning)

    def close(self) -> None:  # pragma: no cover - thin SDK delegation
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class ResponsesClient(ModelClient):
    """Wraps the ``openai`` SDK's Responses API (Fase 10) — the ChatGPT/Codex
    subscription backend speaks only Responses. Token is a Bearer (sent as the
    SDK's ``api_key``); ``default_headers`` carry ChatGPT-Account-ID + originator."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "the openai SDK is not installed; run `pip install lohra[openai]`"
            ) from exc
        self._client = openai.OpenAI(
            api_key=api_key, base_url=base_url, default_headers=dict(default_headers or {})
        )

    def create(self, **kwargs: Any) -> Any:
        # The Codex backend REQUIRES stream=true (verified live), so even the
        # non-callback path streams and reconstructs the final Response.
        stream = self._client.responses.create(stream=True, **kwargs)
        return assemble_responses_stream(stream)

    def stream(
        self,
        *,
        on_text: TextCallback | None = None,
        on_reasoning: TextCallback | None = None,
        **kwargs: Any,
    ) -> Any:  # pragma: no cover - SDK iterator; assembly is tested via the helper
        events = self._client.responses.create(stream=True, **kwargs)
        return assemble_responses_stream(events, on_text=on_text)

    def close(self) -> None:  # pragma: no cover - thin SDK delegation
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def assemble_responses_stream(events: Any, *, on_text: TextCallback | None = None) -> dict:
    """Consume a Responses stream into a Response-shaped dict for normalize_response.

    Under store=false (required by the Codex backend) the terminal
    response.completed event's `response.output` is EMPTY — the real output items
    (message, function_call, reasoning) only arrive via response.output_item.done.
    So reconstruct from those; fall back to the completed response.output when it's
    populated (store=true). Status + usage come from the terminal event."""
    items: list[Any] = []
    status = "completed"
    usage = None
    for event in events:
        etype = _field(event, "type")
        if etype == "response.output_text.delta":
            delta = _field(event, "delta")
            if delta and on_text:
                on_text(delta)
        elif etype == "response.output_item.done":
            item = _field(event, "item")
            if item is not None:
                items.append(item)
        elif etype == "response.failed":
            # A failed turn must NOT collapse to a silent empty "stop": surface the
            # provider error (rate-limit / server / policy) so the loop reports it.
            resp = _field(event, "response")
            err = _field(resp, "error") if resp is not None else None
            code = _field(err, "code") if err is not None else None
            msg = _field(err, "message") if err is not None else None
            # Keep the CODE on the exception, not only inside the formatted prose:
            # it is the sole machine-readable signal here (this backend reports
            # quota exhaustion as an error code, with no HTTP status attached).
            raise ProviderCallFailed(
                f"Responses API failed: {(code or '')} {(msg or 'unknown error')}".strip(),
                code=code if isinstance(code, str) else None,
            )
        elif etype in ("response.completed", "response.incomplete"):
            resp = _field(event, "response")
            if resp is not None:
                status = _field(resp, "status") or status
                usage = _field(resp, "usage")
                out = _field(resp, "output")
                if out:  # store=true: the terminal event carries the full output
                    items = list(out)
    return {"status": status, "output": items, "usage": usage}


def resolve_api_key(profile: ProviderProfile, env: Mapping[str, str] | None = None) -> str | None:
    """First set value among the profile's ``env_vars`` (registration order)."""
    environ = os.environ if env is None else env
    return next((environ[var] for var in profile.env_vars if environ.get(var)), None)


def build_client(
    profile: ProviderProfile, *, env: Mapping[str, str] | None = None
) -> ModelClient:
    """Construct the right ModelClient for a profile's api_mode (offline)."""
    api_key = resolve_api_key(profile, env)
    base_url = profile.base_url or None
    if api_key is None and not profile.requires_api_key:
        # Keyless local endpoints (ollama) ignore the key, but the OpenAI SDK
        # still requires a non-empty string — supply a harmless placeholder.
        api_key = "lohra-local"
    if profile.api_mode == "anthropic_messages":
        return AnthropicClient(api_key=api_key, base_url=base_url)
    if profile.api_mode == "chat_completions":
        return OpenAIClient(api_key=api_key, base_url=base_url)
    raise ValueError(f"no client wired for api_mode {profile.api_mode!r}")
