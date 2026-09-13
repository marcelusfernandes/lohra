"""#90 legacy compatibility: row-local proof, read aliases, no invented history."""

import json
import sqlite3

import pytest

from lohra.agent.types import Usage
from lohra.state import SessionDB
from lohra.workflow.artifact import ArtifactScope
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache, content_hash
from lohra.workflow.cache_preview import preview_resume
from lohra.workflow.cell_identity import LEGACY_SCOPE_UNPROVEN
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_workflow_nested_identity import CHILD, parent, run
from tests.test_workflow_token_budget import _core


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _manifest_run(db, spec, child, target, calls):
    def reply(prompt):
        calls.append(prompt)
        return json.dumps({"path": str(target)})

    core = _core(db, reply)
    try:
        return WorkflowEngine(
            core, budget=Budget(), cache=NodeCache(db, "run"), run_id="run",
            loader=lambda ref: child, artifact_scope=ArtifactScope.of(str(target.parent)),
        ).run(validate_spec(spec), {})
    finally:
        core.shutdown()


def _legacy_manifest(db, tmp_path):
    target = tmp_path / "report.txt"
    target.write_text("a report", encoding="utf-8")
    child = {**CHILD, "nodes": [
        {"id": "write", "type": "agent", "prompt": "write", "schema_ref": "artifact_manifest"},
    ]}
    calls = []
    result = _manifest_run(db, parent("a"), child, target, calls)
    assert result.status == "complete" and len(calls) == 1
    # Convert ONLY this synthetic row to the exact pre-#90 representation.
    # Its real harness-measured artifact owners and its paid ledger are kept.
    from lohra.workflow.cache_preview import _PreviewEngine, _cell_hash_of

    parsed = validate_spec(child)
    legacy = _cell_hash_of(_PreviewEngine(parsed, None), parsed.nodes[0], {"args": {}})
    current = db.cache_hashes_for_node("run", "sub[a]:write")[0]
    assert db.cache_get("run", current)["artifact_verification"] == "verified"
    db._connection.execute(
        "UPDATE workflow_node_cache SET content_hash=?,node_id='write',node_scope_json=NULL "
        "WHERE content_hash=?", (legacy, current),
    )
    db._connection.execute(
        "UPDATE workflow_node_cost SET content_hash=? WHERE content_hash=?", (legacy, current),
    )
    db._connection.commit()
    return child, target, calls, legacy


def test_proven_legacy_manifest_replays_without_copying_or_repricing(db, tmp_path):
    child, target, calls, legacy = _legacy_manifest(db, tmp_path)
    cache = NodeCache(db, "run")
    before = (cache.total_split(), cache.cost_count(), db.cache_get("run", legacy))
    for _ in range(2):
        preview = preview_resume(
            db, "run", validate_spec(parent("a")), {}, loader=lambda ref: child,
            artifact_scope=ArtifactScope.of(str(tmp_path)),
        )
        assert preview["replay"] == 1 and preview["tokens_to_repay"] == 0
        result = _manifest_run(db, parent("a"), child, target, calls)
        assert result.cells_replayed == 1 and result.tokens_saved == 8
    assert len(calls) == 1
    assert (cache.total_split(), cache.cost_count(), db.cache_get("run", legacy)) == before
    assert db._connection.execute("SELECT count(*) FROM workflow_node_cache").fetchone()[0] == 1


@pytest.mark.parametrize("damage", ["sibling", "removed_owner", "mixed", "empty", "malformed", "raw_id"])
def test_legacy_owner_must_be_explicit_consistent_and_belong_to_this_cell(db, tmp_path, damage):
    child, target, calls, legacy = _legacy_manifest(db, tmp_path)
    spec = parent("b") if damage == "sibling" else parent("a")
    row = db.cache_get("run", legacy)
    entries = json.loads(row["artifact_json"])
    if damage == "removed_owner":
        del entries[0]["owner"]
    elif damage == "mixed":
        entries.append({**entries[0], "owner": "sub[b]:write"})
    elif damage == "empty":
        entries = []
    blob = "{broken" if damage == "malformed" else json.dumps(entries)
    db._connection.execute(
        "UPDATE workflow_node_cache SET artifact_json=?,node_id=? WHERE content_hash=?",
        (blob, "other" if damage == "raw_id" else "write", legacy),
    )
    db._connection.commit()
    preview = preview_resume(db, "run", validate_spec(spec), {}, loader=lambda ref: child)
    assert preview["replay"] == 0 and preview["tokens_to_repay"] == 0
    assert preview["unknown"][0]["why"] == LEGACY_SCOPE_UNPROVEN
    result = _manifest_run(db, spec, child, target, calls)
    assert result.status == "complete" and len(calls) == 2
    assert result.cells_replayed == 0
    assert any("legacy cache" in fault for fault in result.advisory_faults)


def test_legacy_alias_still_rechecks_files_and_uses_the_original_cost(db, tmp_path):
    child, target, calls, legacy = _legacy_manifest(db, tmp_path)
    target.write_text("external change", encoding="utf-8")
    preview = preview_resume(
        db, "run", validate_spec(parent("a")), {}, loader=lambda ref: child,
        artifact_scope=ArtifactScope.of(str(tmp_path)),
    )
    assert preview["invalidate"] == 1 and preview["tokens_to_repay"] == 8
    assert preview["invalidated"] == [
        {"node_id": "sub[a]:write", "reason": "artifact_changed", "template": "child"},
    ]
    result = _manifest_run(db, parent("a"), child, target, calls)
    assert result.cells_replayed == 0 and len(calls) == 2
    assert NodeCache(db, "run").total_cost() == (10, 6)  # two actual executions
    assert db.cache_get("run", legacy) is not None  # historical spend remains


