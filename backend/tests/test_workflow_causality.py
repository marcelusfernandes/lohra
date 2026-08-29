"""Behavioral discriminators for workflow leaf causal identity (OBS-02).

These tests deliberately assert the relation a future audit sink needs, not a
storage schema. The orchestration core treats the context as an opaque value;
the workflow engine owns its vocabulary.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.causality import CausalContext
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


class _Client(ModelClient):
    def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        prompt = " ".join(
            str(m.get("content", "")) for m in messages if isinstance(m, dict)
        )
        if "slow" in prompt:
            time.sleep(0.04)
        return _text_response(prompt.rsplit(" ", 1)[-1])

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


def _factory():
    return Agent(
        model="test-model",
        provider=get_provider_profile("anthropic"),
        client=_Client(),
    )


class _RecordingCore(OrchestrationCore):
    def __init__(self, db, child_factory=_factory):
        super().__init__(db, child_factory, max_concurrent=4)
        self.seen: dict[str, CausalContext] = {}
        self._seen_lock = threading.Lock()

    def spawn(self, prompt, **kwargs):
        context = kwargs.get("causal_context")
        sub_id = super().spawn(prompt, **kwargs)
        with self._seen_lock:
            self.seen[sub_id] = context
        return sub_id


def _pipeline(name: str):
    return validate_spec(
        {
            "meta": {"name": name},
            "nodes": [
                {
                    "id": "pipe",
                    "type": "pipeline",
                    "items": "${args.items}",
                    "stages": [
                        {"type": "agent", "prompt": "first ${item}"},
                        {"type": "agent", "prompt": "second ${stage.result}"},
                    ],
                }
            ],
        }
    )


def test_explicit_context_survives_out_of_order_and_concurrent_runs():
    """A callback order or a shared node id cannot change causal ownership."""
    db = SessionDB(":memory:")
    core = _RecordingCore(db)
    try:
        def run(run_id, items):
            return WorkflowEngine(
                core,
                budget=Budget(),
                run_id=run_id,
                segment_id=f"segment-{run_id}",
            ).run(_pipeline("same-spec"), {"items": items})

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(run, "run-a", ["slow-a", "fast-a"])
            b = pool.submit(run, "run-b", ["fast-b", "slow-b"])
            assert a.result().outputs["pipe"] == ["slow-a", "fast-a"]
            assert b.result().outputs["pipe"] == ["fast-b", "slow-b"]

        contexts = list(core.seen.values())
        assert len(contexts) == 8
        assert {c.run_id for c in contexts} == {"run-a", "run-b"}
        assert {c.segment_id for c in contexts} == {"segment-run-a", "segment-run-b"}
        assert {c.item_index for c in contexts} == {0, 1}
        assert {c.stage_index for c in contexts} == {0, 1}
        assert {c.role for c in contexts} == {"pipeline.stage"}
        assert len({(c.run_id, c.cell_id) for c in contexts}) == 8
        assert all(c.node_path == ("pipe",) for c in contexts)
    finally:
        core.shutdown()
        db.close()


def test_core_preserves_opaque_context_and_correction_turn_history():
    db = SessionDB(":memory:")
    core = OrchestrationCore(db, _factory)
    initial = CausalContext(
        run_id="run",
        segment_id="segment",
        node_path=("node",),
        cell_id="cell",
        role="agent",
    )
    correction = replace(initial, attempt=1, turn=1)
    try:
        sub_id = core.spawn("answer first", causal_context=initial)
        core.collect(sub_id, wait=True)
        assert core.causal_snapshot(sub_id)["causal_context"] is initial

        core.steer(sub_id, "answer corrected", causal_context=correction)
        core.collect(sub_id, wait=True)
        snapshot = core.causal_snapshot(sub_id)
        assert snapshot["causal_context"] is correction
        assert snapshot["causal_history"] == (initial, correction)
    finally:
        core.shutdown()
        db.close()


def test_cache_replay_creates_no_fictitious_subsession_and_resume_changes_segment():
    db = SessionDB(":memory:")
    core = _RecordingCore(db)
    cache = NodeCache(db, "run-cache")
    first = validate_spec(
        {
            "meta": {"name": "cached"},
            "nodes": [{"id": "leaf", "type": "agent", "prompt": "answer one"}],
        }
    )
    changed = validate_spec(
        {
            "meta": {"name": "cached"},
            "nodes": [{"id": "leaf", "type": "agent", "prompt": "answer two"}],
        }
    )
    try:
        WorkflowEngine(
            core, budget=Budget(), cache=cache, run_id="run-cache"
        ).run(first, {})
        first_context = next(iter(core.seen.values()))

        WorkflowEngine(
            core, budget=Budget(), cache=cache, run_id="run-cache"
        ).run(first, {})
        assert len(core.seen) == 1  # cache replay is not an execution

        WorkflowEngine(
            core, budget=Budget(), cache=cache, run_id="run-cache"
        ).run(changed, {})
        contexts = list(core.seen.values())
        assert len(contexts) == 2
        assert contexts[0] == first_context
        assert contexts[1].run_id == first_context.run_id
        assert contexts[1].segment_id != first_context.segment_id
        assert contexts[1].cell_id != first_context.cell_id
    finally:
        core.shutdown()
        db.close()


def test_nested_workflow_namespaces_the_node_path_without_changing_run_segment():
    db = SessionDB(":memory:")
    core = _RecordingCore(db)
    child = {
        "meta": {"name": "child"},
        "nodes": [{"id": "leaf", "type": "agent", "prompt": "answer nested"}],
    }
    parent = validate_spec(
        {
            "meta": {"name": "parent"},
            "nodes": [{"id": "nested", "type": "workflow", "ref": "child"}],
        }
    )
    try:
        WorkflowEngine(
            core,
            budget=Budget(),
            loader=lambda ref: child if ref == "child" else None,
            run_id="run-nested",
            segment_id="segment-nested",
        ).run(parent, {})
        context = next(iter(core.seen.values()))
        assert context.run_id == "run-nested"
        assert context.segment_id == "segment-nested"
        assert context.node_path == ("nested", "leaf")
    finally:
        core.shutdown()
        db.close()

class _SequenceClient(ModelClient):
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self._lock = threading.Lock()

    def create(self, **kwargs):
        with self._lock:
            return _text_response(next(self.outputs))

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


def _sequence_factory(outputs):
    client = _SequenceClient(outputs)

    def factory():
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=client,
        )

    return factory


def test_fresh_retry_increments_attempt_but_keeps_cell_identity():
    db = SessionDB(":memory:")
    core = _RecordingCore(db, _sequence_factory(["", "ok"]))
    spec = validate_spec(
        {
            "meta": {"name": "retry"},
            "nodes": [
                {"id": "leaf", "type": "agent", "prompt": "go", "retries": 1}
            ],
        }
    )
    try:
        result = WorkflowEngine(
            core, budget=Budget(), run_id="run-retry", segment_id="segment"
        ).run(spec, {})
        assert result.outputs["leaf"] == "ok"
        contexts = list(core.seen.values())
        assert [context.attempt for context in contexts] == [0, 1]
        assert len({context.cell_id for context in contexts}) == 1
        assert len(core.seen) == 2  # fresh retry means a new sub-session
    finally:
        core.shutdown()
        db.close()


def test_schema_correction_is_a_new_attempt_in_the_same_subsession():
    db = SessionDB(":memory:")
    core = _RecordingCore(
        db,
        _sequence_factory(
            ['{"wrong": true}', '{"still_wrong": true}', '{"value": "ok"}']
        ),
    )
    spec = validate_spec(
        {
            "meta": {"name": "correction"},
            "nodes": [
                {
                    "id": "leaf",
                    "type": "agent",
                    "prompt": "json",
                    "schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                }
            ],
        }
    )
    try:
        result = WorkflowEngine(
            core, budget=Budget(), run_id="run-correction", segment_id="segment"
        ).run(spec, {})
        assert result.outputs["leaf"] == {"value": "ok"}
        assert len(core.seen) == 1
        sub_id = next(iter(core.seen))
        core.collect(sub_id, wait=True)
        history = core.causal_snapshot(sub_id)["causal_history"]
        assert [context.attempt for context in history] == [0, 1, 2]
        assert [context.turn for context in history] == [0, 1, 2]
        assert len({context.cell_id for context in history}) == 1
    finally:
        core.shutdown()
        db.close()


class _RoleCore:
    """Deterministic core double: output is selected from the explicit role."""

    def __init__(self):
        self.contexts: dict[str, CausalContext] = {}

    def spawn(self, prompt, **kwargs):
        sub_id = f"sub-{len(self.contexts)}"
        self.contexts[sub_id] = kwargs["causal_context"]
        return sub_id

    def collect(self, sub_id, **kwargs):
        role = self.contexts[sub_id].role
        outputs = {
            "parallel.branch": "branch",
            "verify.skeptic": '{"refuted": false, "reason": "sound"}',
            "judge.attempt": "candidate",
            "judge.score": '{"score": 1, "reason": "good"}',
            "judge.synthesis": "winner",
            "loop.round": "",
            "gate.draft": "draft",
            "gate.review": '{"ok": true, "feedback": ""}',
            "completeness.review": '{"complete": true, "missing": []}',
        }
        return {
            "status": "complete",
            "output": outputs.get(role, "ok"),
            "causal_context": self.contexts[sub_id],
        }

    def steer(self, sub_id, text, **kwargs):
        self.contexts[sub_id] = kwargs["causal_context"]
        return {"ok": True, "queued": False}

    def cancel(self, sub_id):
        return {"ok": True}


def test_every_leaf_strategy_assigns_role_and_fanout_coordinates():
    core = _RoleCore()
    spec = validate_spec(
        {
            "meta": {"name": "causal-matrix"},
            "nodes": [
                {"id": "parallel", "type": "parallel", "branches": ["a", "b"]},
                {"id": "verify", "type": "verify", "finding": "claim", "skeptics": 2},
                {
                    "id": "judge",
                    "type": "judge_panel",
                    "attempts": ["a", "b"],
                    "judges": 1,
                    "synthesize": {"prompt": "rewrite ${winner}"},
                },
                {
                    "id": "loop",
                    "type": "loop_until_dry",
                    "body": {"prompt": "find"},
                    "max_rounds": 2,
                    "stop_after_k_empty": 1,
                },
                {
                    "id": "gate",
                    "type": "gate",
                    "body": {"prompt": "draft"},
                    "validator": "must pass",
                    "attempts": 1,
                },
                {
                    "id": "complete",
                    "type": "completeness_check",
                    "task": "task",
                    "results": "results",
                },
            ],
        }
    )
    result = WorkflowEngine(
        core, budget=Budget(), run_id="run", segment_id="segment"
    ).run(spec, {})

    assert not result.faults
    contexts = list(core.contexts.values())
    by_role = {}
    for context in contexts:
        by_role.setdefault(context.role, []).append(context)
    assert set(by_role) == {
        "parallel.branch",
        "verify.skeptic",
        "judge.attempt",
        "judge.score",
        "judge.synthesis",
        "loop.round",
        "gate.draft",
        "gate.review",
        "completeness.review",
    }
    assert {c.branch_path for c in by_role["parallel.branch"]} == {(0,), (1,)}
    assert {c.branch_path for c in by_role["verify.skeptic"]} == {(0,), (1,)}
    assert {c.branch_path for c in by_role["judge.attempt"]} == {(0,), (1,)}
    assert {c.branch_path for c in by_role["judge.score"]} == {(0, 0), (1, 0)}
    assert by_role["loop.round"][0].branch_path == (0,)
    assert by_role["gate.draft"][0].attempt == 0
    assert by_role["gate.review"][0].attempt == 0
    assert all(c.run_id == "run" and c.segment_id == "segment" for c in contexts)
    expected_node = {
        "parallel.branch": "parallel",
        "verify.skeptic": "verify",
        "judge.attempt": "judge",
        "judge.score": "judge",
        "judge.synthesis": "judge",
        "loop.round": "loop",
        "gate.draft": "gate",
        "gate.review": "gate",
        "completeness.review": "complete",
    }
    assert all(c.node_path == (expected_node[c.role],) for c in contexts)


def test_generic_leaf_fallback_uses_prompt_in_cell_identity():
    core = _RoleCore()
    engine = WorkflowEngine(core, budget=Budget(), run_id="run", segment_id="segment")

    engine.spawn_leaf("one")
    engine.spawn_leaf("two")

    contexts = list(core.contexts.values())
    assert contexts[0].role == contexts[1].role == "leaf"
    assert contexts[0].cell_id != contexts[1].cell_id


def test_callback_can_read_context_before_a_lateral_registry_is_populated():
    db = SessionDB(":memory:")
    core = OrchestrationCore(db, _factory)
    context = CausalContext(
        run_id="run",
        segment_id="segment",
        node_path=("node",),
        cell_id="cell",
        role="agent",
    )
    callback_done = threading.Event()
    observed = []
    lateral_registry = {}

    def on_done(sub_id):
        observed.append(
            (sub_id in lateral_registry, core.causal_snapshot(sub_id)["causal_context"])
        )
        callback_done.set()

    try:
        sub_id = core.spawn("answer", causal_context=context, on_done=on_done)
        assert callback_done.wait(1)
        lateral_registry[sub_id] = "node"
        assert observed == [(False, context)]
    finally:
        core.shutdown()
        db.close()


def test_causal_history_is_bounded_for_indefinitely_resumed_child():
    db = SessionDB(":memory:")
    core = OrchestrationCore(db, _factory)
    initial = CausalContext(
        run_id="run",
        segment_id="segment",
        node_path=("node",),
        cell_id="cell",
        role="agent",
    )
    try:
        sub_id = core.spawn("turn 0", causal_context=initial)
        core.collect(sub_id, wait=True)
        for turn in range(1, 70):
            context = replace(initial, attempt=turn, turn=turn)
            core.steer(sub_id, f"turn {turn}", causal_context=context)
            core.collect(sub_id, wait=True)
        snapshot = core.causal_snapshot(sub_id)
        assert len(snapshot["causal_history"]) == 64
        assert snapshot["causal_history"][0].turn == 6
        assert snapshot["causal_history"][-1].turn == 69
        assert snapshot["causal_history_dropped"] == 6
    finally:
        core.shutdown()
        db.close()


def test_collect_stays_json_serializable_for_the_agent_facing_tools():
    """``collect_session``/``steer_session`` splat ``collect()`` into
    ``tool_result(**out)``, which json.dumps the whole dict.  A workflow
    dataclass in that payload is a hard TypeError for the model-facing tool, and
    three always-null plumbing keys in every ordinary collect besides.
    """
    import json

    from lohra.orchestration.tools import OrchestrationTool

    db = SessionDB(":memory:")
    core = OrchestrationCore(db, _factory)
    context = CausalContext(
        run_id="run",
        segment_id="segment",
        node_path=("node",),
        cell_id="cell",
        role="agent",
    )
    try:
        sub_id = core.spawn("go", causal_context=context)
        out = core.collect(sub_id, wait=True)
        assert "causal_context" not in out
        assert "causal_history" not in out
        assert "causal_history_dropped" not in out
        json.dumps(out)  # the contract the two splats depend on

        tools = OrchestrationTool(core, "parent")
        payload = json.loads(tools.collect({"sub_id": sub_id, "wait": True}))
        assert "causal_context" not in json.dumps(payload)

        # The workflow still gets the identity — through a typed accessor.
        snapshot = core.causal_snapshot(sub_id)
        assert snapshot is not None
        assert snapshot["causal_context"] is context
        assert snapshot["causal_history"] == (context,)
        assert snapshot["causal_history_dropped"] == 0
        assert core.causal_snapshot("nope") is None
    finally:
        core.shutdown()
        db.close()
