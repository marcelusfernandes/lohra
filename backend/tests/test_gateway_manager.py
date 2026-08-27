"""Tests for SessionManager + SessionDB.list_sessions."""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.gateway.manager import SessionManager
from lohra.providers import get_provider_profile
from lohra.state import SessionDB


class _FakeClient(ModelClient):
    def create(self, **kwargs):
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": None}


def _factory(_session_id: str):
    return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"), client=_FakeClient())


@pytest.fixture
def manager():
    db = SessionDB(":memory:")
    yield SessionManager(db, _factory)
    db.close()


def test_create_session_generates_id(manager):
    session = manager.create_session()
    assert session.session_id
    assert manager.db.get_session(session.session_id) is not None


def test_create_session_with_explicit_id_is_idempotent(manager):
    a = manager.create_session(session_id="s1")
    b = manager.create_session(session_id="s1")
    assert a is b  # same live session reused


def test_get_unknown_session_returns_none(manager):
    assert manager.get("nope") is None


def test_get_revives_persisted_session(manager):
    manager.create_session(session_id="s1")
    # drop the in-memory live cache to force a revive from the DB
    manager._sessions.clear()
    revived = manager.get("s1")
    assert revived is not None
    assert revived.session_id == "s1"


def test_the_factory_is_told_which_session_it_is_building_for():
    """Tools bound into the agent's dispatch (run_workflow's owner, the
    orchestration parent) are fixed at construction, so the factory has to see
    the session id — on the create path AND on the revive path."""
    seen: list[str] = []

    def spy(session_id: str):
        seen.append(session_id)
        return _factory(session_id)

    db = SessionDB(":memory:")
    try:
        manager = SessionManager(db, spy)
        manager.create_session(session_id="s1")
        manager._sessions.clear()  # force a revive from the DB
        assert manager.get("s1") is not None
        assert seen == ["s1", "s1"]
    finally:
        db.close()


def test_list_sessions_newest_first(manager):
    manager.create_session(session_id="old", title="first")
    manager.create_session(session_id="new", title="second")
    listed = manager.list_sessions()
    ids = [s["id"] for s in listed]
    assert ids.index("new") < ids.index("old")
    assert {"id", "title", "model", "message_count"} <= set(listed[0].keys())


def test_history_returns_persisted_messages(manager):
    session = manager.create_session(session_id="s1")
    session.submit("hi", lambda _f: None)
    history = manager.history("s1")
    assert [m["role"] for m in history] == ["user", "assistant"]


# --- lineage fork on compaction ---


def test_fork_for_compaction_persists_child_with_parent_lineage(manager):
    parent = manager.create_session(session_id="p1")
    messages = [
        {"role": "user", "content": "[COMPACTED]"},
        {"role": "assistant", "content": "ok"},
    ]
    child_id = manager.fork_for_compaction("p1", parent.agent, messages)

    assert child_id != "p1"
    row = manager.db.get_session(child_id)
    assert row is not None
    assert row["parent_session_id"] == "p1"
    # the full compressed transcript is persisted on the child
    assert [m["content"] for m in manager.db.load_messages(child_id)] == ["[COMPACTED]", "ok"]


def test_fork_for_compaction_marks_parent_ended_with_reason(manager):
    parent = manager.create_session(session_id="p1")
    manager.fork_for_compaction("p1", parent.agent, [{"role": "user", "content": "x"}])
    row = manager.db.get_session("p1")
    assert row["end_reason"] == "compression"
    assert row["ended_at"] is not None


def test_fork_for_compaction_registers_resumable_child(manager):
    parent = manager.create_session(session_id="p1")
    child_id = manager.fork_for_compaction("p1", parent.agent, [{"role": "user", "content": "x"}])
    child = manager.get(child_id)
    assert child is not None
    assert child.session_id == child_id
    # the child reuses the parent's agent (frozen system prompt, Invariante #1)
    assert child.agent is parent.agent


def test_fork_skipped_when_another_process_holds_the_lock(manager):
    """If another process is already forking this session, don't double-fork."""
    parent = manager.create_session(session_id="p1")
    # simulate a concurrent process holding the compaction lock on p1
    manager.db.acquire_compression_lock("p1", "other-process")

    child_id = manager.fork_for_compaction("p1", parent.agent, [{"role": "user", "content": "x"}])

    assert child_id is None  # fork was skipped
    # the parent must NOT have been ended, and no child created
    assert manager.db.get_session("p1")["end_reason"] is None
    assert "p1" in manager._sessions  # parent left intact, not evicted


def test_fork_evicts_parent_and_refuses_resume(manager):
    parent = manager.create_session(session_id="p1")
    manager.fork_for_compaction("p1", parent.agent, [{"role": "user", "content": "x"}])
    # the dead parent is evicted from the live cache...
    assert "p1" not in manager._sessions
    # ...and cannot be revived for new turns (client must use the child)
    assert manager.get("p1") is None


def test_child_reuses_parent_busy_lock(manager):
    parent = manager.create_session(session_id="p1")
    child_id = manager.fork_for_compaction("p1", parent.agent, [{"role": "user", "content": "x"}])
    child = manager.get(child_id)
    # same lock object => two turns can never drive the shared Agent at once
    assert child._busy is parent._busy


def test_compaction_fork_is_end_to_end_through_submit(manager, monkeypatch):
    """A turn that compacts forks a live, resumable child registered in the manager."""
    from lohra.agent.context import ContextEngine

    class _AlwaysCompress(ContextEngine):
        def should_compress(self, prompt_tokens, context_window):
            return True

        def compress(self, messages, *, summarize):
            return [{"role": "user", "content": "[COMPACTED]"}, *messages]

    class _FakeAux:
        def summarizer(self):
            return lambda _t: "S"

    parent = manager.create_session(session_id="p1")
    parent.agent.context_engine = _AlwaysCompress()
    parent.agent.aux_client = _FakeAux()

    frames = []
    parent.submit("hi", frames.append)

    forked = [f for f in frames if f["params"]["type"] == "session.forked"]
    assert len(forked) == 1
    child_id = forked[0]["params"]["payload"]["child_session_id"]
    assert manager.get(child_id) is not None
    assert manager.db.get_session(child_id)["parent_session_id"] == "p1"
