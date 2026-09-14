"""#112: final financial usage belongs to an acquisition, not its sealed result."""

from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from lohra.agent.types import Usage
from lohra.providers.transports.anthropic_messages import AnthropicMessagesTransport
from lohra.state import SessionDB
from lohra.workflow import library, quiescence
from lohra.workflow.engine import WorkflowEngine
from tests.pipeline_deadlines import control_pipeline_deadlines, control_scalar_deadlines
from tests.test_workflow_pipeline_accounting import _cells, _service, _spec


METERS = ("tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
VECTOR = (11, 13, 17, 19, 7)


@pytest.fixture
def five_meter_reply(monkeypatch):
    # Synthetic canonical Usage, not a provider-parser test. Agent/Core carry
    # the normalized response unchanged through their real execution paths.
    normalize = AnthropicMessagesTransport.normalize_response
    monkeypatch.setattr(AnthropicMessagesTransport, "normalize_response",
                        lambda transport, raw: replace(normalize(transport, raw), usage=Usage(*VECTOR)))


def meter(db, run_id):
    row = db.run_spend_get(run_id)
    return tuple(row[field] for field in METERS)


@pytest.mark.parametrize("kind", ["scalar", "pipeline"])
@pytest.mark.parametrize("nested", [False, True])
def test_late_receipt_is_financial_only_and_resume_adds_only_new_execution(
    tmp_path, monkeypatch, five_meter_reply, kind, nested,
):
    live, release, expire, sealed = (Event() for _ in range(4))
    deadlines = {"a": expire}
    control = control_scalar_deadlines if kind == "scalar" else control_pipeline_deadlines
    control(monkeypatch, deadlines)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.02)
    original_seal, histories, outcomes, calls = WorkflowEngine._seal, [], [], []

    def observed_seal(engine, result):
        original_seal(engine, result)
        histories.append((engine, deepcopy(result)))
        if engine._depth == 0:
            sealed.set()

    def respond(prompt):
        calls.append(prompt)
        live.set()
        assert release.wait(5)
        return "LATE"

    monkeypatch.setattr(WorkflowEngine, "_seal", observed_seal)
    monkeypatch.setattr(library, "record_outcome", lambda *args, **kwargs: outcomes.append(kwargs))
    child = _spec(kind=kind, tail=False)
    spec = child
    if nested:
        spec = {"meta": {"name": "root"}, "nodes": [
            {"id": "call", "type": "workflow", "ref": "same-child"},
        ]}
        monkeypatch.setattr(library, "get_template", lambda home, ref: child)
    db = SessionDB(tmp_path / "state.db")
    service = _service(db, tmp_path, respond)
    try:
        run_id = service.start(spec, token_budget=10)["run_id"]
        state = service._runs[run_id]
        assert live.wait(5)
        expire.set()
        assert sealed.wait(5)
        assert not state.future.done()
        assert histories[-1][1].usage_uncertain_leaves == 1
        assert histories[-1][1].tokens_in == 0
        assert _cells(db) == []
        release.set()
        state.future.result(timeout=5)
        for engine, before in histories:
            assert engine._result == before
        assert _cells(db) == []
        assert meter(db, run_id) == VECTOR
        assert state.engine.budget.tokens_spent == 24  # reasoning is report-only
        assert state.engine.budget.est_leaf_cost == 24  # one measurement, not each observation
        assert outcomes[-1]["tokens_total"] == 24
        assert outcomes[-1]["budget_overrun"] == 14
        status = service.status(run_id)
        assert status["tokens_spent_total"] == 24
        assert status["token_budget"]["overrun_max"] == 14
        assert status["tokens_spent_split"] == {"cache_read": 17, "cache_write": 19, "reasoning": 7}
        assert status["financial"] == {
            "committed": True, "capture_complete": True, "node_costs_cutoff": "engine_seal",
        }
        service.shutdown()
        db.close()
        db = SessionDB(tmp_path / "state.db")
        assert meter(db, run_id) == VECTOR
        deadlines.clear()
        service = _service(db, tmp_path, respond)
        assert "error" not in service.start(None, resume_run_id=run_id, token_budget=100)
        service._runs[run_id].future.result(timeout=5)
        assert len(calls) == 2
        assert meter(db, run_id) == tuple(2 * v for v in VECTOR)
        assert outcomes[-1]["tokens_total"] == 48
        assert outcomes[-1]["budget_overrun"] == 14
    finally:
        expire.set()
        release.set()
        service.shutdown()
        db.close()


