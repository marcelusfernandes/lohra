"""#111 review repair: the pipeline owns a stage before causal construction.

The independent review exposed a delay in cache lookup, earlier than the prior
Core-acceptance probes. These author regressions use the same scheduling seam
with root/nested and callback-before/after-inventory contrasts.
"""

import json
from threading import Event

import pytest

from lohra.orchestration.core import OrchestrationCore
from lohra.state import SessionDB
from lohra.workflow import library, quiescence, strategies
from lohra.workflow.engine import WorkflowEngine
from tests.test_workflow_pipeline_accounting import _cells, _service, _spec


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("callback_before_track", [False, True])
def test_delayed_lookup_keeps_the_pipeline_owner(
    tmp_path, monkeypatch, nested, callback_before_track,
):
    lookup_entered, release_lookup, accepted, return_core = (Event() for _ in range(4))
    running, release, tail, release_tail, tracked = (Event() for _ in range(5))
    lookup, spawn, track = (
        WorkflowEngine.cache_lookup, OrchestrationCore.spawn, WorkflowEngine._track,
    )
    leaf = {}

    def held_lookup(engine, *args, **kwargs):
        if kwargs.get("cell_node_id") == "a#0#1":
            leaf["engine"] = engine
            lookup_entered.set()
            # _advance already passed its expiry guard, but has not constructed
            # the next stage's CausalContext or asked Core to accept any work.
            assert release_lookup.wait(5)
        return lookup(engine, *args, **kwargs)

    def held_spawn(core, prompt, **kwargs):
        sub_id = spawn(core, prompt, **kwargs)
        if prompt == "second":
            leaf["sub_id"] = sub_id
            accepted.set()
            # Select a genuine started worker before stranded cleanup can
            # validly cancel the queued entry. Core acceptance is unchanged.
            assert running.wait(5)
            if callback_before_track:
                assert return_core.wait(5)
        return sub_id

    def observed_track(engine, sub_id, **kwargs):
        track(engine, sub_id, **kwargs)
        if sub_id == leaf.get("sub_id"):
            tracked.set()

    def respond(prompt):
        if prompt == "second":
            running.set()
            assert release.wait(5)
        elif prompt == "tail":
            tail.set()
            assert release_tail.wait(5)
        return "VALUE"

    child = _spec(stages=[{"prompt": "first"}, {"prompt": "second"}])
    spec = child
    if nested:
        spec = {"meta": {"name": "root"}, "nodes": [
            {"id": "call", "type": "workflow", "ref": "child"},
        ]}
        monkeypatch.setattr(library, "get_template", lambda home, ref: child)
    monkeypatch.setattr(WorkflowEngine, "cache_lookup", held_lookup)
    monkeypatch.setattr(OrchestrationCore, "spawn", held_spawn)
    monkeypatch.setattr(WorkflowEngine, "_track", observed_track)
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.1)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.05)
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        state = svc._runs[svc.start(spec)["run_id"]]
        assert lookup_entered.wait(5) and tail.wait(5)
        engine = leaf["engine"]
        assert engine._current_node == "b" and not engine._sealed
        assert engine._result.outputs["a"] == [None]
        assert engine._expired_pipelines == {"a": False}
        release_lookup.set()
        assert accepted.wait(5) and running.wait(5)
        sub_id = leaf["sub_id"]
        if callback_before_track:
            assert sub_id not in engine.spawned
        else:
            assert tracked.wait(5)
        release.set()
        state.core._children[sub_id].future.result(timeout=5)
        costs = {key: cost.usage.input_tokens for key, cost in engine.node_costs().items()}
        causal = state.core.causal_snapshot(sub_id)["causal_context"]
        observed = {
            "causal_path": causal.node_path, "costs_before_tail": costs,
            "tracked_owner": engine._leaf_node.get(sub_id),
            "callback_before_track": callback_before_track, "nested": nested,
        }
        print(json.dumps(observed, sort_keys=True))
        assert causal.node_path == (("call", "a") if nested else ("a",)), observed
        assert costs == {"a": 10}, observed
        assert engine.spend_split().input_tokens == 10
        # The accepted first cell survives; this expired second cell cannot
        # publish output/cache even though its terminal accounting is accepted.
        assert len(_cells(db)) == 1 and _cells(db)[0].endswith("a#0#0")
        return_core.set()
        assert tracked.wait(5)
        assert engine._leaf_node[sub_id] == "a"
        assert {engine.core.causal_snapshot(s)["causal_context"].node_path
                for s in engine.spawned if engine._leaf_node[s] == "b"} == {
                    ("call", "b") if nested else ("b",),
                }
        release_tail.set()
        state.future.result(timeout=5)
        assert state.engine._result.tokens_in == 15
        assert state.engine._result.usage_uncertain_leaves == 0
        assert engine._result.outputs["a"] == [None]
        assert engine.node_costs()["a"].usage.input_tokens == 10
        assert engine.node_costs()["b"].usage.input_tokens == 5
    finally:
        release_lookup.set()
        return_core.set()
        release.set()
        release_tail.set()
        svc.shutdown()
        db.close()
