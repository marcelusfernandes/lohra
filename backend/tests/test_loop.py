"""Tests for the basic conversation loop (Phase 1 — chat, no tools).

Spec §1: user -> API -> response. The loop reads only NormalizedResponse,
restores-or-builds the frozen system prompt, and returns the result dict
contract. Interrupt is checked between iterations here; mid-call interruption
(thread daemon + poll) is a separate piece.
"""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient, assemble_streamed_response
from lohra.agent.loop import run_conversation
from lohra.providers import get_provider_profile


class FakeClient(ModelClient):
    """Returns canned raw Anthropic-shaped responses; records call kwargs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.stream_calls = 0
        self.closed = False

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeClient called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        # Simulate the SDK's stream -> final-message flow: emit one delta per
        # content block, then return the same raw response normalize() reads.
        self.stream_calls += 1
        raw = self.create(**kwargs)
        if isinstance(raw, dict):
            for block in raw.get("content", []):
                if block.get("type") == "text" and on_text:
                    on_text(block["text"])
                elif block.get("type") == "thinking" and on_reasoning:
                    on_reasoning(block["thinking"])
        return raw

    def close(self):
        self.closed = True


def _text_response(text, stop_reason="end_turn"):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _make_agent(responses, **overrides):
    kwargs = dict(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=FakeClient(responses),
    )
    kwargs.update(overrides)
    return Agent(**kwargs)


# --- happy path ---


def test_single_turn_returns_final_response():
    agent = _make_agent([_text_response("olá!")])
    result = run_conversation(agent, "oi")
    assert result["final_response"] == "olá!"
    assert result["completed"] is True
    assert result["interrupted"] is False
    assert result["error"] is None
    assert result["api_calls"] == 1


def test_user_message_appended_to_history():
    agent = _make_agent([_text_response("hi")])
    result = run_conversation(agent, "hello")
    assert result["messages"][0] == {"role": "user", "content": "hello"}
    assert result["messages"][-1]["role"] == "assistant"
    assert result["messages"][-1]["content"] == "hi"


def test_conversation_history_is_preserved_not_mutated():
    history = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    snapshot = json.loads(json.dumps(history))
    agent = _make_agent([_text_response("now")])
    result = run_conversation(agent, "new", conversation_history=history)
    assert history == snapshot  # caller's list untouched
    assert result["messages"][:2] == snapshot
    assert result["messages"][2] == {"role": "user", "content": "new"}


def test_system_prompt_passed_to_transport():
    agent = _make_agent([_text_response("hi")], system_message="be terse")
    run_conversation(agent, "oi")
    sent = agent.client.calls[0]
    assert "be terse" in sent["system"]
    assert "Lohra" in sent["system"]


def test_assistant_message_carries_finish_reason_and_reasoning():
    raw = {
        "content": [
            {"type": "thinking", "thinking": "pondering", "signature": "sig"},
            {"type": "text", "text": "answer"},
        ],
        "stop_reason": "end_turn",
        "usage": None,
    }
    agent = _make_agent([raw])
    result = run_conversation(agent, "oi")
    msg = result["messages"][-1]
    assert msg["finish_reason"] == "stop"
    assert msg["reasoning"] == "pondering"
    # provider_data carries thinking blocks so a later turn can replay them
    assert msg["provider_data"]["thinking_blocks"][0]["signature"] == "sig"


# --- frozen system prompt (Invariante #1) ---


def test_system_prompt_built_once_and_cached():
    agent = _make_agent([_text_response("a"), _text_response("b")])
    run_conversation(agent, "first")
    snap1 = agent._cached_system_prompt
    run_conversation(agent, "second")
    snap2 = agent._cached_system_prompt
    assert snap1 is snap2  # same frozen object reused across turns


# --- max_tokens resolution (altitude: agent owns the default, not the transport) ---


def test_max_tokens_resolved_from_provider_profile():
    agent = _make_agent([_text_response("hi")])
    run_conversation(agent, "oi")
    # anthropic profile default_max_tokens is 16000
    assert agent.client.calls[0]["max_tokens"] == 16000


def test_explicit_max_tokens_overrides_profile():
    agent = _make_agent([_text_response("hi")], max_tokens=512)
    run_conversation(agent, "oi")
    assert agent.client.calls[0]["max_tokens"] == 512


# --- finish_reason handling ---


def test_length_finish_reason_marks_partial():
    agent = _make_agent([_text_response("truncated", stop_reason="max_tokens")])
    result = run_conversation(agent, "oi")
    assert result["final_response"] == "truncated"
    assert result["partial"] is True
    assert result["completed"] is True


def test_pause_turn_resends_to_continue():
    agent = _make_agent(
        [
            _text_response("partial", stop_reason="pause_turn"),
            _text_response("done", stop_reason="end_turn"),
        ]
    )
    result = run_conversation(agent, "oi")
    assert result["final_response"] == "done"
    assert result["api_calls"] == 2


# --- interruption (between iterations) ---


def test_interrupt_before_first_call():
    agent = _make_agent([_text_response("never")])
    agent.request_interrupt()
    result = run_conversation(agent, "oi")
    assert result["interrupted"] is True
    assert result["completed"] is False
    assert result["api_calls"] == 0
    assert agent.client.calls == []


def test_interrupt_is_consumed_so_reused_agent_runs_next_turn():
    # A turn consumes the interrupt flag; the next turn must start clean.
    agent = _make_agent([_text_response("second turn")])
    agent.request_interrupt()
    first = run_conversation(agent, "first")
    assert first["interrupted"] is True
    assert agent._interrupt_requested is False  # consumed
    second = run_conversation(agent, "second")
    assert second["interrupted"] is False
    assert second["final_response"] == "second turn"


# --- error handling ---


def test_client_error_is_captured():
    agent = _make_agent([RuntimeError("boom")])
    result = run_conversation(agent, "oi")
    assert result["error"] == "boom"
    assert result["completed"] is False
    assert result["final_response"] is None


# --- input sanitization ---


def test_lone_surrogates_are_sanitized():
    agent = _make_agent([_text_response("ok")])
    result = run_conversation(agent, "bad \ud800 surrogate")
    user_content = result["messages"][0]["content"]
    json.dumps(user_content)  # must not raise
    assert "\ud800" not in user_content


# --- bound on runaway loops ---


def test_max_iterations_bounds_pause_loop():
    agent = _make_agent([_text_response("p", stop_reason="pause_turn")] * 20, max_iterations=3)
    result = run_conversation(agent, "oi")
    assert result["api_calls"] == 3
    assert result["completed"] is False
    # Exhaustion must not look like a silent clean stop — it carries an error.
    assert "max_iterations" in result["error"]


def test_content_filter_is_terminal_completion():
    agent = _make_agent([_text_response("I can't help with that.", stop_reason="refusal")])
    result = run_conversation(agent, "oi")
    assert result["final_response"] == "I can't help with that."
    assert result["completed"] is True
    assert result["partial"] is False


def test_clean_empty_stop_is_completed_with_none_response():
    # No text blocks on a clean end_turn -> content is None but the turn completed.
    raw = {"content": [], "stop_reason": "end_turn", "usage": None}
    agent = _make_agent([raw])
    result = run_conversation(agent, "oi")
    assert result["final_response"] is None
    assert result["completed"] is True
    assert result["error"] is None


def test_non_str_user_message_raises():
    agent = _make_agent([])
    with pytest.raises(TypeError, match="user_message must be str"):
        run_conversation(agent, 123)  # type: ignore[arg-type]


# --- tool dispatch (Phase 2) ---


def _tool_call_response(calls, text=None):
    """Raw Anthropic response with tool_use blocks."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for cid, name, inp in calls:
        content.append({"type": "tool_use", "id": cid, "name": name, "input": inp})
    return {"content": content, "stop_reason": "tool_use", "usage": None}


