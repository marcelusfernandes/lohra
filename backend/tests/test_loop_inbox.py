"""Tests for the steer inbox hook in run_conversation (Phase 7 §6)."""

from lohra.agent.loop import run_conversation
from lohra.providers import get_provider_profile
from tests.test_loop import FakeClient, _text_response, _tool_call_response


def _agent(responses, **overrides):
    from lohra.agent.agent import Agent

    kwargs = dict(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=FakeClient(responses),
    )
    kwargs.update(overrides)
    return Agent(**kwargs)


def _noop_call():
    return _tool_call_response([("c1", "noop", {})])


def test_no_inbox_is_unchanged():
    agent = _agent([_text_response("hi")])
    result = run_conversation(agent, "oi")
    assert result["final_response"] == "hi"


def test_empty_inbox_does_not_inject():
    agent = _agent([_text_response("hi")])
    result = run_conversation(agent, "oi", inbox=lambda: [])
    # only the original user message, no reminder
    users = [m for m in result["messages"] if m["role"] == "user"]
    assert len(users) == 1
    assert "system-reminder" not in users[0]["content"]


def test_steer_text_injected_before_next_iteration():
    # turn does one tool call (iteration 1) then stops (iteration 2). The inbox
    # yields a steer text once — it must appear before iteration 2's call.
    agent = _agent(
        [_noop_call(), _text_response("done")],
        tool_dispatch=lambda name, args: "{}",
    )
    drained = [["please also check X"]]

    def inbox():
        return drained.pop(0) if drained else []

    result = run_conversation(agent, "start", inbox=inbox)
    reminders = [
        m for m in result["messages"] if m["role"] == "user" and "system-reminder" in m["content"]
    ]
    assert len(reminders) == 1
    assert "please also check X" in reminders[0]["content"]


def test_multiple_steer_texts_merge_into_one_message():
    agent = _agent(
        [_noop_call(), _text_response("done")],
        tool_dispatch=lambda name, args: "{}",
    )
    drained = [["first", "second"]]

    def inbox():
        return drained.pop(0) if drained else []

    result = run_conversation(agent, "start", inbox=inbox)
    reminders = [
        m for m in result["messages"] if m["role"] == "user" and "system-reminder" in m["content"]
    ]
    assert len(reminders) == 1  # merged, not two user messages
    assert "first" in reminders[0]["content"]
    assert "second" in reminders[0]["content"]


def test_steer_never_touches_the_system_prompt():
    # Invariante #1: the frozen system prompt is identical regardless of steer.
    agent = _agent(
        [_noop_call(), _text_response("done")],
        tool_dispatch=lambda name, args: "{}",
    )
    before = agent.system_prompt().text
    run_conversation(agent, "start", inbox=lambda: ["steered"])
    assert agent.system_prompt().text == before
