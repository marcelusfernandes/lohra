"""Tests for GatewaySession — the prompt.submit -> events -> complete core (spec §2).

Exercised synchronously with a fake client and a collector sink, so the
protocol sequence is asserted without WS/threading.
"""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.agent.context import ContextEngine
from lohra.gateway.session import GatewaySession
from lohra.providers import get_provider_profile
from lohra.state import SessionDB


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


def _tool_call(cid, name, inp):
    return {
        "content": [{"type": "tool_use", "id": cid, "name": name, "input": inp}],
        "stop_reason": "tool_use",
        "usage": None,
    }


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _session(db, responses, *, tool_dispatch=None, tool_definitions=()):
    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=FakeClient(responses),
        tool_definitions=tool_definitions,
        tool_dispatch=tool_dispatch,
    )
    db.create_session("s1", model=agent.model)
    return GatewaySession("s1", agent, db)


def _types(frames):
    return [f["params"]["type"] for f in frames]


def test_submit_emits_start_deltas_complete(db):
    session = _session(db, [_text("hello world")])
    frames = []
    session.submit("hi", frames.append)
    assert _types(frames) == ["message.start", "message.delta", "message.complete"]
    assert frames[1]["params"]["payload"]["text"] == "hello world"
    complete = frames[-1]["params"]["payload"]
    assert complete["text"] == "hello world"
    assert complete["status"] == "complete"


def test_submit_emits_tool_lifecycle(db):
    def dispatch(name, args):
        return '{"ok": true, "data": "file body"}'

    session = _session(
        db,
        [_tool_call("tc1", "read_file", {"path": "a.txt"}), _text("done")],
        tool_dispatch=dispatch,
        tool_definitions=({"type": "function", "function": {"name": "read_file"}},),
    )
    frames = []
    session.submit("read it", frames.append)
    assert _types(frames) == [
        "message.start",
        "tool.start",
        "tool.complete",
        "message.delta",
        "message.complete",
    ]
    start = frames[1]["params"]["payload"]
    assert start["name"] == "read_file"
    assert start["tool_id"]
    done = frames[2]["params"]["payload"]
    assert done["tool_id"] == start["tool_id"]
    assert "file body" in done["result"]


def test_submit_persists_messages(db):
    session = _session(db, [_text("persisted reply")])
    session.submit("first", lambda _f: None)
    msgs = db.load_messages("s1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "first"


def test_submit_resumes_prior_history(db):
    session = _session(db, [_text("one"), _text("two")])
    session.submit("first", lambda _f: None)
    session.submit("second", lambda _f: None)
    msgs = db.load_messages("s1")
    assert [m["content"] for m in msgs] == ["first", "one", "second", "two"]


def test_busy_session_rejects_concurrent_submit(db):
    session = _session(db, [_text("x")])
    session._busy.acquire()  # simulate an in-flight turn
    frames = []
    result = session.submit("hi", frames.append)
    assert result["busy"] is True
    assert _types(frames) == ["error"]
    assert "busy" in frames[0]["params"]["payload"]["message"]


class _AlwaysCompress(ContextEngine):
    """A context engine that always compacts the next turn (deterministic test)."""

    def should_compress(self, prompt_tokens, context_window):
        return True

    def compress(self, messages, *, summarize):
        summarize("middle")  # exercise the injected summarizer
        return [{"role": "user", "content": "[COMPACTED]"}, *messages]


class _FakeAux:
    def summarizer(self):
        return lambda _text: "SUMMARY"


def _compacting_session(db, on_compaction):
    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=FakeClient([_text("after compaction")]),
        context_engine=_AlwaysCompress(),
        aux_client=_FakeAux(),
    )
    db.create_session("s1", model=agent.model)
    return GatewaySession("s1", agent, db, on_compaction=on_compaction)