def test_tool_call_executed_and_loop_continues():
    dispatched = []

    def dispatch(name, args):
        dispatched.append((name, args))
        return '{"ok": true, "data": "file contents"}'

    agent = _make_agent(
        [
            _tool_call_response([("tc_1", "read_file", {"path": "a.txt"})], text="reading"),
            _text_response("here is the file"),
        ],
        tool_dispatch=dispatch,
        tool_definitions=({"type": "function", "function": {"name": "read_file"}},),
    )
    result = run_conversation(agent, "read a.txt")
    assert dispatched == [("read_file", {"path": "a.txt"})]
    assert result["final_response"] == "here is the file"
    assert result["api_calls"] == 2
    # a role:"tool" result message sits between the two assistant turns
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "tc_1"
    assert tool_msgs[0]["name"] == "read_file"
    assert "file contents" in tool_msgs[0]["content"]


def test_tool_definitions_passed_to_transport():
    defs = ({"type": "function", "function": {"name": "read_file"}},)
    agent = _make_agent([_text_response("hi")], tool_definitions=defs)
    run_conversation(agent, "oi")
    assert agent.client.calls[0]["tools"] == [
        {"name": "read_file", "description": "", "input_schema": {"type": "object", "properties": {}}}
    ]


def test_multiple_tool_calls_execute_in_order():
    def dispatch(name, args):
        return f'{{"tool": "{name}", "n": {args["n"]}}}'

    agent = _make_agent(
        [
            _tool_call_response(
                [
                    ("tc_1", "t", {"n": 1}),
                    ("tc_2", "t", {"n": 2}),
                    ("tc_3", "t", {"n": 3}),
                ]
            ),
            _text_response("done"),
        ],
        tool_dispatch=dispatch,
    )
    result = run_conversation(agent, "go")
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["tc_1", "tc_2", "tc_3"]
    assert ['"n": 1' in tool_msgs[0]["content"],
            '"n": 2' in tool_msgs[1]["content"],
            '"n": 3' in tool_msgs[2]["content"]] == [True, True, True]


