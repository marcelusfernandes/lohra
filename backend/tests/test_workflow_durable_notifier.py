"""Durable integration of the workflow completion notifier (SUP-05).

Until now ``bind_workflow_notifier`` announced a finished run ONLY through the
live, process-local steer inbox of the session that launched it. The moment the
process died — or the session did — that announcement was gone, even though the
run itself survives restarts on its durable line.

Two channels, one fenced gate:

- **live inbox (unchanged)** — ``enqueue_steer`` on the owning session, exactly
  as before. It stays process-local and at-least-once only within the process;
  after a crash the run is recovered and finished elsewhere, and the durable
  channel below is what carries the news.
- **durable notice** — ``DurableNoticeStore`` (via ``SessionDB``), for the OWNER
  session of every NON-CANCELLED terminal completion. TTL 7 days, dedup by
  fingerprint, per-owner cap — all of it store-enforced, not by convention here.
  At-least-once ACROSS processes too: a crash after the callback fires but
  before the consumer acks can deliver the notice again — that is the accepted
  cost of not losing the fact (documented, not fixed).

The fence is the one the service already computes: the ``on_run_done`` callback
is invoked ONLY when the fenced terminal write was accepted (``owned``), so the
durable publish inside it can never land a stale owner's summary over the
recovering owner's run. An ownerless run never publishes and never broadcasts.
"""

import threading

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient

_LEAF_COST = 8


@pytest.fixture
def db(tmp_path):
    """File-backed: notices must survive the Python object that wrote them."""
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


