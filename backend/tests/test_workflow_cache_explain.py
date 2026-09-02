"""#44 épico 3 — o ledger diz POR QUE uma célula não replaiou, e sob QUAL spec.

Antes disto o audit gravava ``cache.missed`` com ``data: {}``: um miss sem causa
e um replay sem economia. Como o ``cell_id`` do audit é a identidade ESTRUTURAL
(sha256 de run/role/node_path/branch/item/stage), nenhuma análise post-hoc
consegue separar "nunca completou" de "a identidade mudou" — a causa tem que ser
derivada pelo ENGINE no momento do lookup. E o ``segment.started`` não carregava
name/version, então um pivô que reescreve a spec do run apagava a identidade sob
a qual as células foram escritas.
"""

from __future__ import annotations

from typing import Any

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import (
    MISS_IDENTITY_CHANGED,
    MISS_IDENTITY_CHANGED_OR_SIBLING,
    MISS_NEVER_COMPLETED,
    NodeCache,
)
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db):
    class Client:
        def create(self, **kwargs):
            return _text_response("R")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    return OrchestrationCore(
        db,
        lambda: Agent(
            model="claude-opus-4-8", provider=get_provider_profile("anthropic"), client=Client()
        ),
    )


def _run(db, run_id: str, spec_dict: dict, events: list[dict[str, Any]]):
    spec = validate_spec(spec_dict)
    core = _core(db)
    try:
        return WorkflowEngine(
            core,
            budget=Budget(),
            cache=NodeCache(db, run_id),
            on_audit=events.append,
            run_id=run_id,
        ).run(spec, {})
    finally:
        core.shutdown()


def _cache_events(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event["event_type"] == event_type]


def _spec(model: str | None = None):
    node: dict[str, Any] = {"id": "a", "type": "agent", "prompt": "do it"}
    if model is not None:
        node["model"] = model
    return {"meta": {"name": "explain", "version": 1}, "nodes": [node]}


# --- (v) cache.missed carries a reason -------------------------------------


def test_first_lookup_of_a_node_misses_as_never_completed(db):
    events: list[dict[str, Any]] = []
    _run(db, "run-1", _spec(), events)
    missed = _cache_events(events, "cache.missed")
    assert len(missed) == 1
    assert missed[0]["data"]["reason"] == MISS_NEVER_COMPLETED


def test_a_changed_model_on_a_node_with_a_cell_misses_as_identity_changed(db):
    # The `final_certification` case of the real run: a pivot swapped the model
    # of a node that ALREADY had a cell, and the next resume re-paid ~2.13M
    # tokens as a silent miss.
    events: list[dict[str, Any]] = []
    _run(db, "run-1", _spec("deepseek-v4"), events)
    resume: list[dict[str, Any]] = []
    _run(db, "run-1", _spec("glm-5.3-flash"), resume)
    missed = _cache_events(resume, "cache.missed")
    assert len(missed) == 1
    assert missed[0]["data"]["reason"] == MISS_IDENTITY_CHANGED


def test_an_unchanged_resume_replays_and_never_misses(db):
    events: list[dict[str, Any]] = []
    _run(db, "run-1", _spec(), events)
    resume: list[dict[str, Any]] = []
    _run(db, "run-1", _spec(), resume)
    assert not _cache_events(resume, "cache.missed")
    assert len(_cache_events(resume, "cache.replayed")) == 1


def test_a_pipeline_cell_says_sibling_because_its_node_id_is_shared(db):
    # D6: every (item, stage) cell of a pipeline is stored under the RAW node id,
    # so "a row exists with another hash" cannot tell a changed identity from a
    # sibling item. The ledger says so instead of asserting the stronger claim.
    def spec(items):
        return {
            "meta": {"name": "explain", "version": 1},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": items,
                    "stages": [{"prompt": "s ${item}"}],
                }
            ],
        }

    _run(db, "run-1", spec(["x", "y"]), [])
    # A resume that swaps one item: `x` replays, `z` is a NEW cell — and the rows
    # it sees under node id `p` belong to its siblings, not to an older identity.
    resume: list[dict[str, Any]] = []
    _run(db, "run-1", spec(["x", "z"]), resume)
    assert len(_cache_events(resume, "cache.replayed")) == 1  # x
    missed = _cache_events(resume, "cache.missed")
    assert len(missed) == 1  # z
    assert missed[0]["data"]["reason"] == MISS_IDENTITY_CHANGED_OR_SIBLING