def test_tool_args_parsed_from_json_string():
    seen = {}

    def dispatch(name, args):
        seen.update(args)
        return "{}"

    agent = _make_agent(
        [
            _tool_call_response([("tc_1", "t", {"path": "x", "n": 5})]),
            _text_response("ok"),
        ],
        tool_dispatch=dispatch,
    )
    run_conversation(agent, "go")
    assert seen == {"path": "x", "n": 5}


def test_tool_calls_without_dispatch_degrades_to_terminal():
    agent = _make_agent([_tool_call_response([("tc_1", "t", {})], text="I would call a tool")])
    result = run_conversation(agent, "go")
    assert result["api_calls"] == 1
    assert result["final_response"] == "I would call a tool"


@pytest.mark.parametrize(
    "calls",
    (
        [],
        [(None, "read_file", {})],
        [("tc_1", "read_file", {}), (None, "write_file", {})],
        [("tc_1", "", {})],
    ),
)
def test_incomplete_tool_calls_return_a_protocol_error_instead_of_crashing(calls):
    dispatched = []
    agent = _make_agent(
        [_tool_call_response(calls, text="partial")],
        tool_dispatch=lambda name, args: dispatched.append((name, args)) or "{}",
    )

    result = run_conversation(agent, "go")

    assert result["completed"] is False
    assert result["final_response"] is None
    assert result["error"] == "provider returned incomplete tool_calls"
    assert result["api_calls"] == 1
    assert result["messages"] == [{"role": "user", "content": "go"}]
    assert dispatched == []


def test_incomplete_non_stream_chat_completion_is_rejected_before_dispatch():
    raw = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": None,
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": None,
    }
    dispatched = []
    agent = Agent(
        model="test-model",
        provider=get_provider_profile("openrouter"),
        client=FakeClient([raw]),
        tool_dispatch=lambda name, args: dispatched.append((name, args)) or "{}",
    )

    result = run_conversation(agent, "go")

    assert result["error"] == "provider returned incomplete tool_calls"
    assert result["messages"] == [{"role": "user", "content": "go"}]
    assert dispatched == []


def test_incomplete_chat_completion_stream_becomes_a_turn_error():
    class PartialToolStreamClient(ModelClient):
        def create(self, **kwargs):  # pragma: no cover - streaming is the subject
            raise AssertionError("create must not be called")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return assemble_streamed_response(
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": None,
                                            "function": {"name": "read_file", "arguments": "{}"},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    },
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                ],
                on_text=on_text,
                on_reasoning=on_reasoning,
            )

    agent = Agent(
        model="test-model",
        provider=get_provider_profile("openrouter"),
        client=PartialToolStreamClient(),
    )

    result = run_conversation(agent, "go", stream_delta_callback=lambda _text: None)

    assert result["completed"] is False
    assert result["error"] == "incomplete tool-call stream"
    assert result["api_calls"] == 1


