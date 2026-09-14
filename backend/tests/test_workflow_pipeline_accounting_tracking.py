"""#111: engine-inventoried stages cannot disappear behind pipeline tracking.

Promoted from the directed spawn probe after both ordering defects reproduced.
"""

from threading import Event

import pytest

from lohra.orchestration.core import OrchestrationCore
from lohra.state import SessionDB
from lohra.workflow import library, quiescence, strategies
from lohra.workflow.engine import WorkflowEngine
from tests.pipeline_deadlines import control_pipeline_deadlines
from tests.test_workflow_pipeline_accounting import _service, _spec


@pytest.mark.parametrize("track_after_expiry", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("pause", [False, True])
def test_engine_inventory_closes_the_pipeline_append_gap(
    tmp_path, monkeypatch, track_after_expiry, nested, pause,
):
    accepted, allow_track, tracked, pipeline_return = (Event() for _ in range(4))
    running, release, tail, release_tail = (Event() for _ in range(4))
    sealing, allow_seal, sealed = (Event() for _ in range(3))
    expire = Event()
    control_pipeline_deadlines(monkeypatch, {"a": expire})
    spawn, core_spawn, seal = (
        WorkflowEngine.spawn_leaf_with_done, OrchestrationCore.spawn, WorkflowEngine._seal,
    )
    owner = {}

    def held_core_spawn(core, prompt, **kwargs):
        sub_id = core_spawn(core, prompt, **kwargs)
        if prompt == "second":
            owner["sub_id"] = sub_id
            accepted.set()
            if track_after_expiry:
                assert allow_track.wait(5)
        return sub_id

    def held_engine_spawn(engine, prompt, on_done, **kwargs):
        if prompt == "second":
            owner["engine"] = engine
        sub_id = spawn(engine, prompt, on_done, **kwargs)
        if prompt == "second":
            tracked.set()
            assert pipeline_return.wait(5)
        return sub_id

    def observed_seal(engine, result):
        if engine is owner.get("engine"):
            sealing.set()
            assert allow_seal.wait(5)
        seal(engine, result)
        if engine is owner.get("engine"):
            sealed.set()

    def respond(prompt):
        if prompt == "second":
            running.set()
            assert release.wait(5)
        elif prompt == "tail":
            tail.set()
            assert release_tail.wait(5)
        return "VALUE"

    child = _spec(stages=[{"prompt": "first"}, {"prompt": "second"}])
    # A normal sibling pipeline is not in the expired scope, nor is scalar b.
    child["nodes"].append({"id": "sibling", "type": "pipeline", "items": ["x"],
                           "stages": [{"prompt": "sibling"}], "depends_on": ["b"]})
    spec = child
    if nested:
        spec = {"meta": {"name": "root"}, "nodes": [
            {"id": "call", "type": "workflow", "ref": "child"},
        ]}
        monkeypatch.setattr(library, "get_template", lambda home, ref: child)
    monkeypatch.setattr(WorkflowEngine, "spawn_leaf_with_done", held_engine_spawn)
    monkeypatch.setattr(OrchestrationCore, "spawn", held_core_spawn)
    monkeypatch.setattr(WorkflowEngine, "_seal", observed_seal)
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.1)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.05)
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        run_id = svc.start(spec)["run_id"]
        state = svc._runs[run_id]
        assert accepted.wait(5) and running.wait(5)
        engine = owner["engine"]
        if not track_after_expiry:
            assert tracked.wait(5)
        if pause:
            engine.note_quota_exhausted("a", None)
        expire.set()  # after acceptance/start, and on the selected side of track
        if pause:
            assert sealing.wait(5)  # barrier expired; no downstream node runs
        else:
            assert tail.wait(5)  # the node loop already moved to b
        if track_after_expiry:
            assert owner["sub_id"] not in engine.spawned
        allow_track.set()
        assert tracked.wait(5)
        assert engine._leaf_node[owner["sub_id"]] == "a"
        release_tail.set()
        assert sealing.wait(5)
        allow_seal.set()
        assert sealed.wait(5)
        result = engine._result
        assert result.usage_uncertain_leaves == 1
        assert result.outputs["a"] == [None]
        assert set(engine._pending_account) == {owner["sub_id"]}
        assert engine._pending_account[owner["sub_id"]] is pause
        unknown = "a: leaf still running at seal; provider usage unknown"
        assert unknown in result.faults
        assert (unknown in result.pause_faults) is pause
        assert set(engine._expired_pipelines) == {"a"}
        assert result.status == ("paused" if pause else "degraded")
        # Let the existing stranded cleanup return, then let the provider finish.
        pipeline_return.set()
        release.set()
        state.future.result(timeout=5)
        assert state.engine._result.usage_uncertain_leaves == 1
        if nested:
            assert state.engine.spawned == ()
            assert state.engine._expired_pipelines == {}
    finally:
        expire.set()
        allow_track.set()
        pipeline_return.set()
        release.set()
        release_tail.set()
        allow_seal.set()
        svc.shutdown()
        db.close()


def test_terminal_callback_before_track_keeps_the_captured_pipeline_owner(tmp_path, monkeypatch):
    accepted, return_core, running, release, tail, release_tail = (Event() for _ in range(6))
    expire = Event()
    control_pipeline_deadlines(monkeypatch, {"a": expire})
    core_spawn = OrchestrationCore.spawn
    leaf = {}

    def held_spawn(core, prompt, **kwargs):
        sub_id = core_spawn(core, prompt, **kwargs)
        if prompt == "second":
            leaf["sub_id"] = sub_id
            accepted.set()
            assert return_core.wait(5)
        return sub_id

    def respond(prompt):
        if prompt == "second":
            running.set()
            assert release.wait(5)
        elif prompt == "tail":
            tail.set()
            assert release_tail.wait(5)
        return "VALUE"

    monkeypatch.setattr(OrchestrationCore, "spawn", held_spawn)
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.1)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.05)
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        spec = _spec(stages=[{"prompt": "first"}, {"prompt": "second"}])
        run_id = svc.start(spec)["run_id"]
        state = svc._runs[run_id]
        assert accepted.wait(5) and running.wait(5)
        expire.set()
        assert tail.wait(5)
        assert leaf["sub_id"] not in state.engine.spawned
        assert state.engine._current_node == "b" and not state.engine._sealed
        release.set()
        state.core._children[leaf["sub_id"]].future.result(timeout=5)
        costs = state.engine.node_costs()
        assert costs["a"].usage.input_tokens == 10
        assert "b" not in costs  # tail's provider is still blocked
        return_core.set()
        release_tail.set()
        state.future.result(timeout=5)
        assert state.engine._result.tokens_in == 15
        assert state.engine._result.outputs["a"] == [None]
    finally:
        expire.set()
        return_core.set()
        release.set()
        release_tail.set()
        svc.shutdown()
        db.close()
