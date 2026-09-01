"""``lohra workflow watch`` off the durable line alone — issue #47.

Before this file ``watch.py`` had zero tests, and the diagnostic behind #47
confirmed why that mattered: a run paused by ``token_budget_exhausted`` has no
auto-resume (``service.py``'s ``_on_paused`` arms a retry only for
``QUOTA_EXHAUSTED``), so ``status in TERMINAL`` — which never includes
``"paused"`` — spins ``watch_run`` forever. The fix has to discriminate by
``pause_reason``/``resume_at``, never by status alone: a run paused by quota
with a scheduled retry is exactly the case watch should keep following.

No real sleeps or lease acquisitions: rows are seeded straight onto the durable
line with ``RunStateStore.save`` (unfenced — nothing here ever calls
``acquire``), and ``sleep`` is an injected callback the test can use to mutate
the row between ticks.
"""

from __future__ import annotations

import pytest

from lohra.providers.errors import QUOTA_EXHAUSTED
from lohra.state import SessionDB
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.runstate_store import RunStateStore
from lohra.workflow.watch import watch_run


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _store(db, clock=None):
    return RunStateStore(db, holder="watch-test", clock=clock or (lambda: 1000.0))


def _seed(store, run_id, **overrides):
    fields = dict(
        run_id=run_id,
        name="a run",
        status="running",
        pause_reason=None,
        resume_at=None,
        attempts=0,
        token_budget=None,
    )
    fields.update(overrides)
    assert store.save(**fields)


class _Recorder:
    """``write``/``warn`` collected as plain lists — no real stream."""

    def __init__(self):
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def warn(self, line: str) -> None:
        self.warnings.append(line)


# --- classic terminal / missing / gone / stale -----------------------------


def test_watch_run_reports_terminal_status_and_stops(db):
    store = _store(db)
    _seed(store, "r1", status="complete")
    out = _Recorder()
    slept = []
    code = watch_run(store, db, "r1", write=out.write, warn=out.warn, sleep=slept.append)
    assert code == 0
    assert slept == []  # terminal on the very first read: never waited
    assert out.lines and "complete" in out.lines[-1]
    assert out.warnings == []


def test_watch_run_no_such_run(db):
    store = _store(db)
    out = _Recorder()
    code = watch_run(store, db, "ghost", write=out.write, warn=out.warn, sleep=lambda s: None)
    assert code == 1
    assert "no workflow run" in out.warnings[0]


def test_watch_run_vanishes_mid_loop(db):
    store = _store(db)
    _seed(store, "r1", status="running")
    # A live lease so the run reads as running-and-OWNED on the first tick —
    # otherwise ``is_stale`` fires immediately and the loop never reaches the
    # ``sleep`` this test uses to make the row disappear. Acquired straight on
    # the db (not ``store.acquire``) so no heartbeat timer is spun up.
    assert db.acquire_run_lease("r1", "watch-test", ttl_seconds=900.0, now=1000.0) is not None
    out = _Recorder()

    def sleep(_seconds: float) -> None:
        db._connection.execute("DELETE FROM workflow_run_state WHERE run_id=?", ("r1",))
        db._connection.commit()

    code = watch_run(store, db, "r1", write=out.write, warn=out.warn, sleep=sleep)
    assert code == 1
    assert "is gone" in out.warnings[-1]


def test_watch_run_stale_running_stops_with_resume_hint(db):
    store = _store(db)
    _seed(store, "r1", status="running")  # no lease ever acquired -> stale immediately
    out = _Recorder()
    code = watch_run(store, db, "r1", write=out.write, warn=out.warn, sleep=lambda s: None)
    assert code == 0
    assert any("resume_run_id" in w for w in out.warnings)


# --- the #47 discrimination: budget exits, quota keeps following ----------


def test_watch_run_paused_by_budget_exits_as_terminal(db):
    store = _store(db)
    _seed(
        store, "r1",
        status="paused", pause_reason=TOKEN_BUDGET_EXHAUSTED, resume_at=None, attempts=1,
    )
    out = _Recorder()
    slept = []
    code = watch_run(store, db, "r1", write=out.write, warn=out.warn, sleep=slept.append)
    assert code == 0
    assert slept == []  # never looped: this is exactly the bug's infinite spin, closed
    assert any(TOKEN_BUDGET_EXHAUSTED in w and "resume_run_id" in w for w in out.warnings)


def test_watch_run_paused_by_checkpoint_also_exits(db):
    """Budget is not the only no-auto-resume reason — any pause with no
    ``resume_at`` would spin the same way status-only logic used to."""
    store = _store(db)
    _seed(
        store, "r1",
        status="paused", pause_reason=CHECKPOINT, resume_at=None, attempts=0,
        checkpoint={"question": "proceed?"},
    )
    out = _Recorder()
    slept = []
    code = watch_run(store, db, "r1", write=out.write, warn=out.warn, sleep=slept.append)
    assert code == 0
    assert slept == []
    assert any("checkpoint" in w.lower() for w in out.warnings)


def test_watch_run_paused_by_quota_keeps_observing(db):
    clock = [1000.0]
    store = _store(db, clock=lambda: clock[0])
    _seed(
        store, "r1",
        status="paused", pause_reason=QUOTA_EXHAUSTED, resume_at=1090.0, attempts=1,
    )
    out = _Recorder()
    ticks = {"n": 0}

    def sleep(_seconds: float) -> None:
        ticks["n"] += 1
        # First tick: still waiting on the same scheduled retry. Second tick:
        # the auto-resume fired and the run finished — prove the loop kept
        # going past the pause instead of exiting on it.
        if ticks["n"] == 1:
            clock[0] = 1050.0
        else:
            store.save(
                run_id="r1", name="a run", status="complete",
                pause_reason=None, resume_at=None, attempts=1,
            )

    code = watch_run(store, db, "r1", write=out.write, warn=out.warn, sleep=sleep)
    assert code == 0
    assert ticks["n"] == 2  # it really did wait through the pause
    assert out.lines[-1].startswith("r1") or "complete" in out.lines[-1]
    eta_notes = [w for w in out.warnings if "auto-resume in" in w]
    assert len(eta_notes) == 1  # noted once, not re-printed every poll tick


def test_watch_run_paused_by_quota_but_attempts_spent_exits(db):
    """``resume_at is None`` — the scheduler's own allowlist gave up — is the
    same "nothing is coming" case as budget, even though the reason string
    still says quota."""
    store = _store(db)
    _seed(
        store, "r1",
        status="paused", pause_reason=QUOTA_EXHAUSTED, resume_at=None, attempts=5,
    )
    out = _Recorder()
    slept = []
    code = watch_run(store, db, "r1", write=out.write, warn=out.warn, sleep=slept.append)
    assert code == 0
    assert slept == []


# --- pause_reason surfaces on the row itself -------------------------------


def test_watch_run_line_shows_pause_reason(db):
    store = _store(db)
    _seed(store, "r1", status="paused", pause_reason=TOKEN_BUDGET_EXHAUSTED, resume_at=None)
    out = _Recorder()
    watch_run(store, db, "r1", write=out.write, warn=out.warn, sleep=lambda s: None)
    assert any(TOKEN_BUDGET_EXHAUSTED in line for line in out.lines)