def test_dispatch_exception_becomes_tool_error_and_loop_continues():
    def dispatch(name, args):
        raise RuntimeError("kaboom")

    agent = _make_agent(
        [
            _tool_call_response([("tc_1", "t", {})]),
            _text_response("recovered"),
        ],
        tool_dispatch=dispatch,
    )
    result = run_conversation(agent, "go")
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert "error" in tool_msgs[0]["content"]
    assert result["final_response"] == "recovered"


# --- context compaction (Phase 5) ---


class _AlwaysSummary(ModelClient):
    def create(self, **kwargs):
        return _text_response("COMPACTED SUMMARY")


def test_loop_compacts_long_history_before_calling():
    from lohra.agent.aux import AuxClient
    from lohra.agent.context import ContextCompressor
    from lohra.providers.transports import get_transport

    history = []
    for i in range(20):
        if i % 2 == 0:
            history.append({"role": "user", "content": "x" * 40})
        else:
            history.append({"role": "assistant", "content": "y" * 40, "finish_reason": "stop"})

    agent = _make_agent(
        [_text_response("done")],
        context_window=100,  # threshold 50 tokens; the history estimates well above
        context_engine=ContextCompressor(protect_first_n=2, protect_last_n=2),
        aux_client=AuxClient(
            client=_AlwaysSummary(),
            transport=get_transport("anthropic_messages"),
            model="claude-haiku-4-5",
        ),
    )
    result = run_conversation(agent, "new question", conversation_history=history)
    assert result["compacted"] is True
    assert result["final_response"] == "done"
    # the summary replaced the middle; far fewer messages reach the API
    assert any("COMPACTED SUMMARY" in (m.get("content") or "") for m in result["messages"])
    assert len(agent.client.calls[0]["messages"]) < len(history)


def test_no_engine_means_no_compaction():
    agent = _make_agent([_text_response("hi")])
    result = run_conversation(agent, "oi")
    assert result["compacted"] is False


# --- streaming callbacks (spec §6) ---


def test_stream_delta_callback_receives_text():
    chunks = []
    agent = _make_agent([_text_response("streamed reply")])
    result = run_conversation(agent, "oi", stream_delta_callback=chunks.append)
    assert "".join(chunks) == "streamed reply"
    assert result["final_response"] == "streamed reply"


def test_reasoning_callback_receives_thinking():
    reasoning = []
    raw = {
        "content": [
            {"type": "thinking", "thinking": "let me think", "signature": "s"},
            {"type": "text", "text": "answer"},
        ],
        "stop_reason": "end_turn",
        "usage": None,
    }
    agent = _make_agent([raw])
    result = run_conversation(agent, "oi", reasoning_callback=reasoning.append)
    assert "".join(reasoning) == "let me think"
    assert result["final_response"] == "answer"


def test_no_callbacks_uses_non_streaming_path():
    agent = _make_agent([_text_response("hi")])
    run_conversation(agent, "oi")
    assert agent.client.stream_calls == 0  # non-streaming create() path
    assert len(agent.client.calls) == 1


def test_callback_uses_streaming_path():
    agent = _make_agent([_text_response("hi")])
    run_conversation(agent, "oi", stream_delta_callback=lambda _t: None)
    assert agent.client.stream_calls == 1


# --- usage_total (turn-level accumulator) ---


def _paused_response(text, usage):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "pause_turn", "usage": usage}


