"""#111: terminal accounting is independent of an expired pipeline's output.

Real Service/Core/Agent and reopened SQLite; scripted replies cost 5/3. Events
prove the provider/callback, next-node and seal ordering. The callback gate and
partial-cache controls adapt the original issue #111 investigation probes.
"""

from copy import deepcopy
from threading import Event

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import quiescence, strategies
from lohra.workflow import library
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient


def _service(db, home, respond):
    def factory():
        return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
                     client=ScriptedClient(respond))

    return WorkflowService(base_child_factory=factory, db=db, home=home)


def _spec(*, kind="pipeline", tail=True, stages=None):
    first = (
        {"id": "a", "type": "agent", "prompt": "late", "timeout": 0.1}
        if kind == "scalar" else
        {"id": "a", "type": "pipeline", "items": ["one"],
         "stages": stages or [{"prompt": "late ${item}"}]}
    )
    nodes = [first]
    if tail:
        nodes.append({"id": "b", "type": "agent", "prompt": "tail", "depends_on": ["a"]})
    return {"meta": {"name": "pipeline-accounting"}, "nodes": nodes}


def _meter(db, run_id):
    row = db.run_spend_get(run_id)
    return row["tokens_in"], row["tokens_out"]


def _cells(db):
    return [r[0] for r in db._connection.execute(
        "SELECT node_id FROM workflow_node_cache ORDER BY node_id"
    )]


@pytest.fixture(autouse=True)
def short_caps(monkeypatch):
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.1)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.05)