def test_compacted_turn_forks_to_child_and_emits_event(db):
    captured = {}

    def on_compaction(parent_id, agent, messages):
        captured["parent"] = parent_id
        captured["messages"] = messages
        return "child123"

    session = _compacting_session(db, on_compaction)
    frames = []
    session.submit("hi", frames.append)

    assert captured["parent"] == "s1"
    # the child receives the full compressed transcript, not just the delta
    assert any(m.get("content") == "[COMPACTED]" for m in captured["messages"])
    forked = [f for f in frames if f["params"]["type"] == "session.forked"]
    assert len(forked) == 1
    payload = forked[0]["params"]["payload"]
    assert payload["parent_session_id"] == "s1"
    assert payload["child_session_id"] == "child123"


def test_compacted_turn_does_not_persist_to_parent(db):
    session = _compacting_session(db, lambda p, a, m: "child123")
    session.submit("hi", lambda _f: None)
    # the lineage split persists to the child (via on_compaction), never the parent
    assert db.load_messages("s1") == []


def test_compacted_turn_reports_child_in_message_complete(db):
    session = _compacting_session(db, lambda p, a, m: "child123")
    frames = []
    session.submit("hi", frames.append)
    complete = frames[-1]["params"]["payload"]
    assert complete["status"] == "complete"
    assert complete["child_session_id"] == "child123"


def test_no_compaction_persists_normally_and_omits_fork(db):
    called = []
    session = _session(db, [_text("plain reply")])
    session._on_compaction = lambda *a: called.append(a) or "x"  # type: ignore[attr-defined]
    frames = []
    session.submit("hi", frames.append)
    assert called == []  # never invoked when the turn did not compact
    assert [f["params"]["type"] for f in frames if f["params"]["type"] == "session.forked"] == []
    assert [m["role"] for m in db.load_messages("s1")] == ["user", "assistant"]


def test_error_turn_marks_complete_status_error(db):
    class Boom(ModelClient):
        def create(self, **kwargs):
            raise RuntimeError("api down")

        def stream(self, **kwargs):
            raise RuntimeError("api down")

    agent = Agent(model="m", provider=get_provider_profile("anthropic"), client=Boom())
    db.create_session("s1", model="m")
    session = GatewaySession("s1", agent, db)
    frames = []
    session.submit("hi", frames.append)
    complete = frames[-1]["params"]["payload"]
    assert complete["status"] == "error"
    # an errored turn is not persisted (no dangling user message)
    assert db.load_messages("s1") == []


# SUP-03: optional on_settle callback + discard_steers on GatewaySession;
# plain enqueue/drain contracts stay identical.


def test_enqueue_steer_callback_fires_read_exactly_on_drain(db):
    session = _session(db, [_text("x")])
    settled = []
    session.enqueue_steer("turn left", on_settle=lambda outcome: settled.append(outcome))
    assert settled == []  # not delivered yet -> no callback
    assert session.drain_steers() == ["turn left"]  # drain delivers the text...
    assert settled == ["read"]  # ...and the callback fires exactly then, 'read'
    assert session.drain_steers() == []  # a second drain delivers nothing...
    assert settled == ["read"]  # ...and must NOT re-fire the callback


def test_discard_steers_empties_without_text_and_marks_discarded(db):
    session = _session(db, [_text("x")])
    settled = []
    session.enqueue_steer("one", on_settle=lambda outcome: settled.append(outcome))
    session.enqueue_steer("two", on_settle=lambda outcome: settled.append(outcome))
    returned = session.discard_steers()
    assert not returned  # empties without handing the texts back (None/empty)
    assert session.drain_steers() == []  # inbox truly empty afterwards
    assert settled == ["discarded", "discarded"]  # every item settled as such


def test_settle_callback_exception_is_fail_isolated_and_outside_lock(db):
    session = _session(db, [_text("x")])
    settled = []
    lock_state = []

    def boom(outcome):
        lock_state.append(session._inbox_lock.locked())
        raise RuntimeError("callback blew up")

    session.enqueue_steer("first", on_settle=boom)
    session.enqueue_steer("second", on_settle=lambda outcome: settled.append(outcome))
    assert session.drain_steers() == ["first", "second"]  # delivery unaffected
    assert lock_state == [False]  # callback ran OUTSIDE the inbox lock
    assert settled == ["read"]  # the second item's callback still ran
    assert session.drain_steers() == []  # inbox fully emptied despite the raise
