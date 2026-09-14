"""Terminal numeric observation is synchronous and independent of on_done."""

from threading import Event, Thread, get_ident

import pytest

from lohra.agent.types import Usage
from lohra.workflow.budget import Budget
from lohra.workflow.usage_ledger import AcquisitionUsageLedger
from tests.test_workflow_pipeline import _core
from tests.test_workflow_post_drain_accounting import VECTOR
from tests.test_workflow_post_drain_accounting import five_meter_reply as five_meter_reply


@pytest.mark.parametrize("with_hook", [False, True])
def test_terminal_observer_precedes_future_and_hook_outside_core_lock(tmp_path, with_hook):
    from lohra.state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    core = _core(db, lambda prompt: "DONE")
    entered, release, called = Event(), Event(), []

    def observe(sub_id, usage):
        assert core._lock.acquire(timeout=1)
        core._lock.release()
        assert core.collect(sub_id)["status"] == "complete"
        called.append((sub_id, usage))
        entered.set()
        assert release.wait(5)

    core._terminal_observer = observe
    hooks = []
    try:
        sub_id = core.spawn("go", on_done=hooks.append if with_hook else None)
        assert entered.wait(5)
        future = core._children[sub_id].future
        assert not future.done() and hooks == []
        assert "error" in core.steer(sub_id, "too early")
        release.set()
        future.result(timeout=5)
        assert hooks == ([sub_id] if with_hook else [])
        core._fire_done(core._children[sub_id])
        assert called == [(sub_id, Usage(5, 3)), (sub_id, Usage(5, 3))]
        assert hooks == ([sub_id] if with_hook else [])
    finally:
        release.set()
        core.shutdown()
        db.close()


@pytest.mark.parametrize("positive_prefix", [False, True])
def test_external_queued_drop_after_drain_adds_no_usage_beyond_known_prefix(
    tmp_path, positive_prefix, five_meter_reply,
):
    from lohra.state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    calls = []
    core = _core(db, lambda prompt: calls.append(prompt) or "DONE", pool_width=1)
    book = AcquisitionUsageLedger(Budget())
    core._terminal_observer = book.observe
    busy, let_worker, external, let_external = (Event() for _ in range(4))
    original, caller = core._fire_done, []
    cancelled = None

    def held_delivery(sub):
        if caller and get_ident() == caller[0]:
            external.set()
            assert let_external.wait(5)
        original(sub)

    def occupy():
        busy.set()
        assert let_worker.wait(5)

    try:
        if positive_prefix:
            sub_id = core.spawn("first")
            core._children[sub_id].future.result(timeout=5)
            assert book._received[sub_id] == Usage(*VECTOR)
        core._pool.submit(occupy)
        assert busy.wait(5)
        if positive_prefix:
            assert "error" not in core.steer(sub_id, "queued second")
        else:
            sub_id = core.spawn("never ran")
        future = core._children[sub_id].future
        assert not future.running() and not future.done()
        core._fire_done = held_delivery

        def cancel():
            caller.append(get_ident())
            core.cancel(sub_id)

        cancelled = Thread(target=cancel)
        cancelled.start()
        assert external.wait(5) and future.cancelled()
        let_worker.set()
        core.shutdown()
        assert cancelled.is_alive()  # pool drain does not drain external callers
        frozen = book.finalize()
        assert frozen.usage == (Usage(*VECTOR) if positive_prefix else Usage())
        let_external.set()
        cancelled.join(5)
        assert not cancelled.is_alive()
        assert book.finalize() is frozen and not book.errors
        assert len(calls) == int(positive_prefix)
    finally:
        let_worker.set()
        let_external.set()
        if cancelled is not None:
            cancelled.join(5)
        core.shutdown()
        db.close()


def test_failed_observer_keeps_hook_delivery_and_drain_visible(tmp_path, caplog):
    from lohra.state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    core = _core(db, lambda prompt: "DONE")
    hooks = []

    def fail(*args):
        raise RuntimeError("observer failure")

    core._terminal_observer = fail
    try:
        sub_id = core.spawn("go", on_done=hooks.append)
        core.shutdown()
        assert core._children[sub_id].future.done()
        assert hooks == [sub_id]
        assert "observer failure" in caplog.text
    finally:
        core.shutdown()
        db.close()
