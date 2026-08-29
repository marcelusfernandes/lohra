"""Durable run state — resume/checkpoint across PROCESSES (WF-29).

Everything a run needs to be resumed used to live in ``RunState``, a process-local
dataclass: the spec, the args, the status, why it paused, what a checkpoint was
waiting for, how many auto-resume attempts it had spent. Only the token ledger and
the node cache reached SQLite. So a run that paused in one process was, in the
next one, a run that had never existed — ``run_workflow(resume_run_id=...)``
answered "a run this process never launched has nothing to replay" and
``workflow_status`` answered "no workflow run".

The honest simulation of a fresh process here is TWO ``WorkflowService`` instances
over the SAME file-backed ``SessionDB`` and the same home: the second one starts
with an empty ``_runs`` dict — exactly what a restart leaves — while SQLite and
the run's working root survive. (``:memory:`` would be a lie only in the other
direction; a file makes the sharing explicit.)

Four things are pinned:

- **the durable line**: spec/args/status/pause_reason/checkpoint/attempts/faults
  survive, so a resume in a fresh process replays completed cells and spawns
  NOTHING for them (the counter is the discriminator);
- **liveness across processes**: a lease in the ``compression_locks`` pattern
  (PK single-winner + TTL, clock injected) makes two processes resuming the same
  run a single winner, while a ``running`` row whose owner died is resumable —
  loudly, with a "recovered after process loss" fault, never blocked;
- **status/list** read the durable line instead of denying the run exists;
- **cold-start rearm**: a quota pause re-arms its auto-resume timer in the new
  process without resetting the backoff.

No real sleeps: timers are injected (``TimerFactory``) and every lease clock is a
list the test advances by hand.
"""

import threading
import time

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.providers.errors import QUOTA_EXHAUSTED
from lohra.state import SessionDB
from lohra.workflow import library
from lohra.workflow.autoresume import AutoResumeScheduler
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.lease_heartbeat import LeaseHeartbeat
from lohra.workflow.runstate_store import RECOVERED_FAULT, RunStateStore
from lohra.workflow import service as service_module
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient
from tests.test_workflow_quota import TimerFactory, _quota_responder

LEAF_COST = 8  # one fake turn: 5 input + 3 output tokens


@pytest.fixture
def db(tmp_path):
    """A FILE-backed store: the point of these tests is state that outlives the
    Python object holding it."""
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


def _counting(reply="R"):
    """(responder, counter): every leaf spawn bumps counter[0]."""
    counter = [0]

    def responder(_prompt):
        counter[0] += 1
        return reply

    return responder, counter


def _service(
    db, home, responder, *, timers=None, clock=None, lease_ttl=None, on_run_done=None,
    lease_timers=None,
):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    extra = {}
    if clock is not None:
        extra["clock"] = clock
    if lease_ttl is not None:
        extra["lease_ttl"] = lease_ttl
    if lease_timers is not None:
        extra["lease_timer_factory"] = lease_timers
    svc = WorkflowService(
        base_child_factory=factory, db=db, home=home, on_run_done=on_run_done, **extra
    )
    if timers is not None:
        svc.set_autoresume(
            AutoResumeScheduler(svc.resume, timer_factory=timers, clock=lambda: 1000.0)
        )
    return svc