def test_usage_total_sums_every_api_call():
    agent = _make_agent(
        [
            _paused_response(
                "…",
                {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 40},
            ),
            {
                "content": [{"type": "text", "text": "fim"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 150, "output_tokens": 20, "cache_read_input_tokens": 90},
            },
        ]
    )
    result = run_conversation(agent, "oi")
    # "usage" keeps its documented meaning: the LAST call's usage
    assert result["usage"].input_tokens == 150
    total = result["usage_total"]
    assert total.input_tokens == 250
    assert total.output_tokens == 30
    assert total.cache_read_tokens == 130


def test_usage_total_none_when_provider_never_reports():
    raw = {"content": [], "stop_reason": "end_turn", "usage": None}
    agent = _make_agent([raw])
    result = run_conversation(agent, "oi")
    assert result["usage_total"] is None


# --- agent helpers ---


def test_agent_transport_resolves_from_api_mode():
    agent = _make_agent([])
    assert agent.transport.api_mode == "anthropic_messages"


def test_agent_unknown_api_mode_raises():
    from lohra.providers.base import ProviderProfile

    profile = ProviderProfile(name="ghost", api_mode="no_such_mode")  # no transport
    agent = Agent(model="gpt", provider=profile, client=FakeClient([]))
    with pytest.raises(LookupError, match="no_such_mode"):
        _ = agent.transport


def test_preflight_reads_the_whole_prompt_not_just_the_uncached_slice():
    """A cached prompt still OCCUPIES the window (Fatia C).

    Since the transports normalize to the disjoint convention, ``input_tokens``
    is only the slice the provider did NOT serve from cache. Reading it alone as
    the occupancy inverts the signal — the longer the conversation, the more of
    it is cached, the SMALLER the number — and the turn silently overflows the
    window instead of compacting. Occupancy is all four prompt meters plus the
    output that just joined the history.
    """
    from lohra.agent.aux import AuxClient
    from lohra.agent.context import ContextCompressor
    from lohra.providers.transports import get_transport

    history = []
    for i in range(20):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": "x" * 40})

    # Iteration 1: a tool call whose usage says 190,500 tokens of window are in
    # use — 185k of them served from cache. Iteration 2's preflight must see it.
    first = _tool_call_response([("tc_1", "read_file", {"path": "a.txt"})])
    first["usage"] = {
        "input_tokens": 5_000,
        "output_tokens": 500,
        "cache_read_input_tokens": 185_000,
        "cache_creation_input_tokens": 0,
    }
    agent = _make_agent(
        [first, _text_response("done")],
        context_window=200_000,  # threshold 100,000
        context_engine=ContextCompressor(protect_first_n=2, protect_last_n=2),
        aux_client=AuxClient(
            client=_AlwaysSummary(),
            transport=get_transport("anthropic_messages"),
            model="claude-haiku-4-5",
        ),
        tool_dispatch=lambda name, args: '{"ok": true}',
        tool_definitions=({"type": "function", "function": {"name": "read_file"}},),
    )
    result = run_conversation(agent, "go", conversation_history=history)
    assert result["compacted"] is True
    assert any("COMPACTED SUMMARY" in (m.get("content") or "") for m in result["messages"])


def test_a_failing_compaction_degrades_instead_of_killing_the_turn():
    # Épico OBS, 2026-08-29: o aux 400ou no preflight e o turno INTEIRO morreu
    # antes de o agente ver qualquer coisa. Falha de compactação é degradável
    # por definição — segue sem comprimir, avisa, e o turno continua.
    from lohra.agent.loop import run_conversation

    class ExplodingAux:
        def summarizer(self):
            def _fail(_messages):
                raise RuntimeError("aux backend rejected the call")
            return _fail

    class AlwaysCompress:
        def should_compress(self, *_a):
            return True

        def compress(self, messages, summarize):
            summarize(messages)  # explode
            return messages

    agent = Agent(
        model="m",
        provider=get_provider_profile("anthropic"),
        client=FakeClient([_text_response("sobrevivi")]),
        context_engine=AlwaysCompress(),
        aux_client=ExplodingAux(),
    )
    result = run_conversation(agent, "oi")
    assert result["error"] is None
    assert result["final_response"] == "sobrevivi"
    assert result["compacted"] is False


def test_a_failing_compaction_is_latched_and_not_retried_every_iteration():
    """Degrading is per TURN, not per round-trip.

    The try/except lives inside the iteration loop and ``compacted`` only flips
    on success, so an aux that is down (the Codex 400 that motivated the
    degrade) was re-called on every tool round-trip — one failed HTTP call per
    iteration, up to ``max_iterations``, each logging a warning.
    """
    from lohra.agent.loop import run_conversation

    attempts = []

    class ExplodingAux:
        def summarizer(self):
            def _fail(_messages):
                attempts.append(1)
                raise RuntimeError("aux backend rejected the call")
            return _fail

    class AlwaysCompress:
        def should_compress(self, *_a):
            return True

        def compress(self, messages, summarize):
            summarize(messages)  # explode
            return messages

    agent = _make_agent(
        [
            _tool_call_response([(f"tc_{index}", "t", {"n": index})])
            for index in range(5)
        ]
        + [_text_response("sobrevivi")],
        context_engine=AlwaysCompress(),
        aux_client=ExplodingAux(),
        tool_dispatch=lambda name, args: "{}",
    )
    result = run_conversation(agent, "oi")
    assert result["error"] is None
    assert result["final_response"] == "sobrevivi"
    assert len(agent.client.calls) == 6, "the turn really took six round-trips"
    assert len(attempts) == 1, f"aux was re-called every iteration ({len(attempts)}x)"


# --- request_overlay (SUP-05) ---


def test_request_overlay_is_embedded_in_the_current_user_message():
    """O overlay entra DENTRO da user message do turno — nunca como uma
    user message extra (user/user quebra providers), nunca no system prompt."""
    agent = _make_agent([_text_response("ok")])
    result = run_conversation(
        agent, "oi", request_overlay="AVISOS OPERACIONAIS: quota baixa"
    )
    sent = agent.client.calls[0]["messages"]
    user = [m for m in sent if m["role"] == "user"]
    assert len(user) == 1, "overlay não pode criar user/user"
    assert "oi" in user[0]["content"]
    assert "AVISOS OPERACIONAIS: quota baixa" in user[0]["content"]
    # system prompt intocado
    assert "AVISOS" not in agent.client.calls[0]["system"]
    # a história do result NÃO contém o overlay (provider-facing only)
    stored = [m for m in result["messages"] if m["role"] == "user"]
    assert len(stored) == 1
    assert "AVISOS" not in stored[0]["content"]


def test_request_overlay_is_reapplied_on_every_api_call_of_the_turn():
    agent = _make_agent(
        [_tool_call_response([("tc_1", "t", {"n": 1})]), _text_response("fim")],
        tool_dispatch=lambda name, args: "{}",
    )
    run_conversation(agent, "oi", request_overlay="NOTICE-X")
    assert len(agent.client.calls) == 2
    for call in agent.client.calls:
        joined = json.dumps(call["messages"], ensure_ascii=False)
        assert "NOTICE-X" in joined, "overlay ausente de uma chamada do turno"
        assert joined.count("NOTICE-X") == 1


def test_request_overlay_none_is_byte_identical():
    agent = _make_agent([_text_response("ok")])
    run_conversation(agent, "oi")
    baseline = json.dumps(agent.client.calls[0]["messages"])
    agent2 = _make_agent([_text_response("ok")])
    run_conversation(agent2, "oi", request_overlay=None)
    assert json.dumps(agent2.client.calls[0]["messages"]) == baseline


def test_request_overlay_empty_string_is_ignored():
    agent = _make_agent([_text_response("ok")])
    run_conversation(agent, "oi", request_overlay="   ")
    assert agent.client.calls[0]["messages"][0]["content"] == "oi"


def test_request_overlay_survives_compaction_copy():
    """Compressão substitui a lista de mensagens; o overlay é reaplicado por
    cima da lista ATUAL a cada call — não depende da referência original."""

    class Shrink:
        def should_compress(self, *_a):
            return True

        def compress(self, messages, summarize):
            return [m for m in messages if m.get("role") == "user"]

    agent = _make_agent(
        [
            _tool_call_response([("tc_1", "t", {"n": 1})]),
            _text_response("fim"),
        ],
        tool_dispatch=lambda name, args: "{}",
        context_engine=Shrink(),
    )
    run_conversation(agent, "oi", request_overlay="PERSIST")
    for call in agent.client.calls:
        assert "PERSIST" in json.dumps(call["messages"])
