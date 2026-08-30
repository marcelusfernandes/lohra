"""Deterministic comparison: steering (inbox) vs cancel-respawn vs fresh turn.

Three strategies to correct a running agent, compared by token cost and tool
dispatches. All usage numbers are explicit and additive (in + out):

- Steering: tool-call 100 in/10 out + final CORRECT 120 in/10 out -> 240.
- Cancel-respawn: interrupted turn 110 + respawned turn 240 -> 350.
- Fresh simple turn: CORRECT 40 in/5 out -> 45 (cheapest of all).
"""

from lohra.agent.agent import Agent
from lohra.agent.loop import run_conversation
from lohra.providers import get_provider_profile

from tests.test_loop import FakeClient, _text_response, _tool_call_response


def _usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "output_tokens": out}


def _turn_total(result) -> int:
    """Total tokens billed in a turn (input + output, summed over calls)."""
    u = result["usage_total"]
    return u.input_tokens + u.output_tokens


def _tool_response(inp: int, out: int, *, tool_id: str = "c1", name: str = "noop"):
    """Tool-call response with EXPLICIT usage (default helper sends usage=None)."""
    raw = _tool_call_response([(tool_id, name, {})])
    raw["usage"] = _usage(inp, out)
    return raw


def _text(inp: int, out: int, text: str):
    raw = _text_response(text)
    raw["usage"] = _usage(inp, out)
    return raw


def _agent(responses, **overrides) -> Agent:
    kwargs = dict(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=FakeClient(responses),
        tool_dispatch=lambda name, args: "{}",
        tool_definitions=({"type": "function", "function": {"name": "noop"}},),
    )
    kwargs.update(overrides)
    return Agent(**kwargs)


def _tool_msgs(result) -> list:
    return [m for m in result["messages"] if m["role"] == "tool"]


# ---------------------------------------------------------------------------
# Strategy A — steering via inbox: the correction arrives between iterations
# and is injected as a system-reminder before the final call. One turn, one
# tool dispatch, 240 tokens total (100+10 tool call + 120+10 final).
# ---------------------------------------------------------------------------


def test_steering_inbox_converges_with_240_tokens_and_one_tool_call():
    agent = _agent(
        [
            _tool_response(100, 10),
            _text(120, 10, "CORRECT"),
        ]
    )

    # Correction is queued while the tool runs; drained only on iteration 2.
    drains: list[int] = []

    def inbox():
        drains.append(1)
        return ["correction: fix the approach"] if len(drains) == 2 else []

    result = run_conversation(agent, "start", inbox=inbox)

    # Converged: final response, not interrupted, not errored.
    assert result["final_response"] == "CORRECT"
    assert result["completed"] is True
    assert result["interrupted"] is False
    assert result["error"] is None
    assert result["api_calls"] == 2

    # Token economy: exactly 240 across the two calls (220 in + 20 out).
    assert result["usage_total"] is not None
    assert result["usage_total"].input_tokens == 220
    assert result["usage_total"].output_tokens == 20
    assert _turn_total(result) == 240

    # Tool ran exactly once; the reminder was injected in iteration 2 (after
    # the tool result, before the final text).
    assert len(_tool_msgs(result)) == 1
    reminders = [
        m
        for m in result["messages"]
        if m["role"] == "user" and "system-reminder" in m["content"]
    ]
    assert len(reminders) == 1
    assert "correction: fix the approach" in reminders[0]["content"]
    assert reminders[0] is result["messages"][-2]


# ---------------------------------------------------------------------------
# Strategy B — cancel + respawn: interrupt the first Agent mid-turn, then
# answer with a corrected prompt on a fresh Agent. Same quality (CORRECT),
# but 350 tokens (110 interrupted + 240 respawned) and the tool ran twice.
# ---------------------------------------------------------------------------


def test_cancel_respawn_repeats_work_350_tokens_and_two_tool_calls():
    # --- Turn 1: same tool-call, but dispatch requests an interrupt. ---
    first_agent = _agent([_tool_response(100, 10)])

    def dispatch_with_interrupt(name, args):
        first_agent.request_interrupt()
        return "{}"

    first_agent.tool_dispatch = dispatch_with_interrupt

    first = run_conversation(first_agent, "start")
    assert first["interrupted"] is True
    assert first["completed"] is False
    assert first["final_response"] is None
    assert len(_tool_msgs(first)) == 1  # work already spent...
    assert first["usage_total"].input_tokens == 100  # ...and billed: 100/10
    assert first["usage_total"].output_tokens == 10
    assert _turn_total(first) == 110

    # --- Turn 2: respawn with the corrected prompt — repeats the tool call. ---
    second_agent = _agent(
        [
            _tool_response(100, 10, tool_id="c2"),
            _text(120, 10, "CORRECT"),
        ]
    )
    second = run_conversation(second_agent, "start, but corrected: fix the approach")

    assert second["final_response"] == "CORRECT"
    assert second["interrupted"] is False
    assert second["completed"] is True
    assert len(_tool_msgs(second)) == 1
    assert _turn_total(second) == 240

    # --- Cross-strategy accounting. ---
    respawn_total = _turn_total(first) + _turn_total(second)
    assert respawn_total == 350

    # The tool ran twice (once per Agent) — wasted work steering avoids.
    total_tool_msgs = len(_tool_msgs(first)) + len(_tool_msgs(second))
    assert total_tool_msgs == 2

    # Same quality outcome as steering (CORRECT), but 110 tokens dearer.
    assert respawn_total == 240 + 110


# ---------------------------------------------------------------------------
# Counterpoint — a fresh turn that is simply correct from the start is the
# cheapest path of all: 40 in + 5 out = 45 tokens, well under steering's 240.
# ---------------------------------------------------------------------------


def test_fresh_correct_turn_is_cheaper_than_steering():
    agent = _agent([_text(40, 5, "CORRECT")])
    result = run_conversation(agent, "start")

    assert result["final_response"] == "CORRECT"
    assert result["completed"] is True
    assert result["usage_total"].input_tokens == 40
    assert result["usage_total"].output_tokens == 5
    fresh_total = _turn_total(result)
    assert fresh_total == 45

    # Economy ordering: fresh < steering < cancel-respawn.
    assert fresh_total < 240 < 350
