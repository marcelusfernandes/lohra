"""Narrow cancellation, callback-failure and admission controls for #111."""

from copy import deepcopy
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from lohra.state import SessionDB
from lohra.workflow import quiescence, strategies
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from tests.pipeline_deadlines import control_pipeline_deadlines
from tests.test_workflow_pipeline import _core
from tests.test_workflow_pipeline_accounting import _cells, _meter, _service, _spec


@pytest.mark.parametrize("seam", ["queued", "running", "terminal", "during_cancel"])
def test_cleanup_accounts_or_defers_every_observed_state_once(tmp_path, monkeypatch, seam):
    started, release = Event(), Event()

    def respond(prompt):
        started.set()
        assert release.wait(5)
        return "BILLED"

    db = SessionDB(tmp_path / "state.db")
    core = _core(db, respond, pool_width=1)
    engine = WorkflowEngine(core, budget=Budget(lifetime=3))
    engine._current_node = "p"
    pipeline = strategies._PipelineRun(engine, SimpleNamespace(id="p"), ["one"], [], {})
    pipeline._expired = True
    try:
        if seam == "queued":
            core.spawn("occupy worker")
            assert started.wait(5)
        # An owned hook deliberately does no accounting here: isolate cleanup's
        # terminal observation, rather than letting the callback mask a skip.
        sub_id = engine.spawn_leaf_with_done("pipeline leaf", lambda _: None)
        if seam != "queued":
            assert started.wait(5)
        if seam == "terminal":
            release.set()
            core._children[sub_id].future.result(timeout=5)
        if seam == "during_cancel":
            cancel = core.cancel

            def terminal_in_cancel(leaf):
                # Test-only event gate: cleanup saw running, terminal wins
                # before the actual Core cancellation attempts its transition.
                release.set()
                core._children[leaf].future.result(timeout=5)
                return cancel(leaf)

            monkeypatch.setattr(core, "cancel", terminal_in_cancel)
        attempts, report = pipeline._cancel_running([sub_id], quiesce=False)
        assert attempts == (0 if seam == "terminal" else 1)
        assert report.still_alive == report.settled == ()
        if seam == "running":
            assert sub_id in engine._pending_account
            assert sub_id not in engine._accounted
            assert engine.budget.lifetime_remaining == 2
            release.set()
            core._children[sub_id].future.result(timeout=5)
        for _ in range(3):
            engine.account_leaf(sub_id)
        pipeline._cancel_running([sub_id], quiesce=False)
        assert engine.budget.lifetime_remaining == (3 if seam == "queued" else 2)
        assert engine.budget.tokens_spent == (0 if seam == "queued" else 8)
        assert engine.spend_split().input_tokens == (0 if seam == "queued" else 5)
    finally:
        release.set()
        core.shutdown()
        db.close()


def test_expired_accounting_exception_cannot_change_a_sealed_result(tmp_path, monkeypatch):
    entered, release, sealed = Event(), Event(), Event()
    expire = Event()
    control_pipeline_deadlines(monkeypatch, {"a": expire})
    account, seal = WorkflowEngine.account_leaf, WorkflowEngine._seal
    hook, callback_threads = strategies._PipelineRun._hook, {}

    def observed_hook(pipeline, cell):
        original = hook(pipeline, cell)

        def done(sub_id):
            callback_threads[sub_id] = get_ident()
            original(sub_id)

        return done

    def fail_late(engine, sub_id, **kwargs):
        if not entered.is_set():
            assert callback_threads.get(sub_id) == get_ident()
            entered.set()
            assert release.wait(5)
            raise RuntimeError("late accounting failure")
        return account(engine, sub_id, **kwargs)

    def observed_seal(engine, result):
        seal(engine, result)
        sealed.set()

    monkeypatch.setattr(WorkflowEngine, "account_leaf", fail_late)
    monkeypatch.setattr(strategies._PipelineRun, "_hook", observed_hook)
    monkeypatch.setattr(WorkflowEngine, "_seal", observed_seal)
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.1)
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, lambda prompt: "TERMINAL")
    try:
        run_id = svc.start(_spec(tail=False))["run_id"]
        state = svc._runs[run_id]
        assert entered.wait(5)
        expire.set()  # the failing observer is already inside the callback
        assert sealed.wait(5)
        before = deepcopy(state.engine._result)
        assert before.tokens_in == 5  # cancellation's terminal reread settled it
        release.set()
        state.future.result(timeout=5)
        assert state.engine._result == before
        assert not any("late accounting failure" in f for f in before.faults)
        assert _cells(db) == []
        assert _meter(db, run_id) == (5, 3)
    finally:
        expire.set()
        release.set()
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("reply", ["LATE", "", "not-json"])
def test_discarded_stage_charges_live_budget_without_retry_or_successor(tmp_path, monkeypatch, reply):
    late, release, tail, release_tail = (Event() for _ in range(4))
    expire = Event()
    control_pipeline_deadlines(monkeypatch, {"a": expire})
    calls = []

    def respond(prompt):
        calls.append(prompt)
        if prompt.startswith("late"):
            late.set()
            assert release.wait(5)
            return reply
        assert prompt == "tail"  # neither pipeline successor nor later node
        tail.set()
        assert release_tail.wait(5)
        return "TAIL"

    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.1)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.05)
    spec = _spec(stages=[
        {"prompt": "late ${item}", "schema": {"type": "object"}},
        {"prompt": "pipeline successor"},
    ])
    spec["nodes"].append({"id": "c", "type": "agent", "prompt": "over budget", "depends_on": ["b"]})
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        run_id = svc.start(spec, token_budget=16)["run_id"]
        state = svc._runs[run_id]
        assert late.wait(5)
        expire.set()
        assert tail.wait(5)
        release.set()
        sub_id = state.engine.spawned[0]
        state.core._children[sub_id].future.result(timeout=5)
        for _ in range(3):
            state.engine.account_leaf(sub_id)
        assert state.engine.budget.tokens_spent == 8
        release_tail.set()
        state.future.result(timeout=5)
        result = state.engine._result
        assert result.status == "paused" and result.pause_reason == "token_budget_exhausted"
        assert result.outputs["a"] == [None]
        assert result.validation_retries == result.leaf_respawns == 0
        assert len(calls) == 2
        assert _cells(db) == ["b"]
        assert _meter(db, run_id) == (10, 6)
    finally:
        expire.set()
        release.set()
        release_tail.set()
        svc.shutdown()
        db.close()
