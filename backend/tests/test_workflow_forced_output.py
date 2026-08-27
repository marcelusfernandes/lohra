"""Tests for forced tool_choice — Part A: transport param + helpers (Milestone I).

Part A is the contained, low-risk foundation: an optional `tool_choice` on
build_kwargs (byte-identical default — Invariant #1), a synthetic StructuredOutput
tool builder, and the provider-ignored-it fallback detector. Wiring it into the
leaf path (Part B) is a separate, gated step.
"""

from lohra.agent.types import ToolCall
from lohra.providers.transports.anthropic_messages import AnthropicMessagesTransport
from lohra.providers.transports.chat_completions import ChatCompletionsTransport
from lohra.workflow.validation import (
    STRUCTURED_OUTPUT_TOOL,
    extract_structured_call,
    synthetic_structured_tool,
)

_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
_ARGS = dict(model="m", messages=[{"role": "user", "content": "hi"}], system="SYS")


# --- Invariant #1: tool_choice=None is byte-identical to today ---


def test_chat_completions_default_is_byte_identical():
    t = ChatCompletionsTransport()
    assert t.build_kwargs(**_ARGS) == t.build_kwargs(**_ARGS, tool_choice=None)


def test_anthropic_default_is_byte_identical():
    t = AnthropicMessagesTransport()
    base = t.build_kwargs(**_ARGS)
    assert base == t.build_kwargs(**_ARGS, tool_choice=None)
    assert base.get("system") == "SYS"  # the frozen system string is untouched


# --- forcing injects the provider-specific shape, system unchanged ---


def test_chat_completions_forces_tool():
    kwargs = ChatCompletionsTransport().build_kwargs(**_ARGS, tool_choice="StructuredOutput")
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "StructuredOutput"}}


def test_anthropic_forces_tool_without_touching_system():
    kwargs = AnthropicMessagesTransport().build_kwargs(**_ARGS, tool_choice="StructuredOutput")
    assert kwargs["tool_choice"] == {"type": "tool", "name": "StructuredOutput"}
    assert kwargs["system"] == "SYS"  # forcing rides in tool_choice, NOT the prompt


# --- synthetic tool builder ---


def test_synthetic_tool_wraps_schema():
    tool = synthetic_structured_tool(_SCHEMA)
    assert tool["function"]["name"] == STRUCTURED_OUTPUT_TOOL
    assert tool["function"]["parameters"] == _SCHEMA


# --- fallback detector ---


def test_extract_structured_call_validates_args():
    calls = [ToolCall(id="1", name=STRUCTURED_OUTPUT_TOOL, arguments='{"n": 7}')]
    ok, value, _ = extract_structured_call(calls, _SCHEMA)
    assert ok and value == {"n": 7}


def test_extract_missing_call_signals_fallback():
    # provider ignored tool_choice -> no StructuredOutput call -> caller falls back
    ok, _, reason = extract_structured_call([ToolCall(id="1", name="other", arguments="{}")], _SCHEMA)
    assert not ok and "ignored tool_choice" in reason
    ok2, _, _ = extract_structured_call([], _SCHEMA)
    assert not ok2


def test_extract_structured_call_schema_mismatch():
    calls = [ToolCall(id="1", name=STRUCTURED_OUTPUT_TOOL, arguments='{"n": "bad"}')]
    ok, _, err = extract_structured_call(calls, _SCHEMA)
    assert not ok and err


# --- Part B: forced output through the real leaf path ---

import pytest  # noqa: E402

from lohra.agent.agent import Agent  # noqa: E402
from lohra.orchestration.core import OrchestrationCore  # noqa: E402
from lohra.providers import get_provider_profile  # noqa: E402
from lohra.state import SessionDB  # noqa: E402
from lohra.workflow.budget import Budget  # noqa: E402
from lohra.workflow.engine import WorkflowEngine  # noqa: E402
from lohra.workflow.schema import validate_spec  # noqa: E402
from tests.test_loop import _text_response, _tool_call_response  # noqa: E402


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, responses, captured=None):
    def factory():
        from tests.test_loop import FakeClient

        client = FakeClient(responses)
        if captured is not None:
            captured.append(client)
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=client,
            tool_dispatch=lambda n, a: "{}",
        )

    return OrchestrationCore(db, factory)


def _run_tool_less(core):
    spec = validate_spec({"meta": {"name": "f"}, "nodes": [
        {"id": "a", "type": "agent", "prompt": "give n", "schema": _SCHEMA, "tool_less": True}]})
    return WorkflowEngine(core, budget=Budget()).run(spec, {})


def test_tool_less_node_forces_structured_output(db):
    # the leaf calls the synthetic StructuredOutput tool; its args ARE the answer,
    # and the request carried tool_choice (forced on the wire).
    captured = []
    core = _core(db, [_tool_call_response([("c1", "StructuredOutput", {"n": 5})])], captured)
    try:
        assert _run_tool_less(core).outputs["a"] == {"n": 5}
        assert "tool_choice" in captured[0].calls[0]  # forced on the wire
    finally:
        core.shutdown()


def test_tool_less_falls_back_when_provider_ignores(db):
    # provider ignored tool_choice -> answered as text -> §5.1 validates it anyway,
    # and the reduced-rigor fallback is counted in the rollup (§5.3).
    core = _core(db, [_text_response('{"n": 9}')])
    try:
        result = _run_tool_less(core)
        assert result.outputs["a"] == {"n": 9}
        assert result.forcing_fallbacks == 1
    finally:
        core.shutdown()


def test_normal_node_is_not_forced(db):
    # without tool_less: no tool_choice on the wire (discriminates a regression
    # that would force every schema'd node).
    captured = []
    core = _core(db, [_text_response('{"n": 3}')], captured)
    spec = validate_spec({"meta": {"name": "f"}, "nodes": [
        {"id": "a", "type": "agent", "prompt": "n", "schema": _SCHEMA}]})
    try:
        assert WorkflowEngine(core, budget=Budget()).run(spec, {}).outputs["a"] == {"n": 3}
        assert "tool_choice" not in captured[0].calls[0]  # NOT forced
    finally:
        core.shutdown()
