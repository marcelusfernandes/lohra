"""Tests for CompletionService — runs one agent turn for the OpenAI endpoint.

A fake client (anthropic-shaped) stands in for the SDK so no network is used.
"""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.server.format import CompletionError
from lohra.server.service import CompletionService


class FakeClient(ModelClient):
    def __init__(self, responses):
        self._responses = list(responses)

    def create(self, **kwargs):
        return self._responses.pop(0)

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        raw = self.create(**kwargs)
        for block in raw.get("content", []):
            if block.get("type") == "text" and on_text:
                on_text(block["text"])
        return raw


def _text(text, stop="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop, "usage": None}


def _factory(responses):
    return lambda: Agent(
        model="placeholder",
        provider=get_provider_profile("anthropic"),
        client=FakeClient(responses),
    )


def _messages(user="hi"):
    return [{"role": "user", "content": user}]


def test_run_returns_content_and_stop():
    svc = CompletionService(_factory([_text("hello there")]))
    out = svc.run(model="claude-opus-4-8", messages=_messages())
    assert out["content"] == "hello there"
    assert out["finish_reason"] == "stop"
    assert out["model"] == "claude-opus-4-8"  # echoes the requested model


def test_run_maps_length_finish_reason():
    svc = CompletionService(_factory([_text("cut", stop="max_tokens")]))
    out = svc.run(model="m", messages=_messages())
    assert out["finish_reason"] == "length"


def test_run_reports_usage_estimate_when_provider_gives_none():
    svc = CompletionService(_factory([_text("abcd")]))  # usage=None
    out = svc.run(model="m", messages=_messages("hello"))
    usage = out["usage"]
    assert usage["prompt_tokens"] >= 0
    assert usage["completion_tokens"] >= 1
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_run_reports_real_usage_when_provider_returns_it():
    response = {
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }
    out = CompletionService(_factory([response])).run(model="m", messages=_messages())
    assert out["usage"]["prompt_tokens"] == 123
    assert out["usage"]["completion_tokens"] == 45
    assert out["usage"]["total_tokens"] == 168


def test_run_reemits_the_cache_split_in_the_openai_wire_shape():
    """Fatia C: internamente ``input_tokens`` e o prompt NAO cacheado; na
    fronteira do servidor volta a convencao da OpenAI (cached ⊆ prompt)."""
    response = {
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 40,
        },
    }
    out = CompletionService(_factory([response])).run(model="m", messages=_messages())
    usage = out["usage"]
    assert usage["prompt_tokens"] == 200  # 100 + 60 + 40, o total real do prompt
    assert usage["prompt_tokens_details"]["cached_tokens"] == 60
    assert usage["prompt_tokens_details"]["cache_write_tokens"] == 40
    assert usage["total_tokens"] == 220


def test_run_passes_history_to_the_agent():
    svc = CompletionService(_factory([_text("ok")]))
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "earlier reply"},
        {"role": "user", "content": "second"},
    ]
    # should not raise; the trailing user turn drives the run
    out = svc.run(model="m", messages=messages)
    assert out["content"] == "ok"


def test_run_streams_deltas():
    svc = CompletionService(_factory([_text("streamed")]))
    seen = []
    svc.run(model="m", messages=_messages(), on_delta=seen.append)
    assert "".join(seen) == "streamed"


def test_run_rejects_non_user_last_message():
    svc = CompletionService(_factory([_text("x")]))
    with pytest.raises(CompletionError):
        svc.run(model="m", messages=[{"role": "assistant", "content": "x"}])


def test_run_surfaces_provider_error():
    class Boom(ModelClient):
        def create(self, **kwargs):
            raise RuntimeError("upstream 500")

        def stream(self, **kwargs):
            raise RuntimeError("upstream 500")

    factory = lambda: Agent(  # noqa: E731
        model="m", provider=get_provider_profile("anthropic"), client=Boom()
    )
    svc = CompletionService(factory)
    with pytest.raises(CompletionError, match="upstream 500"):
        svc.run(model="m", messages=_messages())


def _tool_use(call_id, name, inp):
    return {
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": inp}],
        "stop_reason": "tool_use",
        "usage": None,
    }


def test_agentic_run_executes_a_tool_server_side():
    # the model asks for a tool, the agent runs it, then answers — the client
    # only sees the final text
    tool_calls = []

    def dispatch(name, args):
        tool_calls.append((name, args))
        return '{"ok": true, "data": "42"}'

    def factory():
        return Agent(
            model="m",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_tool_use("t1", "read_file", {"path": "a"}), _text("the answer is 42")]),
            tool_definitions=({"type": "function", "function": {"name": "read_file"}},),
            tool_dispatch=dispatch,
            max_iterations=10,
        )

    out = CompletionService(factory).run(model="m", messages=_messages("read a"))
    assert tool_calls == [("read_file", {"path": "a"})]
    assert out["content"] == "the answer is 42"
    assert out["finish_reason"] == "stop"


def test_temperature_and_max_tokens_override_the_agent():
    captured = {}

    class Capturing(ModelClient):
        def create(self, **kwargs):
            captured.update(kwargs)
            return _text("ok")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    factory = lambda: Agent(  # noqa: E731
        model="m", provider=get_provider_profile("anthropic"), client=Capturing()
    )
    svc = CompletionService(factory)
    svc.run(model="m", messages=_messages(), temperature=0.1, max_tokens=42)
    assert captured["temperature"] == 0.1
    assert captured["max_tokens"] == 42