def test_nested_siblings_and_evicted_receipts_keep_disjoint_usage(
    tmp_path, monkeypatch, five_meter_reply,
):
    from lohra.orchestration.core import OrchestrationCore
    events = {name: Event() for name in ("one", "two", "tail")}
    releases = {name: Event() for name in events}
    first_expire, second_expire = Event(), Event()
    deadlines = {"a": first_expire}
    control_pipeline_deadlines(monkeypatch, deadlines)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.02)
    init, seal = OrchestrationCore.__init__, WorkflowEngine._seal
    children, histories, calls = [], [], []

    def small_registry(core, *args, **kwargs):
        init(core, *args, **{**kwargs, "max_children": 1})

    def observed_seal(engine, result):
        seal(engine, result)
        histories.append((engine, deepcopy(result)))
        if engine._depth:
            children.append(engine)
            if len(children) == 1:
                deadlines["a"] = second_expire

    def respond(prompt):
        calls.append(prompt)
        name = prompt.removeprefix("late ")
        if name in events:
            events[name].set()
            assert releases[name].wait(5)
        return prompt.upper()

    child = _spec(tail=False, stages=[{"prompt": "late ${args.tag}"}])
    monkeypatch.setattr(library, "get_template", lambda home, ref: child)
    monkeypatch.setattr(OrchestrationCore, "__init__", small_registry)
    monkeypatch.setattr(WorkflowEngine, "_seal", observed_seal)
    spec = {"meta": {"name": "siblings"}, "nodes": [
        {"id": "one", "type": "workflow", "ref": "child", "args": {"tag": "one"}},
        {"id": "two", "type": "workflow", "ref": "child", "args": {"tag": "two"},
         "depends_on": ["one"]},
        {"id": "tail", "type": "agent", "prompt": "tail", "depends_on": ["two"]},
        {"id": "last", "type": "agent", "prompt": "last", "depends_on": ["tail"]},
    ]}
    db = SessionDB(tmp_path / "state.db")
    service = _service(db, tmp_path, respond)
    try:
        run_id = service.start(spec)["run_id"]
        state = service._runs[run_id]
        assert events["one"].wait(5)
        first_expire.set()
        assert events["two"].wait(5)
        first_id = children[0].spawned[0]
        releases["one"].set()
        state.core._children[first_id].future.result(timeout=5)
        second_expire.set()
        assert events["tail"].wait(5)
        assert first_id not in state.core._children
        second_id = children[1].spawned[0]
        releases["two"].set()
        state.core._children[second_id].future.result(timeout=5)
        releases["tail"].set()
        state.future.result(timeout=5)
        assert second_id not in state.core._children
        assert len(state.engine.spawned) == 2  # root-only inventory misses both children
        assert meter(db, run_id) == tuple(4 * value for value in VECTOR)
        assert state.result.tokens_in == 22  # only the two accepted root leaves
        for engine, before in histories:
            assert engine._result == before
        assert _cells(db) == ["last", "tail"]
        deadlines.clear()
        service.start(None, resume_run_id=run_id)
        service._runs[run_id].future.result(timeout=5)
        assert len(calls) == 6  # cached root leaves are not newly measured or billed
        assert meter(db, run_id) == tuple(6 * value for value in VECTOR)
    finally:
        first_expire.set()
        second_expire.set()
        for release in releases.values():
            release.set()
        service.shutdown()
        db.close()


def test_core_only_receipt_is_financial_without_rewriting_inventory_at_seal(
    tmp_path, monkeypatch, five_meter_reply,
):
    live, release, expire, sealed, track_entered, finish_track = (Event() for _ in range(6))
    control_pipeline_deadlines(monkeypatch, {"a": expire})
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.02)
    track, seal = WorkflowEngine._track, WorkflowEngine._seal
    untracked = []

    def held_track(engine, sub_id, **kwargs):
        context = engine.core.causal_snapshot(sub_id)["causal_context"]
        if context.stage_index == 1:
            untracked.append(sub_id)
            track_entered.set()
            assert finish_track.wait(5)
        return track(engine, sub_id, **kwargs)

    def observed_seal(engine, result):
        seal(engine, result)
        sealed.set()

    def respond(prompt):
        if prompt.startswith("late"):
            live.set()
            assert release.wait(5)
        return "FIRST"

    monkeypatch.setattr(WorkflowEngine, "_track", held_track)
    monkeypatch.setattr(WorkflowEngine, "_seal", observed_seal)
    db = SessionDB(tmp_path / "state.db")
    service = _service(db, tmp_path, respond)
    try:
        run_id = service.start(_spec(tail=False, stages=[
            {"prompt": "first"}, {"prompt": "late ${stage.result}"},
        ]))["run_id"]
        state = service._runs[run_id]
        assert live.wait(5) and track_entered.wait(5)
        assert untracked[0] not in state.engine.spawned
        expire.set()
        assert sealed.wait(5)
        before = deepcopy(state.engine._result)
        assert before.tokens_in == 11 and before.usage_uncertain_leaves == 0
        release.set()
        state.core._children[untracked[0]].future.result(timeout=5)
        finish_track.set()
        state.future.result(timeout=5)
        assert state.result == before
        assert _cells(db) == ["a#0#0"]
        assert meter(db, run_id) == tuple(2 * value for value in VECTOR)
    finally:
        expire.set()
        release.set()
        finish_track.set()
        service.shutdown()
        db.close()
