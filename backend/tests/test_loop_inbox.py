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


# ---------------------------------------------------------------------------
# SUP-03 — one frozen system prompt across the WHOLE session: four turns on a
# single Agent (first turn; second turn resumed with conversation_history;
# third turn whose steer is read between iterations; fourth turn with forced
# preflight compaction) must ALL send the SAME SystemPromptSnapshot object and
# the SAME system TEXT to the provider. Steer lands only in a user-role
# system-reminder; compaction reports True; every steer is drained exactly
# once across the whole sequence.
# ---------------------------------------------------------------------------


class StubAux:
    """Recordable AuxClient stand-in: summarizer() returns a plain callable."""

    def __init__(self):
        self.summaries: list[str] = []

    def summarizer(self):
        def summarize(transcript: str) -> str:
            self.summaries.append(transcript)
            return f"RESUMO({len(transcript)} chars)"

        return summarize


class ToggleEngine:
    """ContextEngine stub: force toggles compression; compress returns a fixed
    resumo message + the tail of what it was given."""

    def __init__(self):
        self.force = False
        self.compress_calls = 0

    def should_compress(self, prompt_tokens: int, context_window: int) -> bool:
        return self.force

    def compress(self, messages: list[dict], *, summarize) -> list[dict]:
        self.compress_calls += 1
        summarize("transcript placeholder")
        return [{"role": "user", "content": "RESUMO DA CONVERSA"}] + messages[-1:]


def _steer_reminders(messages):
    return [m for m in messages if m["role"] == "user" and "system-reminder" in m["content"]]


def test_sup03_one_frozen_prompt_across_four_turns():
    # Five responses: T1 text | T2 text | T3 tool-call then text | T4 text.
    agent = _agent(
        [
            _text_response("t1 done"),
            _text_response("t2 done"),
            _noop_call(),
            _text_response("t3 done"),
            _text_response("t4 done"),
        ],
        tool_dispatch=lambda name, args: "{}",
    )
    # Distinctive system_message so the sent system text is unambiguous.
    agent.system_message = "SUP03 identity fixture"
    agent.context_window = 1000
    engine = ToggleEngine()
    agent.context_engine = engine
    aux = StubAux()
    agent.aux_client = aux

    # --- Turn 1: freeze the snapshot, run the turn, capture the baseline. ---
    r1 = run_conversation(agent, "turn one")
    assert r1["final_response"] == "t1 done"
    assert r1["compacted"] is False
    snap = agent._cached_system_prompt
    assert snap is not None
    system_texts = [snap.text]

    def assert_same_snapshot_and_text():
        # Same OBJECT (snapshot reused, never rebuilt)...
        assert agent._cached_system_prompt is snap
        # ...and the SAME system TEXT in every main provider call so far,
        # exactly as the frozen snapshot carries it.
        for call in client.calls:
            assert call["system"] == snap.text

    client = agent.client

    # --- Turn 2: resume from turn 1's conversation_history. ---
    r2 = run_conversation(agent, "turn two", conversation_history=r1["messages"])
    assert r2["final_response"] == "t2 done"
    assert len(client.calls) == 2
    assert_same_snapshot_and_text()
    # Turn 1's history flows through untouched (messages are copied in).
    assert r2["messages"][0] == {"role": "user", "content": "turn one"}
    assert r2["messages"][-1]["role"] == "assistant"
    assert r2["messages"][-1]["content"] == "t2 done"

    # --- Turn 3: steer drained between iterations (tool call -> reminder ->
    # text), so the reminder rides in a user message AFTER the tool result. ---
    drains: list[int] = []

    def inbox():
        # First drain (iteration 1, before the noop call) is empty; the steer
        # is queued while the tool runs and read on the SECOND iteration.
        drains.append(1)
        return ["steer: also do X"] if len(drains) == 2 else []

    r3 = run_conversation(
        agent,
        "turn three",
        conversation_history=r2["messages"],
        inbox=inbox,
    )
    assert r3["final_response"] == "t3 done"
    assert len(client.calls) == 4
    assert_same_snapshot_and_text()
    # The steer was delivered EXACTLY once: one reminder message, in a
    # role:'user' message, wrapped as a system-reminder — never in the system.
    reminders = _steer_reminders(r3["messages"])
    assert len(reminders) == 1
    assert reminders[0]["role"] == "user"
    assert "steer: also do X" in reminders[0]["content"]
    # The reminder sits at the TAIL: after the noop tool result, before the
    # final text call — read between iterations, exactly once.
    assert len(drains) == 2
    assert reminders[0] is r3["messages"][-2]
    # No steer leaked into the system prompt of any provider call.
    for call in client.calls:
        assert "steer: also do X" not in call["system"]

    # --- Turn 4: forced compaction before the call. ---
    engine.force = True
    r4 = run_conversation(agent, "turn four", conversation_history=r3["messages"])
    assert r4["final_response"] == "t4 done"
    assert len(client.calls) == 5
    assert_same_snapshot_and_text()
    assert r4["compacted"] is True
    assert engine.compress_calls == 1
    assert len(aux.summaries) == 1
    # The resumo message is in place and the pre-compaction history
    # (t1..t3 turns) was summarized away — only resumo + the new user turn.
    contents = [m["content"] for m in r4["messages"]]
    assert any("RESUMO DA CONVERSA" in c for c in contents)
    assert "t3 done" not in contents
    assert "turn four" in contents

    # Whole-sequence cross-checks.
    assert len(system_texts) == 1
    for call in client.calls:
        assert call["system"] == snap.text
    assert client.calls[0]["messages"][0]["content"] == "turn one"