def test_cache_replayed_carries_what_the_replay_saved(db):
    events: list[dict[str, Any]] = []
    _run(db, "run-1", _spec(), events)
    stored = _cache_events(events, "cache.stored")
    assert len(stored) == 1
    chash = stored[0]["identity"]["cell_id"]
    # Re-price the cell with a known split, so the assertion names the axis.
    db.cache_cost_put("run-1", chash, 100, 20, cache_read=5, cache_write=3, reasoning=2)
    resume: list[dict[str, Any]] = []
    _run(db, "run-1", _spec(), resume)
    replayed = _cache_events(resume, "cache.replayed")
    assert len(replayed) == 1
    assert replayed[0]["data"]["tokens_saved"] == 130  # all five meters


def test_an_unpriced_cell_replays_without_inventing_a_saving(db):
    # A cell cached before M5 (and a human's checkpoint answer) has no price row.
    # Absent is the honest report; a 0 would read as "this replay was free".
    events: list[dict[str, Any]] = []
    _run(db, "run-1", _spec(), events)
    db._connection.execute("DELETE FROM workflow_node_cost WHERE run_id = 'run-1'")
    db._connection.commit()
    resume: list[dict[str, Any]] = []
    _run(db, "run-1", _spec(), resume)
    assert "tokens_saved" not in _cache_events(resume, "cache.replayed")[0]["data"]


def test_the_miss_reason_never_changes_workflow_semantics(db):
    # The peek is telemetry: a store that cannot answer it must not break the run.
    class BrokenPeek(NodeCache):
        def hashes_for_node(self, node_id: str, **_: Any) -> list[str]:
            raise RuntimeError("no")

    events: list[dict[str, Any]] = []
    core = _core(db)
    try:
        result = WorkflowEngine(
            core,
            budget=Budget(),
            cache=BrokenPeek(db, "run-1"),
            on_audit=events.append,
            run_id="run-1",
        ).run(validate_spec(_spec()), {})
    finally:
        core.shutdown()
    assert result.outputs["a"] == "R"
    assert "reason" not in _cache_events(events, "cache.missed")[0]["data"]


# --- the audit's closed vocabulary knows these words ------------------------


def test_the_miss_reasons_and_the_new_metadata_survive_sanitization():
    from lohra.workflow.audit import _SAFE_STRING_VALUES, _safe_metadata

    for reason in (MISS_NEVER_COMPLETED, MISS_IDENTITY_CHANGED, MISS_IDENTITY_CHANGED_OR_SIBLING):
        assert reason in _SAFE_STRING_VALUES["reason"]
        assert _safe_metadata(reason, key="reason") == reason
    assert _safe_metadata(4200, key="tokens_saved") == 4200
    assert _safe_metadata("lohra-notion", key="spec_name") == "lohra-notion"
    assert _safe_metadata("4.0", key="spec_version") == "4.0"
    assert _safe_metadata(7, key="spec_version") == 7


# --- (vi) segment.started names the spec the stretch ran under --------------


def _service(db, home):
    class Client:
        def create(self, **kwargs):
            return _text_response("R")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    from lohra.workflow.service import WorkflowService

    return WorkflowService(
        base_child_factory=lambda: Agent(
            model="explain-test",
            provider=get_provider_profile("anthropic"),
            client=Client(),
        ),
        db=db,
        home=home,
        run_concurrency=2,
        max_runs=2,
    )


def test_segment_started_stamps_the_spec_name_and_version(tmp_path):
    # The run keeps ONE spec (a pivot overwrites it), so the stretch has to say
    # which one it ran — otherwise (b) identity_changed and (c) namespace change
    # are indistinguishable after the fact.
    database = SessionDB(str(tmp_path / "state.db"))
    service = _service(database, tmp_path)
    try:
        run_id = service.start(
            {
                "meta": {"name": "explain", "version": "4.0"},
                "nodes": [{"id": "a", "type": "agent", "prompt": "do it"}],
            }
        )["run_id"]
        assert service.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert service._audit.flush(timeout=2)
        rows = database.audit_query(run_id, limit=100)["events"]
        started = [row for row in rows if row["event_type"] == "segment.started"]
        assert len(started) == 1
        assert started[0]["data"]["spec_name"] == "explain"
        assert started[0]["data"]["spec_version"] == "4.0"
        # ...and the fields it already carried are untouched.
        assert started[0]["data"]["resume"] is False
        assert started[0]["data"]["recovered_process"] is False
    finally:
        service.shutdown()
        database.close()
