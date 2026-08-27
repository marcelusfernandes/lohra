"""Tests for resume / content-addressed cache (Fase 8, Milestone G)."""

import threading

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache, content_hash
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _counting_core(db, counter, reply="R"):
    """A core whose every leaf increments `counter` when it actually runs."""

    class CountingClient:
        def create(self, **kwargs):
            counter[0] += 1
            return _text_response(reply)

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=CountingClient(),
        )

    return OrchestrationCore(db, factory)


# --- content_hash ---


def test_content_hash_is_stable_and_order_sensitive():
    assert content_hash("a", 1, {"x": 2}) == content_hash("a", 1, {"x": 2})
    assert content_hash("a", 1) != content_hash("a", 2)


# --- cache get/put ---


def test_cache_put_get_roundtrip(db):
    cache = NodeCache(db, "run-1")
    cache.put_complete("h1", "node", {"n": 5})
    assert cache.get("h1") == (True, {"n": 5})
    assert cache.get("missing") == (False, None)


def test_cache_is_run_scoped(db):
    NodeCache(db, "run-1").put_complete("h", "n", "v")
    assert NodeCache(db, "run-2").get("h") == (False, None)  # cross-run reuse OFF (§6.3)


# --- resume: a re-run with the same run_id replays cached cells ---


def _spec():
    return validate_spec(
        {
            "meta": {"name": "demo", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "first"},
                {"id": "b", "type": "agent", "prompt": "use ${a}", "depends_on": ["a"]},
            ],
        }
    )


def test_resume_replays_cached_cells_without_respawning(db):
    counter = [0]
    spec = _spec()
    # First run: both nodes spawn (2 leaf runs).
    core1 = _counting_core(db, counter)
    try:
        r1 = WorkflowEngine(core1, budget=Budget(), cache=NodeCache(db, "run-X")).run(spec, {})
        assert counter[0] == 2 and r1.outputs["a"] == "R"
    finally:
        core1.shutdown()
    # Resume with the SAME run_id: both cells are cached -> zero new leaf runs.
    core2 = _counting_core(db, counter)
    try:
        r2 = WorkflowEngine(core2, budget=Budget(), cache=NodeCache(db, "run-X")).run(spec, {})
        assert counter[0] == 2  # unchanged — replayed from cache
        assert r2.outputs["a"] == "R" and r2.outputs["b"] == "R"
    finally:
        core2.shutdown()


def test_no_cache_always_runs(db):
    counter = [0]
    core = _counting_core(db, counter)
    try:
        WorkflowEngine(core, budget=Budget()).run(_spec(), {})  # cache=None
        WorkflowEngine(core, budget=Budget()).run(_spec(), {})
        assert counter[0] == 4  # ran all 4 (no cache to replay)
    finally:
        core.shutdown()


def test_partial_resume_respawns_only_incomplete_cells(db):
    # The scenario resume exists for: some items die on run 1; on resume only the
    # previously-failed (uncached) items re-spawn, the completed ones replay.
    fail = {"items": {"b"}}  # item "b" fails on run 1

    class FlakyClient:
        def __init__(self, counter):
            self._counter = counter

        def create(self, **kwargs):
            self._counter[0] += 1
            msgs = kwargs.get("messages") or []
            text = " ".join(m.get("content", "") for m in msgs if isinstance(m.get("content"), str))
            if any(f"s {it}" in text for it in fail["items"]):
                raise RuntimeError("flaky item failed")
            return _text_response("OK")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    counter = [0]

    def factory():
        return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
                     client=FlakyClient(counter))

    spec = validate_spec({"meta": {"name": "p", "version": 1},
                          "nodes": [{"id": "p", "type": "pipeline", "items": "${args.items}",
                                     "stages": [{"type": "agent", "prompt": "s ${item}"}]}]})
    core1 = OrchestrationCore(db, factory)
    try:
        r1 = WorkflowEngine(core1, budget=Budget(), cache=NodeCache(db, "run-PR")).run(
            spec, {"items": ["a", "b", "c"]})
        assert r1.outputs["p"] == ["OK", None, "OK"]  # b failed
        assert counter[0] == 3  # all three ran
    finally:
        core1.shutdown()
    fail["items"] = set()  # b will now succeed
    core2 = OrchestrationCore(db, factory)
    try:
        r2 = WorkflowEngine(core2, budget=Budget(), cache=NodeCache(db, "run-PR")).run(
            spec, {"items": ["a", "b", "c"]})
        # only "b" re-spawns (a/c replay from cache); a dead leaf was NOT tombstoned
        assert counter[0] == 4
        assert r2.outputs["p"] == ["OK", "OK", "OK"]
    finally:
        core2.shutdown()