@pytest.mark.parametrize("kind", ["scalar", "pipeline"])
def test_terminal_after_timeout_before_seal_is_durable_once(tmp_path, kind):
    late, release, tail, release_tail = (Event() for _ in range(4))
    calls = []

    def respond(prompt):
        calls.append(prompt)
        if prompt.startswith("late"):
            late.set()
            assert release.wait(5)
            return "LATE"
        assert prompt == "tail"
        tail.set()
        assert release_tail.wait(5)
        return "TAIL"

    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        run_id = svc.start(_spec(kind=kind))["run_id"]
        state = svc._runs[run_id]
        assert late.wait(5) and tail.wait(5)
        assert not state.engine._sealed
        sub_id = state.engine.spawned[0]
        release.set()
        state.core._children[sub_id].future.result(timeout=5)
        assert not state.engine._sealed
        assert state.core.collect(sub_id)["tokens_in"] == 5
        # Save the live reading before another observer can repair a missed
        # callback. Assert durable spend first to retain the original 10/6 oracle.
        live_usage = state.engine.spend_split()
        live_budget = state.engine.budget.tokens_spent
        assert _cells(db) == []
        release_tail.set()
        state.future.result(timeout=5)
        final = svc.status(run_id)
        assert final["outputs"]["a"] == (None if kind == "scalar" else [None])
        assert final["usage_uncertain_leaves"] == 0
        assert _meter(db, run_id) == (10, 6)
        assert live_usage.input_tokens == 5 and live_budget == 8
        assert state.engine.node_costs()["a"].usage.input_tokens == 5
        for _ in range(3):
            state.engine.account_leaf(sub_id)
        assert state.engine.budget.tokens_spent == 16
        assert _cells(db) == ["b"]
        svc.shutdown()
        db.close()
        db = SessionDB(tmp_path / "state.db")
        assert _meter(db, run_id) == (10, 6)
        svc = _service(db, tmp_path, respond)
        assert "error" not in svc.start(None, resume_run_id=run_id)
        svc._runs[run_id].future.result(timeout=5)
        assert svc.status(run_id)["outputs"]["a"] == ("LATE" if kind == "scalar" else ["LATE"])
        assert len(calls) == 3
        assert _meter(db, run_id) == (15, 9)
    finally:
        release.set()
        release_tail.set()
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("cross_barrier", [False, True])
def test_callback_accounting_does_not_accept_an_expired_stage(tmp_path, monkeypatch, cross_barrier):
    entered, release, tail, release_tail, callback_done = (Event() for _ in range(5))
    account, hook = WorkflowEngine.account_leaf, strategies._PipelineRun._hook
    calls = []

    def held_account(engine, sub_id, **kwargs):
        # Only the first observer (the callback) is held. Cleanup/settle may read
        # the same already-terminal leaf, exercising real accounting dedup.
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return account(engine, sub_id, **kwargs)

    def observed_hook(pipeline, cell):
        original = hook(pipeline, cell)

        def done(sub_id):
            try:
                original(sub_id)
            finally:
                callback_done.set()

        return done

    def respond(prompt):
        calls.append(prompt)
        if prompt == "tail":
            tail.set()
            assert release_tail.wait(5)
            return "TAIL"
        return "FAST"

    monkeypatch.setattr(WorkflowEngine, "account_leaf", held_account)
    monkeypatch.setattr(strategies._PipelineRun, "_hook", observed_hook)
    if not cross_barrier:
        monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 5)
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        run_id = svc.start(_spec())["run_id"]
        state = svc._runs[run_id]
        assert entered.wait(5)
        if cross_barrier:
            assert tail.wait(5)  # the expired pipeline already returned
            assert not state.engine._sealed
        release.set()
        assert callback_done.wait(5) and tail.wait(5)
        assert _cells(db) == ([] if cross_barrier else ["a#0#0"])
        release_tail.set()
        state.future.result(timeout=5)
        assert svc.status(run_id)["outputs"]["a"] == ([None] if cross_barrier else ["FAST"])
        assert _meter(db, run_id) == (10, 6)
        svc.shutdown()
        db.close()
        db = SessionDB(tmp_path / "state.db")
        svc = _service(db, tmp_path, respond)
        svc.start(None, resume_run_id=run_id)
        svc._runs[run_id].future.result(timeout=5)
        assert svc.status(run_id)["outputs"]["a"] == ["FAST"]
        assert len(calls) == (3 if cross_barrier else 2)
    finally:
        release.set()
        release_tail.set()
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("kind", ["scalar", "pipeline"])
def test_live_leaf_at_seal_is_uncertain_without_reopening_result(tmp_path, monkeypatch, kind):
    started, release, sealed = (Event() for _ in range(3))
    seal = WorkflowEngine._seal

    def observed_seal(engine, result):
        seal(engine, result)
        sealed.set()

    def respond(prompt):
        started.set()
        assert release.wait(5)
        return "LATE"

    monkeypatch.setattr(WorkflowEngine, "_seal", observed_seal)
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        run_id = svc.start(_spec(kind=kind, tail=False))["run_id"]
        state = svc._runs[run_id]
        assert started.wait(5) and sealed.wait(5)
        before = deepcopy(state.engine._result)
        assert before.usage_uncertain_leaves == 1
        assert before.tokens_in == before.tokens_out == 0
        assert any("still running" in f for f in before.faults)
        sub_id = state.engine.spawned[0]
        assert state.core.collect(sub_id)["status"] == "running"
        release.set()
        state.future.result(timeout=5)
        for _ in range(3):
            state.engine.account_leaf(sub_id)
        assert state.core.collect(sub_id)["tokens_in"] == 5
        assert state.engine._result == before
        assert _cells(db) == []
        # #112 remains open: terminal usage received after seal is not yet
        # reconciled numerically into durable spend by Service's final drain.
        assert _meter(db, run_id) == (0, 0)
        svc.shutdown()
        db.close()
        db = SessionDB(tmp_path / "state.db")
        assert _meter(db, run_id) == (0, 0)
    finally:
        release.set()
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("nested", [False, True])
def test_accepted_partial_cache_survives_later_expiry_and_reopen(tmp_path, monkeypatch, nested):
    slow, release, tail, release_tail, callback_done = (Event() for _ in range(5))
    hook = strategies._PipelineRun._hook
    calls = []

    def observed_hook(pipeline, cell):
        original = hook(pipeline, cell)

        def done(sub_id):
            try:
                original(sub_id)
            finally:
                if cell.stage == 1:
                    callback_done.set()

        return done

    def respond(prompt):
        calls.append(prompt)
        if prompt == "first one":
            return "FIRST"
        if prompt == "second FIRST":
            slow.set()
            assert release.wait(5)
            return "SECOND"
        assert prompt == "tail"
        tail.set()
        assert release_tail.wait(5)
        return "TAIL"

    child = _spec(stages=[{"prompt": "first ${item}"}, {"prompt": "second ${stage.result}"}])
    spec = child
    if nested:
        spec = {"meta": {"name": "root"}, "nodes": [
            {"id": "call", "type": "workflow", "ref": "child"},
        ]}
        monkeypatch.setattr(library, "get_template", lambda home, ref: child)
    monkeypatch.setattr(strategies._PipelineRun, "_hook", observed_hook)
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        run_id = svc.start(spec)["run_id"]
        state = svc._runs[run_id]
        assert slow.wait(5) and tail.wait(5)
        assert len(_cells(db)) == 1 and _cells(db)[0].endswith("a#0#0")
        release.set()
        assert callback_done.wait(5)
        assert not state.engine._sealed
        release_tail.set()
        state.future.result(timeout=5)
        outputs = svc.status(run_id)["outputs"]
        assert (outputs["call"] if nested else outputs)["a"] == [None]
        assert _meter(db, run_id) == (15, 9)
        costs = state.engine.node_costs()
        cost = costs["sub[call]:a" if nested else "a"]
        assert cost.usage.input_tokens == 10
        if nested:
            assert cost.node_path == ("call", "a")
        svc.shutdown()
        db.close()
        db = SessionDB(tmp_path / "state.db")
        svc = _service(db, tmp_path, respond)
        svc.start(None, resume_run_id=run_id)
        svc._runs[run_id].future.result(timeout=5)
        outputs = svc.status(run_id)["outputs"]
        assert (outputs["call"] if nested else outputs)["a"] == ["SECOND"]
        assert len(calls) == 4 and calls.count("first one") == 1
        assert _meter(db, run_id) == (20, 12)
    finally:
        release.set()
        release_tail.set()
        svc.shutdown()
        db.close()
