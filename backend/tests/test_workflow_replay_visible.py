"""#61 — o replay deixa de ser invisível fora do audit.

Um nó replayado e um nó executado emitiam `COMPLETE` idêntico: só o ledger de
auditoria distinguia. Aqui o progresso por nó, o rollup do `workflow_status` e a
live view passam a dizer o que veio do cache — e quanto isso poupou.
"""

from __future__ import annotations

from typing import Any

import pytest

from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.events import NODE, PLAN
from lohra.workflow.progress import ProgressTracker
from lohra.workflow.schema import validate_spec
from tests.test_workflow_token_budget import _core

# Every leaf costs 5 in + 3 out (tests.test_loop._text_response), so one replayed
# cell saves exactly 8 tokens — the number this file can assert on the nose.
CELL_TOKENS = 8


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _responder(prompt: str) -> str:
    return "R"


_SPEC: dict[str, Any] = {
    "meta": {"name": "replay", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}


def _engine(db, run_id: str, **kw) -> WorkflowEngine:
    return WorkflowEngine(
        _core(db, _responder), budget=Budget(), cache=NodeCache(db, run_id),
        run_id=run_id, **kw,
    )


def _stretch(db, run_id: str, spec: dict, **kw):
    """One stretch of a run: (engine, result). The engine is kept so the LIVE
    progress read — the one ``workflow_status`` makes — can be inspected."""
    engine = _engine(db, run_id, **kw)
    try:
        return engine, engine.run(validate_spec(spec), {})
    finally:
        engine.core.shutdown()


def _nodes(snapshot: dict) -> dict[str, dict]:
    return {node["id"]: node for node in snapshot["nodes"]}


# --- 1. the tracker itself --------------------------------------------------


def test_a_tracker_marks_the_node_whose_cell_came_from_the_cache():
    tracker = ProgressTracker()
    tracker.reset(["a", "b"])
    tracker.mark_replayed("a", CELL_TOKENS)
    tracker.settle("a", "out")
    node = _nodes(tracker.snapshot())["a"]
    assert node["state"] == "complete"
    assert node["replayed"] is True and node["replayed_cells"] == 1
    assert node["tokens_saved"] == CELL_TOKENS
    # ...and a node that really ran says nothing at all, rather than "false".
    assert "replayed" not in _nodes(tracker.snapshot())["b"]


def test_an_unpriced_replay_never_claims_the_cell_was_free():
    """A cell cached before the price sidecar existed has no price. ``0`` there
    would read as "this replay saved nothing", which is the opposite fact."""
    tracker = ProgressTracker()
    tracker.reset(["a"])
    tracker.mark_replayed("a", None)
    node = _nodes(tracker.snapshot())["a"]
    assert node["replayed"] is True
    assert "tokens_saved" not in node


def test_a_fan_out_counts_every_replayed_cell_not_just_the_node():
    tracker = ProgressTracker()
    tracker.reset(["p"])
    for _ in range(6):
        tracker.mark_replayed("p", CELL_TOKENS)
    node = _nodes(tracker.snapshot())["p"]
    assert node["replayed_cells"] == 6 and node["tokens_saved"] == 6 * CELL_TOKENS


# --- 2. the engine: a real resume ------------------------------------------


def test_a_first_run_replays_nothing_and_says_so(db):
    engine, result = _stretch(db, "run-1", _SPEC)
    assert result.status == "complete", result.faults
    assert result.cells_replayed == 0 and result.tokens_saved == 0
    assert "replayed" not in _nodes(engine.progress_snapshot())["a"]


def test_a_resumed_run_names_the_nodes_it_replayed_and_what_they_saved(db):
    assert _stretch(db, "run-1", _SPEC)[1].status == "complete"
    engine, result = _stretch(db, "run-1", _SPEC)  # same cache, same identities
    assert result.status == "complete", result.faults
    assert result.cells_replayed == 2
    assert result.tokens_saved == 2 * CELL_TOKENS
    nodes = _nodes(engine.progress_snapshot())
    assert nodes["a"]["replayed"] is True and nodes["a"]["tokens_saved"] == CELL_TOKENS
    assert nodes["b"]["replayed"] is True


def test_a_replayed_pipeline_reports_every_cell_it_did_not_re_pay(db):
    spec = {
        "meta": {"name": "replay", "version": 1},
        "nodes": [
            {
                "id": "p",
                "type": "pipeline",
                "items": ["x", "y", "z"],
                "stages": ["draft ${item}", "polish ${stage.result}"],
            }
        ],
    }
    assert _stretch(db, "run-1", spec)[1].status == "complete"
    engine, result = _stretch(db, "run-1", spec)
    assert result.cells_replayed == 6  # 3 items x 2 stages
    assert result.tokens_saved == 6 * CELL_TOKENS
    assert _nodes(engine.progress_snapshot())["p"]["replayed_cells"] == 6


def test_a_nested_templates_replays_fold_into_the_parents_totals(db):
    child = {
        "meta": {"name": "child", "version": 2},
        "nodes": [{"id": "leaf", "type": "agent", "prompt": "do ${args.x}"}],
    }
    parent = {
        "meta": {"name": "replay", "version": 1},
        "nodes": [{"id": "sub", "type": "workflow", "ref": "child", "args": {"x": "hi"}}],
    }
    loader = {"child": child}.get
    assert _stretch(db, "run-1", parent, loader=loader)[1].status == "complete"
    engine, result = _stretch(db, "run-1", parent, loader=loader)
    # The nested engine keeps its own tracker, but the METRIC folds up: a parent
    # that reported 0 would say a fully cached sub-workflow cost it a full run.
    assert result.cells_replayed == 1 and result.tokens_saved == CELL_TOKENS
    # ...and so does the parent's own progress line, or a reader who only sees
    # the parent's DAG watches a node finish instantly with no explanation.
    sub = _nodes(engine.progress_snapshot())["sub"]
    assert sub["replayed"] is True and sub["replayed_cells"] == 1
    assert sub["tokens_saved"] == CELL_TOKENS


# --- 3. the live view -------------------------------------------------------


def _node_event(**extra):
    from lohra.workflow.liveview import render_event

    payload = {
        "node_id": "a", "state": "complete", "done": 1, "total": 2,
        "running": 0, "pending": 1, "tokens": 40, **extra,
    }
    return render_event("run-12345678", NODE, payload)[0]


def test_the_live_view_marks_a_replayed_node_and_leaves_the_others_alone():
    assert "⟲" in _node_event(replayed=True)
    assert "⟲" not in _node_event()


def test_the_replay_glyph_folds_to_exactly_one_ascii_character():
    """The TUI block truncates a line to the terminal width, so a fold that GREW
    a line by one would wrap it — and a wrapped line is what the block's cursor
    arithmetic cannot survive."""
    from lohra.workflow.liveview import _ascii

    line = _node_event(replayed=True)
    assert len(_ascii(line)) == len(line)
    assert "⟲" not in _ascii(line)


# --- 4. workflow_status, in this process and across one -----------------------


def test_workflow_status_carries_the_replay_per_node_and_in_the_rollup(db, tmp_path):
    from tests.test_workflow_operability import _service

    svc = _service(db, tmp_path, _responder)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        fresh = svc.status(run_id)
        assert fresh["cells_replayed"] == 0 and fresh["tokens_saved"] == 0
        svc.start(_SPEC, {}, resume_run_id=run_id)  # everything is cached
        status = svc.status(run_id, wait=True, timeout=10)
        assert status["status"] == "complete"
        assert status["cells_replayed"] == 2
        assert status["tokens_saved"] == 2 * CELL_TOKENS
        assert _nodes(status["progress"])["a"]["replayed"] is True
    finally:
        svc.shutdown()


def test_another_process_reads_the_same_replay_off_the_durable_line(db, tmp_path):
    """WF-29's rule: the facts a resume needs ride the run's persisted line. A
    reader that never owned the run must see what the cache saved it too."""
    from tests.test_workflow_operability import _service

    svc = _service(db, tmp_path, _responder)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        svc.start(_SPEC, {}, resume_run_id=run_id)
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()
    reader = _service(db, tmp_path, _responder)  # a fresh process, no local state
    try:
        line = reader.status(run_id)
        assert line["observation"]["source"] == "durable_store"
        assert line["cells_replayed"] == 2
        assert line["tokens_saved"] == 2 * CELL_TOKENS
        assert _nodes(line["progress"])["a"]["replayed"] is True
    finally:
        reader.shutdown()


def test_the_tui_block_marks_a_replayed_node_and_keeps_it_marked():
    """The fancy mode renders its own lines — a glyph that only reached the
    append-only view would be half of "the live view marks it"."""
    from lohra.workflow.liveview_tui import LiveBlock

    block = LiveBlock("run-1")
    block.update(PLAN, {"name": "w", "nodes": [{"id": "a", "type": "agent"}]})
    block.update(NODE, {"node_id": "a", "state": "running", "done": 0, "total": 1})
    assert "⟲" not in block.compute_frame(width=120)[0]
    block.update(NODE, {"node_id": "a", "state": "complete", "done": 1, "total": 1,
                        "replayed": True})
    assert "⟲" in block.compute_frame(width=120)[0]