def test_upstream_change_misses_cache(db):
    # Changing args changes b's resolved prompt -> b's content_hash changes -> miss.
    counter = [0]
    spec = validate_spec({"meta": {"name": "demo", "version": 1},
                          "nodes": [{"id": "b", "type": "agent", "prompt": "echo ${args.x}"}]})
    core = _counting_core(db, counter)
    try:
        WorkflowEngine(core, budget=Budget(), cache=NodeCache(db, "run-U")).run(spec, {"x": "one"})
        WorkflowEngine(core, budget=Budget(), cache=NodeCache(db, "run-U")).run(spec, {"x": "two"})
        assert counter[0] == 2  # different input -> re-ran, no stale hit
        WorkflowEngine(core, budget=Budget(), cache=NodeCache(db, "run-U")).run(spec, {"x": "one"})
        assert counter[0] == 2  # repeat of "one" hits cache
    finally:
        core.shutdown()


def test_pipeline_resume_is_per_item_stage(db):
    counter = [0]
    spec = validate_spec(
        {
            "meta": {"name": "p", "version": 1},
            "nodes": [{"id": "p", "type": "pipeline", "items": "${args.items}",
                       "stages": [{"type": "agent", "prompt": "s ${item}"}]}],
        }
    )
    core1 = _counting_core(db, counter)
    try:
        WorkflowEngine(core1, budget=Budget(), cache=NodeCache(db, "run-P")).run(spec, {"items": ["a", "b"]})
        assert counter[0] == 2  # 2 items x 1 stage
    finally:
        core1.shutdown()
    # resume same run_id -> both (item,stage) cells cached, nothing re-spawns
    core2 = _counting_core(db, counter)
    try:
        result = WorkflowEngine(core2, budget=Budget(), cache=NodeCache(db, "run-P")).run(
            spec, {"items": ["a", "b"]}
        )
        assert counter[0] == 2  # unchanged
        assert result.outputs["p"] == ["R", "R"]
    finally:
        core2.shutdown()


def test_pipeline_cell_identity_includes_the_item(db):
    # A stage prompt that does NOT interpolate ${item}: with only (node, stage,
    # prompt) in the key, N items collapse onto ONE cell and the resume replays a
    # single item's answer for every item.
    counter = [0]
    lock = threading.Lock()

    class UniqueClient:
        def create(self, **kwargs):
            with lock:
                counter[0] += 1
                nth = counter[0]
            return _text_response(f"R{nth}")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    def factory():
        return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
                     client=UniqueClient())

    spec = validate_spec({"meta": {"name": "p", "version": 1},
                          "nodes": [{"id": "p", "type": "pipeline", "items": "${args.items}",
                                     "stages": [{"type": "agent", "prompt": "same for every item"}]}]})
    core1 = OrchestrationCore(db, factory)
    try:
        r1 = WorkflowEngine(core1, budget=Budget(), cache=NodeCache(db, "run-ID")).run(
            spec, {"items": ["a", "b"]})
        assert r1.outputs["p"][0] != r1.outputs["p"][1]  # two items -> two leaves
    finally:
        core1.shutdown()
    core2 = OrchestrationCore(db, factory)
    try:
        r2 = WorkflowEngine(core2, budget=Budget(), cache=NodeCache(db, "run-ID")).run(
            spec, {"items": ["a", "b"]})
        assert r2.outputs["p"] == r1.outputs["p"]  # each item replays ITS OWN cell
    finally:
        core2.shutdown()