def test_root_checkpoint_migration_is_additive_and_does_not_invent_scope(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = content_hash("child", 1, "cp", "checkpoint", "Proceed?")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE workflow_node_cache(content_hash TEXT,run_id TEXT,node_id TEXT,"
        "output_json TEXT,status TEXT,updated_at REAL,PRIMARY KEY(run_id,content_hash))"
    )
    conn.execute("INSERT INTO workflow_node_cache VALUES(?,?,?,?,?,?)", (legacy, "run", "cp", '"sim"', "complete", 1))
    conn.commit()
    conn.close()
    for iteration in range(2):
        db = SessionDB(path)
        try:
            if iteration == 0:
                assert db.cache_get("run", legacy)["node_scope_json"] is None
                assert run(db, CHILD).status == "paused"
                assert run(db, CHILD, answers={"cp": "sim"}).status == "complete"
            replay = run(db, CHILD)
            assert replay.status == "complete" and replay.cells_replayed == 1
            assert db.cache_get("run", legacy)["node_scope_json"] == "[]"
        finally:
            db.close()


@pytest.mark.parametrize("scope", [None, '{broken', '["sibling"]', '"a"'])
def test_checkpoint_provenance_does_not_come_from_version_or_current_spec(db, scope):
    legacy = content_hash("child", 1, "cp", "checkpoint", "Proceed?")
    db.cache_put_with_cost("run", legacy, "cp", '"sim"', "complete", stamp=("policy", "999.0"))
    db._connection.execute("UPDATE workflow_node_cache SET node_scope_json=?", (scope,))
    db._connection.commit()
    assert run(db, CHILD).status == "paused"


def test_scope_and_price_share_the_cell_fence_and_rollback(db, monkeypatch):
    first = db.acquire_run_lease("run", "first", ttl_seconds=1, now=1)
    assert db.acquire_run_lease("run", "second", ttl_seconds=1, now=3) > first
    NodeCache(db, "run", fence=first).put_complete("old", "cp", "sim", node_scope=())
    assert db.cache_get("run", "old") is None
    fence = db.run_fence_of("run")
    monkeypatch.setattr(db, "_COST_SQL", "THIS IS NOT SQL")
    with pytest.raises(sqlite3.Error):
        NodeCache(db, "run", fence=fence).put_complete(
            "failed", "a", "result", Usage(input_tokens=8), node_scope=("call",),
        )
    assert db.cache_get("run", "failed") is None


def test_authored_namespace_punctuation_cannot_become_cache_scope(db):
    from lohra.workflow.cell_identity import CellKeys, scoped_node

    root, child = CellKeys(), CellKeys(("a",))
    root_id, child_id = "sub[a]:x", "x"
    assert scoped_node((), root_id) == scoped_node(("a",), child_id)
    root_hash = root.hash(("same", 1), root_id, "agent", "same")
    child_hash = child.hash(("same", 1), child_id, "agent", "same")
    assert root_hash != child_hash
    cache = NodeCache(db, "run")
    cache.put_complete(root_hash, root_id, "root", Usage(input_tokens=7), node_scope=())
    assert not child.read(cache, child_hash, child_id).hit
    assert cache.hashes_for_node(root_id, node_scope=("a",)) == []
    cache.put_complete(child_hash, root_id, "child", Usage(input_tokens=9), node_scope=("a",))
    assert root.read(cache, root_hash, root_id).output == "root"
    assert child.read(cache, child_hash, child_id).output == "child"
    assert cache.hashes_for_node(root_id, node_scope=()) == [root_hash]
    assert cache.hashes_for_node(root_id, node_scope=("a",)) == [child_hash]


def test_nested_pipeline_keeps_composite_cells_in_each_call_scope(db):
    child = {**CHILD, "nodes": [{
        "id": "p#authored", "type": "pipeline", "items": ["one", "two"],
        "stages": [{"prompt": "${item}"}, {"prompt": "then ${stage.result}"}],
    }]}
    calls = []
    first = run(db, parent("a", "b"), child=child, seen=calls)
    assert first.status == "complete" and len(calls) == 8
    assert first.cells_replayed == 0
    preview = preview_resume(db, "run", validate_spec(parent("a", "b")), {}, loader=lambda ref: child)
    assert preview["replay"] == 8 and preview["tokens_to_repay"] == 0
    replay = run(db, parent("a", "b"), child=child, seen=calls)
    assert replay.status == "complete" and len(calls) == 8
    assert replay.cells_replayed == 8 and replay.tokens_saved == 64
    rows = db._connection.execute("SELECT node_scope_json, count(*) FROM workflow_node_cache GROUP BY node_scope_json").fetchall()
    assert {row[0]: row[1] for row in rows} == {'["a"]': 4, '["b"]': 4}


def test_legacy_revalidation_reason_survives_audit_sanitizing(db):
    from lohra.workflow.audit import sanitize_audit_event

    key = content_hash("child", 1, "cp", "checkpoint", "Proceed?")
    db.cache_put("run", key, "cp", '\"sim\"', "complete")
    events = []
    core = _core(db, lambda prompt: prompt)
    try:
        result = WorkflowEngine(
            core, budget=Budget(), cache=NodeCache(db, "run"), on_audit=events.append, run_id="run",
        ).run(validate_spec(CHILD), {})
    finally:
        core.shutdown()
    assert result.status == "paused"
    miss = next(event for event in events if event["event_type"] == "cache.missed")
    assert sanitize_audit_event(miss)["data"]["reason"] == LEGACY_SCOPE_UNPROVEN
