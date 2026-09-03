"""Tests for the no-barrier pipeline scheduler (Fase 8, Milestone D).

The two tests that DEFINE done (a barrier-per-stage impl passes naive tests):
- no-barrier: a fast item's full chain finishes while a slow item's stage-0 is
  still blocked (a barrier impl DEADLOCKS this and times out).
- throughput: items > pool_width through 2 stages complete with <= pool_width
  leaves concurrent (fails if on_done ever blocks a pool worker).
"""

import threading

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


class ScriptedClient(ModelClient):
    def __init__(self, responder):
        self._responder = responder

    def _prompt(self, kwargs):
        msgs = kwargs.get("messages") or []
        return " ".join(m.get("content", "") for m in msgs if isinstance(m.get("content"), str))

    def create(self, **kwargs):
        return _text_response(self._responder(self._prompt(kwargs)))

    def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
        # ``abort_check`` is named, never forwarded: this responder answers in
        # one piece, so it has no event boundary at which to honour an
        # interrupt (the streaming abort lives in test_stream_abort.py).
        return self.create(**kwargs)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, responder, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _pipeline_spec(stages):
    return {
        "meta": {"name": "p"},
        "nodes": [{"id": "p", "type": "pipeline", "items": "${args.items}", "stages": stages}],
    }


def test_results_in_input_order(db):
    core = _core(db, lambda p: p.split("up ")[-1].strip().upper() if "up " in p else "ok")
    try:
        spec = validate_spec(_pipeline_spec([{"type": "agent", "prompt": "up ${item}"}]))
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"items": ["a", "b", "c"]})
        assert result.outputs["p"] == ["A", "B", "C"]
    finally:
        core.shutdown()


def test_dead_stage_drops_only_that_item(db):
    # item "boom" -> stage errors (client raises) -> that item None; others survive
    def responder(prompt):
        if "boom" in prompt:
            raise RuntimeError("stage died")
        return "ok"

    core = _core(db, responder)
    try:
        spec = validate_spec(_pipeline_spec([{"type": "agent", "prompt": "do ${item}"}]))
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"items": ["x", "boom", "y"]})
        assert result.outputs["p"] == ["ok", None, "ok"]
    finally:
        core.shutdown()


def test_pipeline_stage_retries_on_invalid_then_succeeds(db):
    # a schema'd stage: first leaf returns invalid JSON, a FRESH re-spawn returns
    # valid -> the item is NOT dropped (bounded retry-via-respawn, non-blocking).
    replies = iter(["not json", '{"v": 1}'])

    def factory():
        from tests.test_loop import FakeClient

        return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
                     client=FakeClient([_text_response(next(replies, '{"v": 0}'))]))

    core = OrchestrationCore(db, factory, max_concurrent=4)
    schema = {"type": "object", "properties": {"v": {"type": "integer"}}, "required": ["v"]}
    spec = validate_spec({"meta": {"name": "p"}, "nodes": [
        {"id": "p", "type": "pipeline", "items": "${args.items}",
         "stages": [{"type": "agent", "prompt": "go ${item}", "schema": schema}]}]})
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"items": ["a"]})
        assert result.outputs["p"] == [{"v": 1}]  # retried, not dropped
        assert result.validation_retries >= 1
    finally:
        core.shutdown()


def test_no_barrier_fast_item_finishes_while_slow_item_blocks(db):
    # A barrier-per-stage impl DEADLOCKS here (B.stage0 waits for A, A.stage1 can't
    # start until B.stage0 done) and times out. No-barrier completes.
    a_done = threading.Event()

    def responder(prompt):
        if "s1 A" in prompt:  # A's final stage
            a_done.set()
            return "A1"
        if "s0 B" in prompt:  # B's first stage — block until A's whole chain done
            a_done.wait(5)
            return "B0"
        if "s1 B" in prompt:
            return "B1"
        return "ok"

    core = _core(db, responder, pool_width=4)
    try:
        spec = validate_spec(
            _pipeline_spec([{"type": "agent", "prompt": "s0 ${item}"},
                            {"type": "agent", "prompt": "s1 ${item}"}])
        )
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"items": ["A", "B"]})
        assert result.outputs["p"] == ["A1", "B1"]  # both completed -> no deadlock
        assert a_done.is_set()
    finally:
        core.shutdown()


def test_throughput_bounded_by_pool_width_no_deadlock(db):
    # 6 items x 2 stages on a width-2 pool: completes, and never more than 2 leaves
    # run at once (would fail if on_done blocked a worker).
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def responder(prompt):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        # brief overlap window so concurrency is observable
        threading.Event().wait(0.01)
        with lock:
            state["now"] -= 1
        return "ok"

    core = _core(db, responder, pool_width=2)
    try:
        spec = validate_spec(
            _pipeline_spec([{"type": "agent", "prompt": "a ${item}"},
                            {"type": "agent", "prompt": "b ${item}"}])
        )
        result = WorkflowEngine(core, budget=Budget(pool_width=2)).run(
            spec, {"items": [str(i) for i in range(6)]}
        )
        assert len(result.outputs["p"]) == 6
        assert all(o == "ok" for o in result.outputs["p"])
        assert state["peak"] <= 2  # never exceeded the pool width
    finally:
        core.shutdown()