_TWO_NODE = {
    "meta": {"name": "demo", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}


def _service(db, home, responder, *, timers=None, on_run_done=None):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    svc = WorkflowService(
        base_child_factory=factory, db=db, home=home, on_run_done=on_run_done
    )
    if timers is not None:
        from lohra.workflow.autoresume import AutoResumeScheduler

        svc.set_autoresume(
            AutoResumeScheduler(svc.resume, timer_factory=timers, clock=lambda: 1000.0)
        )
    return svc


# --- 1. the durable half of the completion notification --------------------


def test_an_owned_completion_publishes_a_durable_summary(db, tmp_path):
    """Owner session valid, no live inbox anywhere: the summary lands in the
    DurableNoticeStore under the owner's id, so a FRESH process (or the same
    session, later) claims it."""
    from lohra.agent.equip import bind_workflow_notifier

    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        bind_workflow_notifier(svc, lambda sid: None, db=db)
        run_id = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()

    token, rows = db.notices.claim("sess-1")
    assert token is not None
    assert len(rows) == 1
    assert "demo" in rows[0]["text"] and run_id[:8] in rows[0]["text"]
    assert "complete" in rows[0]["text"]


def test_an_ownerless_run_never_publishes_a_notice(db, tmp_path):
    """Ownerless never publish, never broadcast — the store refuses it anyway,
    but the notifier must not even try."""
    from lohra.agent.equip import bind_workflow_notifier

    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        bind_workflow_notifier(svc, lambda sid: None, db=db)
        run_id = svc.start(_TWO_NODE, {})[
            "run_id"
        ]  # no owner kwarg: nobody launched this
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()

    # Nothing to claim for any plausible owner — and the store itself would
    # raise on an ownerless claim, so the silence has to be observable here.
    with pytest.raises(ValueError):
        db.notices.claim("")
    token, rows = db.notices.claim("nobody")
    assert token is None and rows == []


def test_a_cancelled_run_publishes_no_durable_summary(db, tmp_path):
    """The agent asked for the stop; the durable channel carries the same
    courtesy rule as the live one — cancelled tells nobody."""
    from lohra.agent.equip import bind_workflow_notifier

    entered, release = threading.Event(), threading.Event()

    def responder(_prompt: str) -> str:
        entered.set()
        release.wait(5)
        return "R"

    svc = _service(db, tmp_path, responder)
    try:
        bind_workflow_notifier(svc, lambda sid: None, db=db)
        run_id = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        assert entered.wait(5)
        svc.cancel(run_id)
        release.set()
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "cancelled"
    finally:
        release.set()
        svc.shutdown()

    assert db.notices.pending_count("sess-1") == 0


def test_the_notice_is_durable_across_processes(db, tmp_path):
    """File-backed SessionDB: a SECOND SessionDB over the same file claims what
    the first process published — the fact outlives the writer."""
    from lohra.agent.equip import bind_workflow_notifier

    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        bind_workflow_notifier(svc, lambda sid: None, db=db)
        run_id = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()

    reopened = SessionDB(str(tmp_path / "state.db"))
    try:
        token, rows = reopened.notices.claim("sess-1")
        assert token is not None and len(rows) == 1
        assert run_id[:8] in rows[0]["text"]
    finally:
        reopened.close()


def test_republishing_the_same_completion_is_a_dedup_noop(db, tmp_path):
    """The store's fingerprint dedup absorbs a second publish of the same
    summary — the at-least-once window never becomes at-least-twice ON DISK for
    identical facts."""
    from lohra.state.notices import _fingerprint
    from lohra.workflow.notify import done_summary

    summary = done_summary(run_id="abcdef1234567890", status="complete", name="demo", spent=16)
    assert db.notices.publish("sess-1", summary) is True
    assert db.notices.publish("sess-1", summary) is False
    # Whitespace-only spelling differences collapse to the same fingerprint.
    assert db.notices.publish("sess-1", summary + "\n") is False
    assert db.notices.pending_count("sess-1") == 1
    assert _fingerprint(summary) == _fingerprint(summary + " ")


# --- 2. the fence the publish inherits -------------------------------------


def test_the_callback_only_fires_for_an_owned_stretch(db, tmp_path):
    """The service invokes ``on_run_done`` ONLY after the fenced terminal write
    was accepted — so the durable publish inside the callback cannot be landed
    by a stale owner. Pinned at the callback level: a straggler whose terminal
    write was refused never reaches the notifier, hence never publishes."""
    from tests.test_workflow_durable_state import _TWO_NODE as TWO

    now = [7000.0]
    lost_home, fresh_home = tmp_path / "lost", tmp_path / "fresh"
    lost_notes: list = []
    fresh_notes: list = []

    def make(home, responder):
        def factory():
            return Agent(
                model="claude-opus-4-8",
                provider=get_provider_profile("anthropic"),
                client=ScriptedClient(responder),
            )

        return WorkflowService(
            base_child_factory=factory,
            db=db,
            home=home,
            clock=lambda: now[0],
            lease_ttl=100.0,
            on_run_done=lambda *note: (
                lost_notes if home == lost_home else fresh_notes
            ).append(note),
        )

    release = threading.Event()

    def blocking(_prompt: str) -> str:
        release.wait(5)
        return "R"

    lost = make(lost_home, blocking)
    fresh = make(fresh_home, lambda _p: "R")
    try:
        run_id = lost.start(TWO, {})[
            "run_id"
        ]  # the "lost" owner starts the run (lease, no renewal)
        now[0] = 7101.0  # the owner never renewed: its lease lapsed
        assert "error" not in fresh.start(resume_run_id=run_id)
        assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        release.set()
        lost.shutdown()  # joins the straggler's pool: its late writes are DONE
        fresh.shutdown()

    # The straggler's callback never fired => it never published a durable
    # notice for a run it had lost.
    assert lost_notes == []
    assert [note[0] for note in fresh_notes] == [run_id]


def test_no_db_still_delivers_the_live_inbox_only(db, tmp_path):
    """Compat: without ``db``, the binding is exactly the old one — live inbox
    delivery, no durable write, nothing else changes."""
    from lohra.agent.equip import bind_workflow_notifier

    class Inbox:
        def __init__(self) -> None:
            self.texts: list[str] = []

        def enqueue_steer(self, text: str) -> None:
            self.texts.append(text)

    inbox = Inbox()
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        bind_workflow_notifier(svc, lambda sid: inbox if sid == "sess-1" else None)
        run_id = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()

    assert len(inbox.texts) == 1 and "demo" in inbox.texts[0]
    assert db.notices.pending_count("sess-1") == 0


def test_the_live_inbox_is_preserved_beside_the_durable_one(db, tmp_path):
    """Both channels on a valid owner + resolvable session: the live inbox gets
    its summary now (courtesy, process-local) AND the durable store gets the
    fact (7-day TTL, cross-process). One does not replace the other."""
    from lohra.agent.equip import bind_workflow_notifier

    class Inbox:
        def __init__(self) -> None:
            self.texts: list[str] = []

        def enqueue_steer(self, text: str) -> None:
            self.texts.append(text)

    inbox = Inbox()
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        bind_workflow_notifier(svc, lambda sid: inbox if sid == "sess-1" else None, db=db)
        run_id = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()

    assert len(inbox.texts) == 1 and "demo" in inbox.texts[0]
    assert db.notices.pending_count("sess-1") == 1


def test_a_broken_inbox_still_lands_the_durable_notice(db, tmp_path):
    """The channels are independent: a live sink that explodes must not take the
    durable publish down with it (the callback wrapper already swallows it —
    here we pin that the ORDER of the publishes makes that true)."""
    from lohra.agent.equip import bind_workflow_notifier

    class Boom:
        def enqueue_steer(self, text: str) -> None:
            raise RuntimeError("inbox exploded")

    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        bind_workflow_notifier(svc, lambda sid: Boom(), db=db)
        run_id = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()

    assert db.notices.pending_count("sess-1") == 1


# --- 3. fail-isolation: each channel survives the other's failure ----------


def test_a_failing_durable_store_never_skips_the_live_inbox(db, tmp_path):
    """Fail-isolation is INSIDE the callback, not borrowed from the service's
    wrapper: a durable publish that raises (sqlite busy, disk full) must not
    take the live inbox down with it — the first failure cannot skip the
    second channel."""
    import sqlite3

    from lohra.agent.equip import bind_workflow_notifier

    class ExplodingNotices:
        def publish(self, _owner: str, _text: str) -> bool:
            raise sqlite3.OperationalError("database is locked")

    class ExplodingDB:
        notices = ExplodingNotices()

    class Inbox:
        def __init__(self) -> None:
            self.texts: list[str] = []

        def enqueue_steer(self, text: str) -> None:
            self.texts.append(text)

    inbox = Inbox()
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        # The real SessionDB backs the SERVICE; only the notifier's durable
        # channel is the exploding stub — isolating the failure to one channel.
        bind_workflow_notifier(svc, lambda sid: inbox if sid == "sess-1" else None, db=ExplodingDB())
        run_id = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete" and "error" not in out
    finally:
        svc.shutdown()

    assert len(inbox.texts) == 1 and "demo" in inbox.texts[0]


def test_a_failing_inbox_is_isolated_from_the_durable_publish(db, tmp_path):
    """The mirror case, pinned at the callback level: the live inbox raising
    must neither skip nor unroll the durable publish — and the callback itself
    (not just the service's wrapper) must swallow it."""
    from lohra.agent.equip import bind_workflow_notifier

    class Boom:
        def enqueue_steer(self, text: str) -> None:
            raise RuntimeError("inbox exploded")

    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        bind_workflow_notifier(svc, lambda sid: Boom(), db=db)
        run_id = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        callback = svc._on_run_done
    finally:
        svc.shutdown()

    assert db.notices.pending_count("sess-1") == 1
    # Direct invocation: the isolation lives HERE, not in notify_done's wrap.
    callback(run_id, "complete", "workflow demo (ffffff) finished: complete, spent 8 tokens")
    assert db.notices.pending_count("sess-1") == 2