# A cell that completes BEFORE the human gate, and nothing after it: the resume
# replays the completed cell and answers the gate, so a correct implementation
# spawns exactly zero leaves in the second process.
_GATED = {
    "meta": {"name": "gated", "version": 1},
    "nodes": [
        {"id": "draft", "type": "agent", "prompt": "Draft ${args.topic}"},
        {"id": "ask", "type": "checkpoint", "prompt": "Ship it?", "depends_on": ["draft"]},
    ],
}
# The gate first, then a node that needs the run's args: a resume that lost the
# args resolves ${args.source} to null instead of replaying the inputs.
_ARGS_GATED = {
    "meta": {"name": "argsdemo", "version": 1},
    "nodes": [
        {"id": "ask", "type": "checkpoint", "prompt": "proceed?"},
        {"id": "use", "type": "agent", "prompt": "use ${args.source} after ${ask}"},
    ],
}
_TWO_NODE = {
    "meta": {"name": "demo", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}


def _pause_at_gate(svc, spec=_GATED, args=None):
    run_id = svc.start(spec, args if args is not None else {"topic": "kites"})["run_id"]
    out = svc.status(run_id, wait=True, timeout=10)
    assert out["status"] == "paused" and out["reason"] == CHECKPOINT
    return run_id


# --- 1. the durable line: a checkpoint resumed in a fresh process -----------


def test_a_checkpoint_survives_the_process_that_asked_it(db, tmp_path):
    responder, calls = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = _pause_at_gate(svc)
        assert calls[0] == 1  # the draft leaf, once
    finally:
        svc.shutdown()  # the "kill": _runs is gone, SQLite and the work root are not

    responder2, calls2 = _counting()
    svc2 = _service(db, tmp_path, responder2)
    try:
        # The new process can SEE the pending question...
        pending = svc2.status(run_id)
        assert pending["status"] == "paused"
        assert pending["reason"] == CHECKPOINT
        assert pending["checkpoint"] == {"node_id": "ask", "prompt": "Ship it?"}
        # ...and answer it with nothing but the run id.
        out = svc2.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        assert "error" not in out, out
        rollup = svc2.status(run_id, wait=True, timeout=10)
        assert rollup["status"] == "complete"
        assert rollup["outputs"]["ask"] == "yes"
        # The discriminator: the completed cell REPLAYED — nothing re-spawned.
        assert calls2[0] == 0
    finally:
        svc2.shutdown()


def test_an_unanswered_checkpoint_is_still_refused_in_a_fresh_process(db, tmp_path):
    """The pending question used to vanish silently: a resume in a new process
    re-ran the spec and paused on the same gate, reading as "it did nothing"."""
    svc = _service(db, tmp_path, _counting()[0])
    try:
        run_id = _pause_at_gate(svc)
    finally:
        svc.shutdown()

    svc2 = _service(db, tmp_path, _counting()[0])
    try:
        out = svc2.start(resume_run_id=run_id)
        assert "waiting for an answer" in out["error"]
        assert "checkpoint_answers" in out["error"]
    finally:
        svc2.shutdown()


def test_a_run_nobody_ever_launched_still_says_to_pass_a_spec(db, tmp_path):
    svc = _service(db, tmp_path, _counting()[0])
    try:
        out = svc.start(resume_run_id="deadbeef")
        assert "no spec on file" in out["error"]
        # The claim that made this a process-local feature is gone.
        assert "never launched" not in out["error"]
    finally:
        svc.shutdown()


# --- 2. args and budget across the restart ---------------------------------


def test_a_resume_in_a_fresh_process_replays_the_runs_args(db, tmp_path):
    svc = _service(db, tmp_path, _counting()[0])
    try:
        run_id = _pause_at_gate(svc, _ARGS_GATED, {"source": "dump.txt"})
    finally:
        svc.shutdown()

    prompts = []

    def responder(prompt):
        prompts.append(prompt)
        return "R"

    svc2 = _service(db, tmp_path, responder)
    try:
        out = svc2.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        assert "error" not in out, out
        rollup = svc2.status(run_id, wait=True, timeout=10)
        assert rollup["status"] == "complete"
        assert any("dump.txt" in prompt for prompt in prompts)
        assert not any("upstream null" in fault for fault in rollup["faults"])
    finally:
        svc2.shutdown()


def test_raise_only_resume_still_holds_across_the_restart(db, tmp_path):
    svc = _service(db, tmp_path, _counting()[0], timers=TimerFactory())
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused" and out["reason"] == TOKEN_BUDGET_EXHAUSTED
    finally:
        svc.shutdown()

    responder, calls = _counting()
    svc2 = _service(db, tmp_path, responder)
    try:
        # A ceiling the run has already spent would re-pause on the first spawn.
        refused = svc2.start(resume_run_id=run_id, token_budget=LEAF_COST)
        assert "already spent" in refused["error"]
        assert calls[0] == 0
        out = svc2.start(resume_run_id=run_id, token_budget=500)
        assert "error" not in out, out
        rollup = svc2.status(run_id, wait=True, timeout=10)
        assert rollup["status"] == "complete"
        # The first cell replayed; only the one the budget stopped re-spawned.
        assert calls[0] == 1
        assert rollup["tokens_spent_total"] >= 2 * LEAF_COST
    finally:
        svc2.shutdown()


# --- 3. status / list read the durable line --------------------------------


def test_list_runs_shows_runs_this_process_never_launched(db, tmp_path):
    svc = _service(db, tmp_path, _counting()[0])
    try:
        run_id = _pause_at_gate(svc)
    finally:
        svc.shutdown()

    svc2 = _service(db, tmp_path, _counting()[0])
    try:
        rows = {row["run_id"]: row for row in svc2.list_runs()}
        assert run_id in rows
        assert rows[run_id]["name"] == "gated"
        assert rows[run_id]["status"] == "paused"
    finally:
        svc2.shutdown()


def test_a_live_run_is_listed_once_not_twice(db, tmp_path):
    """The durable line exists from the moment a run starts, so a merge that
    forgot to de-duplicate would list every live run twice."""
    svc = _service(db, tmp_path, _counting()[0])
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        ids = [row["run_id"] for row in svc.list_runs()]
        assert ids.count(run_id) == 1
    finally:
        svc.shutdown()


# --- 4. liveness across processes: lease + the orphaned `running` row -------


def test_the_lease_is_a_single_winner_with_a_ttl(db):
    """The compression-lock contract, for runs: the PK arbitrates, the TTL is
    what releases a lock whose holder died."""
    now = [0.0]
    mine = RunStateStore(db, holder="A", clock=lambda: now[0], ttl=100.0)
    theirs = RunStateStore(db, holder="B", clock=lambda: now[0], ttl=100.0)
    assert mine.acquire("r1") is True
    assert theirs.acquire("r1") is False
    assert theirs.lease_expiry("r1") == 100.0
    now[0] = 101.0  # the holder never came back
    assert theirs.acquire("r1") is True
    assert mine.acquire("r1") is False
    assert theirs.release("r1") is True
    assert mine.acquire("r1") is True


def test_two_processes_resuming_the_same_run_have_one_winner(db, tmp_path):
    """Two engines on one node cache and one working root is the corruption the
    lease exists to prevent. The loser is refused — and told when it may try."""
    release = threading.Event()
    now = [5000.0]
    # The owner's heartbeat timer is armed and NEVER fired: this is the process
    # that died, not the one that is merely slow inside a long node (that one
    # keeps its lease — see the heartbeat section below).
    owner = _service(
        db, tmp_path, lambda _p: (release.wait(5), "R")[1], clock=lambda: now[0], lease_ttl=100.0,
        lease_timers=TimerFactory(),
    )
    responder, calls = _counting()
    other = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        # The owner is really inside a leaf, so the lease is really held.
        run_id = owner.start(_TWO_NODE, {})["run_id"]
        refused = other.start(resume_run_id=run_id)
        assert "another process" in refused["error"]
        assert "~100s" in refused["error"]  # the number that decides what to do
        assert calls[0] == 0  # nothing spawned behind the refusal
        now[0] = 5101.0  # the owner never renewed: the lease lapsed
        out = other.start(resume_run_id=run_id)
        assert "error" not in out, out
    finally:
        release.set()
        other.shutdown()
        owner.shutdown()


def test_a_running_row_whose_process_died_is_resumable_with_a_fault(db, tmp_path):
    """A row saying `running` with nobody holding its lease means the owner was
    lost. Blocking the resume would strand the run forever; the honest move is
    to recover it and SAY so."""
    release = threading.Event()
    now = [7000.0]
    lost = _service(
        db, tmp_path, lambda _p: (release.wait(5), "R")[1], clock=lambda: now[0], lease_ttl=100.0
    )
    responder, calls = _counting()
    fresh = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = lost.start(_TWO_NODE, {})["run_id"]
        assert fresh.status(run_id)["status"] == "running"
        now[0] = 7101.0  # the owner never renewed: the lease lapsed
        stale = fresh.status(run_id)
        assert stale["stale"] is True
        assert "process" in stale["hint"]
        out = fresh.start(resume_run_id=run_id)
        assert "error" not in out, out
        rollup = fresh.status(run_id, wait=True, timeout=10)
        assert rollup["status"] == "complete"
        assert any(RECOVERED_FAULT in fault for fault in rollup["faults_total"])
        assert calls[0] == 2  # nothing had been cached: both cells really re-ran
    finally:
        release.set()
        fresh.shutdown()
        lost.shutdown()


def test_a_live_run_in_this_process_is_still_refused_by_name(db, tmp_path):
    """WF-17's in-process guard is untouched: its message is the specific one,
    not the lease's."""
    release = threading.Event()
    svc = _service(db, tmp_path, lambda _p: (release.wait(5), "R")[1])
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.start(resume_run_id=run_id)
        assert "has not finished" in out["error"]
    finally:
        release.set()
        svc.shutdown()


# --- 5. cold-start rearm ---------------------------------------------------


def test_a_quota_pause_rearms_its_timer_in_the_next_process(db, tmp_path):
    timers = TimerFactory()
    svc = _service(db, tmp_path, _quota_responder, timers=timers)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["reason"] == QUOTA_EXHAUSTED
        # Second stretch: fire the retry so the run's attempt count really is 1.
        timers.last.fire()
        assert svc.status(run_id, wait=True, timeout=10)["attempts"] == 1
    finally:
        svc.shutdown()  # the timer dies with the process

    timers2 = TimerFactory()
    responder, calls = _counting()
    svc2 = _service(db, tmp_path, responder, timers=timers2)
    try:
        assert timers2.timers == []  # nothing armed yet
        svc2.rearm_pending_resumes()
        assert len(timers2.timers) == 1
        # Backoff is NOT reset: the second attempt's own deadline is honoured
        # (60s * 2), not a fresh first-attempt minute.
        assert timers2.last.delay == 120.0
        timers2.last.fire()  # ...and firing it really resumes the run
        assert svc2.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert calls[0] == 2
    finally:
        svc2.shutdown()


def test_the_rearm_honours_what_is_LEFT_of_the_deadline(db, tmp_path):
    """The other half of "don't reset the backoff": a deadline that has not
    passed yet is picked up where it was, not restarted at the floor."""
    timers = TimerFactory()
    svc = _service(db, tmp_path, _quota_responder, timers=timers)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["reason"] == QUOTA_EXHAUSTED and out["resume_at"] == 1060.0
    finally:
        svc.shutdown()

    timers2 = TimerFactory()
    # 90 seconds still to run on the pause's own deadline (1060 - 970).
    svc2 = _service(db, tmp_path, _counting()[0], timers=timers2, clock=lambda: 970.0)
    try:
        svc2.rearm_pending_resumes()
        assert len(timers2.timers) == 1
        assert timers2.last.delay == 90.0  # not a fresh 60, not a doubled 120
    finally:
        svc2.shutdown()


def test_a_run_only_the_database_knows_cannot_be_paused_here(db, tmp_path):
    """`workflow_status` shows it, so "no workflow run" would be a lie — but
    pausing needs the live engine, which lives in the other process."""
    svc = _service(db, tmp_path, _counting()[0])
    try:
        run_id = _pause_at_gate(svc)
    finally:
        svc.shutdown()

    svc2 = _service(db, tmp_path, _counting()[0])
    try:
        out = svc2.pause(run_id)
        assert "not running in this process" in out["error"]
    finally:
        svc2.shutdown()


def test_cancelling_a_run_another_process_is_inside_is_refused(db, tmp_path):
    """The owner's own `_run` would write its result over our "cancelled" the
    moment it finished — a cancel that evaporates is worse than a refusal."""
    release = threading.Event()
    now = [8000.0]
    owner = _service(
        db, tmp_path, lambda _p: (release.wait(5), "R")[1], clock=lambda: now[0], lease_ttl=100.0
    )
    other = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = owner.start(_TWO_NODE, {})["run_id"]
        out = other.cancel(run_id)
        assert "another process" in out["error"]
        now[0] = 8101.0  # the owner is gone; the run is ours to stop
        assert other.cancel(run_id).get("ok") is True
        assert other.status(run_id)["status"] == "cancelled"
    finally:
        release.set()
        other.shutdown()
        owner.shutdown()


def test_only_quota_pauses_are_rearmed(db, tmp_path):
    """A checkpoint waits on a human and a budget waits on a bigger cap: a timer
    for either would burn the attempt cap re-pausing on the same node."""
    svc = _service(db, tmp_path, _counting()[0], timers=TimerFactory())
    try:
        _pause_at_gate(svc)
        svc.start(_TWO_NODE, {}, token_budget=5)
    finally:
        svc.shutdown()

    timers2 = TimerFactory()
    svc2 = _service(db, tmp_path, _counting()[0], timers=timers2)
    try:
        svc2.rearm_pending_resumes()
        assert timers2.timers == []
    finally:
        svc2.shutdown()


def test_a_rearmed_run_can_still_be_cancelled(db, tmp_path):
    """The rearm creates a timer for a run with no RunState. Cancelling it must
    still kill the retry — a resurrection is exactly what WF-19 forbids."""
    timers = TimerFactory()
    svc = _service(db, tmp_path, _quota_responder, timers=timers)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
    finally:
        svc.shutdown()

    timers2 = TimerFactory()
    responder, calls = _counting()
    svc2 = _service(db, tmp_path, responder, timers=timers2)
    try:
        svc2.rearm_pending_resumes()
        assert svc2.cancel(run_id).get("ok") is True
        timers2.last.fire()  # the timer object still exists; it must do nothing
        assert calls[0] == 0
        assert svc2.status(run_id)["status"] == "cancelled"
    finally:
        svc2.shutdown()


# --- 6. the process that finishes the run reports it honestly --------------


def test_the_finishing_process_records_the_outcome_and_notifies(db, tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(library, "record_outcome", lambda *a, **k: recorded.append(k))
    svc = _service(db, tmp_path, _counting()[0])
    try:
        run_id = _pause_at_gate(svc)
    finally:
        svc.shutdown()
    assert recorded == []  # a paused run teaches the library nothing

    announced = []
    svc2 = _service(
        db,
        tmp_path,
        _counting()[0],
        on_run_done=lambda rid, status, summary: announced.append((rid, status, summary)),
    )
    try:
        svc2.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        assert svc2.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert len(recorded) == 1
        # The whole run's cost, not this stretch's: the draft leaf was paid for
        # in the process that died.
        assert recorded[0]["tokens_total"] >= LEAF_COST
        assert announced and announced[0][0] == run_id and announced[0][1] == "complete"
    finally:
        svc2.shutdown()


def test_the_durable_line_is_a_round_trip(db):
    store = RunStateStore(db, holder="A", clock=lambda: 42.0)
    store.save(
        run_id="r1",
        name="demo",
        owner="s1",
        status="paused",
        pause_reason=CHECKPOINT,
        checkpoint={"node_id": "ask", "prompt": "?"},
        resume_at=99.0,
        attempts=2,
        prior_faults=["a: boom"],
        prior_degraded=True,
        tainted=True,
        spec=_GATED,
        args={"topic": "kites"},
        token_budget=500,
    )
    row = store.load("r1")
    assert row.status == "paused" and row.pause_reason == CHECKPOINT
    assert row.checkpoint == {"node_id": "ask", "prompt": "?"}
    assert row.resume_at == 99.0 and row.attempts == 2
    assert row.prior_faults == ["a: boom"] and row.prior_degraded is True
    assert row.tainted is True
    assert row.spec == _GATED and row.args == {"topic": "kites"}
    assert row.token_budget == 500 and row.updated_at == 42.0
    assert store.load("nope") is None


# --- 5. the heartbeat: a lease renewed by TIME, not only by finished cells --


def test_a_run_still_inside_one_long_node_keeps_its_lease(db, tmp_path):
    """A run writes a cache row only when a NODE finishes, so a single leaf that
    outlives the TTL used to lapse its own lease while it was still working —
    and the next process to look found an "ownerless" run and started a SECOND
    engine on its node cache and working root.

    The heartbeat is what tells "slow" from "dead": it renews on a timer, so a
    live run holds its lease however long one node takes."""
    release = threading.Event()
    now = [9000.0]
    beats = TimerFactory()
    owner = _service(
        db, tmp_path, lambda _p: (release.wait(5), "R")[1],
        clock=lambda: now[0], lease_ttl=100.0, lease_timers=beats,
    )
    responder, calls = _counting()
    other = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = owner.start(_TWO_NODE, {})["run_id"]
        assert beats.timers, "acquiring the lease arms the run's heartbeat"
        # Still inside the FIRST leaf: no cell completed, so the cache-write
        # heartbeat never fired. The timer one does.
        now[0] = 9060.0
        beats.last.fire()
        now[0] = 9101.0  # past the expiry the lease was ACQUIRED with
        refused = other.start(resume_run_id=run_id)
        assert "another process" in refused["error"]
        assert calls[0] == 0  # no second engine on this run's cache
    finally:
        release.set()
        other.shutdown()
        owner.shutdown()


def test_the_heartbeat_stops_when_the_run_lets_its_lease_go(db, tmp_path):
    """A timer that outlived its run would renew a lease nobody is using: the
    run would read as alive forever and never be resumable again."""
    beats = TimerFactory()
    svc = _service(db, tmp_path, _counting()[0], lease_timers=beats)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert svc._store.lease_expiry(run_id) is None
        assert beats.last.cancelled
    finally:
        svc.shutdown()


def test_a_launch_that_raises_after_taking_the_lease_gives_it_back(db, tmp_path, monkeypatch):
    """Between acquiring the lease and handing the run to the pool there is real
    work that can fail (mkdir on a full disk, an unreadable policy file). Raising
    there while still holding the lease locks every later resume out of the run
    until the TTL runs down — for a run that never started."""
    responder, calls = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = _pause_at_gate(svc)

        def boom(**_kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(service_module, "make_sandboxed_leaf_factory", boom)
        with pytest.raises(OSError):
            svc.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        assert svc._store.lease_expiry(run_id) is None
        monkeypatch.undo()
        # ...and the run is still resumable once the disk is fixed.
        out = svc.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        assert "error" not in out, out
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


def test_the_heartbeat_re_arms_until_the_lease_stops_being_ours():
    """The tick is a loop, not a one-shot: it re-arms for as long as the renewal
    says the lease is still this process's. When it is not — somebody took the
    run over after a real lapse — beating on would renew nothing and say so."""
    timers = TimerFactory()
    ours = [True]
    beat = LeaseHeartbeat(lambda _run_id: ours[0], interval=10.0, timer_factory=timers)
    beat.start("r1")
    assert len(timers.timers) == 1 and timers.last.delay == 10.0
    timers.last.fire()
    assert len(timers.timers) == 2  # renewed, and armed again
    ours[0] = False
    timers.last.fire()
    assert len(timers.timers) == 2  # no claim left to renew: it stops
    beat.shutdown()


def test_a_heartbeat_that_raises_keeps_beating_and_stops_on_demand():
    """One lost write is what the TTL is for: a heartbeat that gave up on a
    transient error would strand a live run at the next lapse. A stop is final
    though — including for a tick that was already in flight."""
    timers = TimerFactory()

    def boom(_run_id):
        raise RuntimeError("database is locked")

    beat = LeaseHeartbeat(boom, interval=10.0, timer_factory=timers)
    beat.start("r1")
    timers.last.fire()
    assert len(timers.timers) == 2  # still beating
    beat.stop("r1")
    assert timers.last.cancelled
    timers.last.fire()  # a tick that raced the stop renews nothing
    assert len(timers.timers) == 2


def test_a_stop_during_an_inflight_tick_wins_and_no_timer_survives():
    """WF-30 (achado pela própria Lohra em dogfood): _tick reivindica o timer,
    renova, e re-arma SEM checar se um stop() correu no meio — o timer re-armado
    sobrevive ao stop e renova uma lease que deveria morrer."""
    import threading

    from lohra.workflow.lease_heartbeat import LeaseHeartbeat

    entered, release = threading.Event(), threading.Event()

    def renew(run_id: str, **_kw) -> bool:
        entered.set()
        release.wait(2)
        return True

    timers: list = []

    class _FakeTimer:
        def __init__(self, interval, fn):
            self.fn = fn
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    hb = LeaseHeartbeat(renew, interval=1.0, timer_factory=lambda i, f: _FakeTimer(i, f))
    hb.start("r1")
    n_before = len(timers)
    tick = threading.Thread(target=timers[-1].fn)
    tick.start()
    assert entered.wait(2)
    hb.stop("r1")  # corre contra o tick em voo — o stop deve ser autoritativo
    release.set()
    tick.join(2)
    live = [t for t in timers[n_before:] if not t.cancelled]
    assert live == [], "um timer re-armado sobreviveu ao stop (heartbeat imortal)"


def test_shutdown_of_a_live_pipeline_returns_promptly(db, tmp_path):
    """``shutdown()`` waits on the run pool; the pipeline barrier must release.

    A run thread sitting in ``_done.wait(PIPELINE_TIMEOUT)`` would hold
    ``self._pool.shutdown(wait=True)`` for up to 30 minutes — and in that window
    a SIGKILL would strand the leases until their TTL. It does not: the leaves
    already inside a provider call are drained by ``core.shutdown(wait=True)``
    (interrupt is cooperative, by design), every guarded ``on_done`` then fires,
    ``run()``'s dispatch loop turns a dead pool into a settled item, and
    ``_advance`` settles the rest on ``engine.stopped``.
    """
    released = threading.Event()
    entered = threading.Event()

    def responder(_prompt):
        entered.set()
        released.wait(60)  # a leaf really inside a provider call
        return "R"

    spec = {
        "meta": {"name": "slow-pipeline", "version": 1},
        "nodes": [
            {
                "id": "fan",
                "type": "pipeline",
                "items": ["a", "b", "c", "d"],
                "stages": [{"prompt": "work on ${item}"}],
            }
        ],
    }
    svc = _service(db, tmp_path, responder)
    run_id = None
    try:
        run_id = svc.start(spec, {})["run_id"]
        assert entered.wait(10), "the pipeline never reached a leaf"
        # The provider calls in flight come back shortly after the cancel: what
        # is being measured is everything AFTER them, not their own latency.
        threading.Timer(0.5, released.set).start()
        start = time.monotonic()
        svc.shutdown()
        elapsed = time.monotonic() - start
    finally:
        released.set()
    assert elapsed < 30, f"shutdown blocked on the pipeline barrier ({elapsed:.1f}s)"
    # Leases are released, so a fresh process may resume immediately.
    assert RunStateStore(db).lease_expiry(run_id) is None
