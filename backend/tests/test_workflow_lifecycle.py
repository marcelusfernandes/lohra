"""Run lifecycle: timeout, cancel and empty output (CC-parity M4, fatia A).

Three defects, one theme — a run that keeps burning workers or reports a clean
verdict over work that never happened:

- WF-2  a leaf that blows the collect timeout stays alive as a zombie, holding an
        orch worker forever. The engine must CANCEL it and say so in the rollup;
        `timeout`/`retries` become per-node fields (part of the cell identity).
- WF-7  a "complete" leaf that answered nothing ("" / whitespace) reads as a real
        answer. It is a RECOVERABLE failure: bounded fresh re-spawn, then null +
        fault — and it must never land in the resume cache.
- WF-19 cancel doesn't cancel: the engine keeps scheduling nodes, `shutdown()`
        blocks the caller, and a queued steer relaunches a cancelled turn.

Timings are injected and tiny (no real sleeps); every gate is released in a
`finally` so a failing assertion can never hang the suite.
"""

import threading
import time

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import quiescence, strategies
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import ValidationError, validate_spec
from lohra.workflow.service import WorkflowService
from tests.test_loop import FakeClient
from tests.test_workflow_pipeline import ScriptedClient


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def fast_quiescence(monkeypatch):
    """This file's leaves block INSIDE the client, where a cooperative cancel
    can never reach them — so every cancel here would spend the whole default
    quiescence cap (issue #42-B) before the run moves on. The wait itself is
    proved in ``test_workflow_quiescence.py``; here it only has to stay out of
    the way of the timings these tests actually measure."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)


def _core(db, responder, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _engine(core, **kwargs):
    return WorkflowEngine(core, budget=Budget(), **kwargs)


def _agent_spec(**fields):
    node = {"id": "a", "type": "agent", "prompt": "go"}
    node.update(fields)
    return validate_spec({"meta": {"name": "life"}, "nodes": [node]})


def _spy_cancel(core):
    """Record every core.cancel(sub_id) while keeping the real behaviour."""
    seen: list[str] = []
    original = core.cancel

    def spy(sub_id):
        seen.append(sub_id)
        return original(sub_id)

    core.cancel = spy
    return seen


def _faults(result):
    return "\n".join(result.faults)


def _cached_rows(db, run_id):
    """How many cells this run actually cached (a completion leaves a row)."""
    return db._connection.execute(
        "SELECT count(*) FROM workflow_node_cache WHERE run_id = ?", (run_id,)
    ).fetchone()[0]


# --- WF-2: a leaf that blows its timeout is cancelled, not left zombie --------


def test_leaf_timeout_cancels_the_zombie_and_records_a_fault(db):
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    cancelled = _spy_cancel(core)
    try:
        result = _engine(core).run(_agent_spec(timeout=0.2), {})
        assert result.outputs["a"] is None
        assert len(cancelled) == 1  # the zombie was interrupted, not abandoned
        assert "leaf timeout after" in _faults(result)
        assert "cancelled" in _faults(result)
        assert "leaf running" not in _faults(result)  # the timeout, not a bare status
    finally:
        gate.set()
        core.shutdown()


def test_pipeline_expiry_cancels_the_inflight_leaves(db, monkeypatch):
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.3)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    cancelled = _spy_cancel(core)
    spec = validate_spec(
        {
            "meta": {"name": "p"},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": "${args.items}",
                    "stages": [{"prompt": "do ${item}"}],
                }
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {"items": ["x"]})
        assert result.outputs["p"] == [None]
        assert len(cancelled) == 1  # the stranded leaf was cancelled at the barrier
        assert "timed out" in _faults(result)
    finally:
        gate.set()
        core.shutdown()


# --- WF-2: per-node timeout / retries ----------------------------------------


def test_agent_node_accepts_timeout_and_retries():
    spec = _agent_spec(timeout=5, retries=2)
    assert not isinstance(spec, ValidationError)
    assert spec.nodes[0].fields["timeout"] == 5


@pytest.mark.parametrize("value", [0, -1, "fast", True, None])
def test_invalid_timeout_is_a_didactic_issue(value):
    spec = _agent_spec(timeout=value)
    assert isinstance(spec, ValidationError)
    assert "timeout" in spec.message
    assert "e.g." in spec.message  # didactic: it SHOWS the fix


@pytest.mark.parametrize("value", [-1, 4, 1.5, "two", True])
def test_invalid_retries_is_a_didactic_issue(value):
    spec = _agent_spec(retries=value)
    assert isinstance(spec, ValidationError)
    assert "retries" in spec.message
    assert "e.g." in spec.message


def test_timeout_change_invalidates_the_cached_cell(db):
    prompts: list[str] = []
    core = _core(db, lambda prompt: (prompts.append(prompt), "R")[1])
    try:
        cache = NodeCache(db, "run-1")
        assert _engine(core, cache=cache).run(_agent_spec(timeout=5), {}).outputs["a"] == "R"
        assert len(prompts) == 1
        _engine(core, cache=cache).run(_agent_spec(timeout=5), {})  # same cell: replayed
        assert len(prompts) == 1
        _engine(core, cache=cache).run(_agent_spec(timeout=7), {})  # new identity: re-spawn
        assert len(prompts) == 2
        _engine(core, cache=cache).run(_agent_spec(timeout=7, retries=2), {})
        assert len(prompts) == 3
    finally:
        core.shutdown()


def test_per_node_timeout_beats_the_global_default(db, monkeypatch):
    # A huge global default must not hide a node that asked for a short leash.
    monkeypatch.setattr(strategies, "LEAF_TIMEOUT", 600.0)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    try:
        started = time.monotonic()
        result = _engine(core).run(_agent_spec(timeout=0.2), {})
        assert time.monotonic() - started < 5  # honoured the node, not the default
        assert result.outputs["a"] is None
    finally:
        gate.set()
        core.shutdown()


# --- WF-7: an empty answer is a recoverable failure --------------------------


def test_empty_output_retries_once_then_nulls_with_a_fault(db):
    prompts: list[str] = []
    core = _core(db, lambda prompt: (prompts.append(prompt), "")[1])
    try:
        result = _engine(core).run(_agent_spec(), {})
        assert result.outputs["a"] is None  # never reported as a real answer
        assert len(prompts) == 2  # one retry by default (fresh re-spawn)
        assert "empty output after retry" in _faults(result)
        assert result.status == "failed"  # the only node nulled
    finally:
        core.shutdown()


def test_empty_output_recovers_on_the_retry(db):
    replies = iter(["   ", "REAL"])
    prompts: list[str] = []
    core = _core(db, lambda prompt: (prompts.append(prompt), next(replies, ""))[1])
    try:
        result = _engine(core).run(_agent_spec(), {})
        assert result.outputs["a"] == "REAL"
        assert len(prompts) == 2
        assert result.faults == []  # a recovered retry is not a fault
    finally:
        core.shutdown()


def test_retries_field_bounds_the_empty_retry(db):
    prompts: list[str] = []
    core = _core(db, lambda prompt: (prompts.append(prompt), "")[1])
    try:
        _engine(core).run(_agent_spec(retries=3), {})
        assert len(prompts) == 4  # 1 attempt + 3 retries
        prompts.clear()
        _engine(core).run(_agent_spec(retries=0), {})
        assert len(prompts) == 1  # opted out of retrying
    finally:
        core.shutdown()


def test_empty_output_is_never_cached_as_a_completion(db):
    replies = iter(["", "", "REAL"])
    prompts: list[str] = []
    core = _core(db, lambda prompt: (prompts.append(prompt), next(replies, "REAL"))[1])
    try:
        cache = NodeCache(db, "run-e")
        _engine(core, cache=cache).run(_agent_spec(retries=0), {})
        assert len(prompts) == 1
        assert _cached_rows(db, "run-e") == 0  # no row at all — "" is not a completion
        _engine(core, cache=cache).run(_agent_spec(retries=0), {})
        assert len(prompts) == 2  # a resume re-spawns it instead of replaying ""
        _engine(core, cache=cache).run(_agent_spec(retries=0), {})
        assert _cached_rows(db, "run-e") == 1  # control: a REAL answer does cache
    finally:
        core.shutdown()


def test_dead_leaf_is_not_retried_as_an_empty_output(db):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([RuntimeError("leaf boom")] * 8),
        )

    core = OrchestrationCore(db, factory)
    try:
        result = _engine(core).run(_agent_spec(), {})
        assert result.outputs["a"] is None
        assert "leaf error" in _faults(result)
        assert "empty output" not in _faults(result)  # a crash is not an empty answer
    finally:
        core.shutdown()


def test_pipeline_empty_stage_retries_then_drops_the_item(db):
    prompts: list[str] = []
    core = _core(db, lambda prompt: (prompts.append(prompt), "")[1])
    spec = validate_spec(
        {
            "meta": {"name": "p"},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": "${args.items}",
                    "stages": [{"prompt": "do ${item}"}],
                }
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {"items": ["x"]})
        assert result.outputs["p"] == [None]
        assert len(prompts) > 1  # re-spawned before dropping
        assert "empty output" in _faults(result)
    finally:
        core.shutdown()


# --- WF-19: cancel actually cancels ------------------------------------------


def test_engine_cancel_flag_stops_scheduling_further_nodes(db):
    engine_box: list = []
    prompts: list[str] = []

    def responder(prompt):
        prompts.append(prompt)
        engine_box[0].request_cancel()  # cancel arrives during the first node
        return "R"

    core = _core(db, responder)
    spec = validate_spec(
        {
            "meta": {"name": "chain"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "one"},
                {"id": "b", "type": "agent", "prompt": "two"},
                {"id": "c", "type": "agent", "prompt": "three"},
            ],
        }
    )
    try:
        engine = _engine(core)
        engine_box.append(engine)
        result = engine.run(spec, {})
        assert len(prompts) == 1  # b and c were never spawned
        assert result.status == "cancelled"
        assert "b" not in result.outputs and "c" not in result.outputs
        assert result.null_count == 0  # a cancel is not a cascade of nulls
    finally:
        core.shutdown()


def test_pipeline_settles_and_stops_spawning_on_engine_cancel(db, monkeypatch):
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 5.0)
    engine_box: list = []
    prompts: list[str] = []

    def responder(prompt):
        prompts.append(prompt)
        engine_box[0].request_cancel()
        return "R"

    core = _core(db, responder, pool_width=1)
    spec = validate_spec(
        {
            "meta": {"name": "p"},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": "${args.items}",
                    "stages": [
                        {"prompt": "s1 ${item}"},
                        {"prompt": "s2 ${stage.result}"},
                    ],
                }
            ],
        }
    )
    try:
        engine = _engine(core)
        engine_box.append(engine)
        started = time.monotonic()
        engine.run(spec, {"items": ["x", "y", "z"]})
        assert time.monotonic() - started < 4  # the barrier released, no 5s hang
        assert len(prompts) < 6  # stopped chaining stages instead of finishing all
    finally:
        core.shutdown()


def test_shutdown_can_return_without_waiting_for_a_blocked_leaf(db):
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    try:
        core.spawn("long task")
        started = time.monotonic()
        core.shutdown(wait=False)  # cancel path: must not block the tool thread
        assert time.monotonic() - started < 1.0
    finally:
        gate.set()
        core.shutdown()  # process teardown still drains (default wait=True)


def test_cancelled_sub_does_not_relaunch_on_a_leftover_steer(db):
    gate = threading.Event()
    started = threading.Event()
    prompts: list[str] = []

    def responder(prompt):
        prompts.append(prompt)
        started.set()
        gate.wait(5)
        return "done"

    core = _core(db, responder)
    try:
        sub_id = core.spawn("task")
        assert started.wait(5)
        assert core.steer(sub_id, "more work")["queued"] is True
        core.cancel(sub_id)
        gate.set()
        core.collect(sub_id, wait=True, timeout=5)
        time.sleep(0.05)  # let a (buggy) relaunch reach the client
        assert len(prompts) == 1  # the queued steer never resurrected the turn
    finally:
        gate.set()
        core.shutdown()


def test_steer_refuses_a_cancelled_sub_session(db):
    """The sticky cancel flag must also block a FRESH steer: once the turn ended,
    ``steer`` took the not-busy branch and submitted a brand-new ``_run``, which
    resurrects exactly the work the caller stopped."""
    prompts: list[str] = []

    def responder(prompt):
        prompts.append(prompt)
        return "done"

    core = _core(db, responder)
    try:
        sub_id = core.spawn("task")
        core.collect(sub_id, wait=True, timeout=5)  # the turn is over: sub is idle
        core.cancel(sub_id)
        result = core.steer(sub_id, "more work")
        assert "error" in result and "cancelled" in result["error"]
        assert len(prompts) == 1  # no second turn was ever launched
    finally:
        core.shutdown()


def test_pipeline_leaf_spawned_during_expiry_is_cancelled(db, monkeypatch):
    """TOCTOU: the barrier can expire in the window between a stage's spawn and
    its bookkeeping append. The expiry's snapshot misses that leaf, so nobody
    collects it and nobody cancels it — the WF-2 zombie by another road."""
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.2)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    cancelled = _spy_cancel(core)
    runs: list = []
    real_cls = strategies._PipelineRun

    class Capturing(real_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            fired: list = []
            real_expire = self._expire

            def expire_once():  # production fires the expiry exactly once
                if fired:
                    return
                fired.append(True)
                real_expire()

            self._expire = expire_once
            runs.append(self)

    monkeypatch.setattr(strategies, "_PipelineRun", Capturing)
    engine = _engine(core)
    real_spawn = engine.spawn_leaf_with_done
    spawned: list[str] = []

    def racing(prompt, on_done):
        sub_id = real_spawn(prompt, on_done)
        spawned.append(sub_id)
        runs[0]._expire()  # lands in the gap: spawn returned, append hasn't run
        return sub_id

    engine.spawn_leaf_with_done = racing
    spec = validate_spec(
        {
            "meta": {"name": "p"},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": "${args.items}",
                    "stages": [{"prompt": "s ${item}"}],
                }
            ],
        }
    )
    try:
        engine.run(spec, {"items": ["x"]})
        assert cancelled == spawned  # the stranded leaf was cancelled, not leaked
    finally:
        gate.set()
        core.shutdown()


def _service(db, home, responder):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


def test_service_cancel_returns_now_and_stops_scheduling_nodes(db, tmp_path):
    """The REAL cancel path (workflow_cancel -> WorkflowService.cancel): it must
    not block the agent's tool thread on a leaf mid-provider-call, and it must
    stop the engine's node loop instead of racing the pool shutdown (which spams
    'cannot schedule new futures after shutdown' engine faults)."""
    started = threading.Event()
    gate = threading.Event()
    prompts: list[str] = []

    def responder(prompt):
        prompts.append(prompt)
        started.set()
        gate.wait(3)
        return "R"

    svc = _service(db, tmp_path, responder)
    spec = {
        "meta": {"name": "chain"},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "one"},
            {"id": "b", "type": "agent", "prompt": "two"},
            {"id": "c", "type": "agent", "prompt": "three"},
        ],
    }
    try:
        run_id = svc.start(spec, {})["run_id"]
        assert started.wait(5)
        began = time.monotonic()
        assert svc.cancel(run_id)["ok"] is True
        assert time.monotonic() - began < 1.0  # never waits out the blocked leaf
        gate.set()
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "cancelled"
        assert len(prompts) == 1  # b and c were never spawned
        faults = "\n".join(out["faults"])
        assert "run cancelled before node" in faults
        assert "cannot schedule new futures" not in faults
        assert out["engine_faults"] == 0
    finally:
        gate.set()
        svc.shutdown()
