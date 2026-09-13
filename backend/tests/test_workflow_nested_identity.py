"""#90: a cache cell belongs to an invocation, including a human approval.

The T14 claim of re-paying a completed sibling was wrong: its first execution
was labelled as invalidation. Pin that control beside the actual RED cases.
All experiments use synthetic SQLite and fake providers, never a real profile.
"""

import json

import pytest

from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache, content_hash
from lohra.workflow.cache_preview import preview_resume
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_workflow_token_budget import _core


CHILD = {
    "meta": {"name": "child", "version": 1},
    "nodes": [{"id": "cp", "type": "checkpoint", "prompt": "Proceed?", "accept": ["sim"]}],
}


def parent(*calls):
    return {
        "meta": {"name": "parent", "version": 1},
        "nodes": [
            {"id": call, "type": "workflow", "ref": "child", "args": {"target": call},
             **({"depends_on": [calls[i - 1]]} if i else {})}
            for i, call in enumerate(calls)
        ],
    }


def run(db, spec, child=CHILD, answers=None, seen=None):
    def reply(prompt):
        if seen is not None:
            seen.append(prompt)
        return prompt

    core = _core(db, reply)
    try:
        return WorkflowEngine(
            core, budget=Budget(), cache=NodeCache(db, "run"), run_id="run",
            loader=lambda ref: child, checkpoint_answers=answers,
        ).run(validate_spec(spec), {})
    finally:
        core.shutdown()


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def test_identical_sibling_questions_need_two_approvals(db):
    result = run(db, parent("a", "b"), answers={"sub[a]:cp": "sim"})
    assert result.status == "paused", result.outputs
    assert result.checkpoint == {
        "node_id": "sub[b]:cp", "prompt": "Proceed?", "template": "child",
    }


@pytest.mark.parametrize("root_first", [True, False])
def test_same_spec_identity_cannot_share_approval_between_root_and_child(db, root_first):
    spec = parent("a")
    spec["meta"] = CHILD["meta"]
    root = dict(CHILD["nodes"][0])
    if root_first:
        spec["nodes"][0]["depends_on"] = ["cp"]
        spec["nodes"].insert(0, root)
        answers, expected = {"cp": "sim"}, "sub[a]:cp"
    else:
        root["depends_on"] = ["a"]
        spec["nodes"].append(root)
        answers, expected = {"sub[a]:cp": "sim"}, "cp"
    result = run(db, spec, answers=answers)
    assert result.status == "paused", result.outputs
    assert result.checkpoint["node_id"] == expected


def test_legacy_approval_is_not_attributed_by_a_unique_current_call(db):
    # This row could belong to a removed sibling or to the old root. It contains
    # no record that b was ever asked, even though b is the only current call.
    key = content_hash("child", 1, "cp", "checkpoint", "Proceed?")
    db.cache_put("run", key, "cp", json.dumps("sim"), "complete")
    result = run(db, parent("b"))
    assert result.status == "paused", result.outputs
    assert result.checkpoint["node_id"] == "sub[b]:cp"
    assert "legacy" in json.dumps(result.checkpoint).lower()


def test_legacy_approval_cannot_enter_through_an_unchanged_root_hash(db):
    key = content_hash("child", 1, "cp", "checkpoint", "Proceed?")
    db.cache_put("run", key, "cp", json.dumps("sim"), "complete")
    result = run(db, CHILD)
    assert result.status == "paused", result.outputs
    assert result.checkpoint["node_id"] == "cp"
    assert "legacy" in json.dumps(result.checkpoint).lower()


def test_preview_does_not_charge_a_completed_sibling_for_a_first_execution(db):
    child = {
        **CHILD, "nodes": [
            {**CHILD["nodes"][0], "prompt": "Proceed with ${args.target}?"},
            {"id": "do", "type": "agent", "prompt": "Do ${args.target} after ${cp}"},
        ],
    }
    seen = []
    first = run(db, parent("a", "b"), child, {"sub[a]:cp": "sim"}, seen)
    assert first.status == "paused" and len(seen) == 1
    preview = preview_resume(
        db, "run", validate_spec(parent("a", "b")), {}, loader=lambda ref: child,
        checkpoint_answers={"sub[b]:cp": "sim"},
    )
    assert preview == {
        "replay": 2, "invalidate": 0, "never_completed": 2,
        "tokens_to_repay": 0, "invalidated": [],
    }
    done = run(db, parent("a", "b"), child, {"sub[b]:cp": "sim"}, seen)
    assert done.status == "complete" and len(seen) == 2
    # The original H15 experiment is already GREEN before the fix.
    replay = run(db, parent("a", "b"), child, seen=seen)
    assert replay.cells_replayed == 4 and len(seen) == 2


def test_both_invocations_keep_their_costs_in_the_rollup(db):
    child = {**CHILD, "nodes": [{"id": "do", "type": "agent", "prompt": "Do ${args.target}"}]}
    result = run(db, parent("a", "b"), child)
    assert result.tokens_in + result.tokens_out == 16
    assert set(result.node_costs) == {"sub[a]:do", "sub[b]:do"}
    assert sum(c.usage.input_tokens + c.usage.output_tokens for c in result.node_costs.values()) == 16


def test_separate_identical_approvals_survive_database_reopen(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    try:
        result = run(db, parent("a", "b"), answers={"sub[a]:cp": "sim", "sub[b]:cp": "sim"})
        assert result.status == "complete"
        assert result.cells_replayed == 0
    finally:
        db.close()
    db = SessionDB(path)
    try:
        replay = run(db, parent("a", "b"))
        assert replay.status == "complete" and replay.cells_replayed == 2
        assert replay.outputs == {"a": {"cp": "sim"}, "b": {"cp": "sim"}}
    finally:
        db.close()
