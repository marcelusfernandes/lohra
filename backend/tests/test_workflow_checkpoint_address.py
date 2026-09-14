"""#106: answer identity must not come from an ambiguous display label."""

import json

import pytest

from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import content_hash
from lohra.workflow.cache_preview import preview_resume
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.launch import route_answer
from lohra.workflow.route_fault import ROUTE_FAULT
from lohra.workflow.runstate_store import DurableRun
from lohra.workflow.runstate_store import RunStateStore
from lohra.workflow.schema import validate_spec
from tests.test_workflow_nested_checkpoint import _install
from tests.test_workflow_nested_identity import CHILD, parent, run
from tests.test_workflow_operability import _service
from tests.test_workflow_token_budget import _core


def collision(root_first):
    spec = parent("a")
    root = {**CHILD["nodes"][0], "id": "sub[a]:cp"}
    spec["nodes"] = [root, *spec["nodes"]] if root_first else [*spec["nodes"], root]
    return spec


@pytest.mark.parametrize("root_first", [True, False])
def test_ambiguous_legacy_answer_is_refused_before_either_gate_opens(root_first):
    db = SessionDB(":memory:")
    try:
        # RED: the validated spec currently completes BOTH guarded checkpoints.
        with pytest.raises(ValueError, match="ambiguous"):
            run(db, collision(root_first), answers={"sub[a]:cp": "sim"})
        assert db._connection.execute("SELECT count(*) FROM workflow_node_cache").fetchone()[0] == 0
    finally:
        db.close()


def answer(address, value="sim"):
    return [{"address": address, "answer": value}]


@pytest.mark.parametrize("root_first", [True, False])
def test_structured_answer_opens_only_its_gate_and_replays_after_db_reopen(tmp_path, root_first):
    path = tmp_path / "state.db"
    spec = collision(root_first)
    first = ["sub[a]:cp"] if root_first else ["a", "cp"]
    second = ["a", "cp"] if root_first else ["sub[a]:cp"]
    db = SessionDB(path)
    result = run(db, spec, answers=answer(first))
    assert result.status == "paused", result.outputs
    assert result.checkpoint["answer_address"] == second
    assert db._connection.execute("SELECT count(*) FROM workflow_node_cache").fetchone()[0] == 1
    db.close()
    db = SessionDB(path)
    try:
        result = run(db, spec, answers=answer(second))
        assert result.status == "complete"
        assert result.outputs == {"sub[a]:cp": "sim", "a": {"cp": "sim"}}
        assert result.cells_replayed == 1
        assert run(db, spec).cells_replayed == 2
    finally:
        db.close()


@pytest.mark.parametrize("root_first", [True, False])
@pytest.mark.parametrize("legacy_payload", [True, False])
def test_durable_pause_exposes_the_address_and_refuses_ambiguous_old_answers(
    tmp_path, root_first, legacy_payload,
):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    _install(tmp_path, CHILD)
    svc = _service(db, tmp_path, lambda _: pytest.fail("checkpoint spawned a leaf"))
    run_id = svc.start(collision(root_first), {})["run_id"]
    paused = svc.status(run_id, wait=True, timeout=10)
    first = paused["checkpoint"]["answer_address"]
    assert first == (["sub[a]:cp"] if root_first else ["a", "cp"])
    svc.shutdown()
    if legacy_payload:
        row = db.run_state_get(run_id)
        payload = json.loads(row["pause_payload_json"])
        del payload["checkpoint"]["answer_address"]
        db._connection.execute(
            "UPDATE workflow_run_state SET pause_payload_json=? WHERE run_id=?",
            (json.dumps(payload), run_id),
        )
        db._connection.commit()
    db.close()
    db = SessionDB(path)
    svc = _service(db, tmp_path, lambda _: pytest.fail("checkpoint spawned a leaf"))
    try:
        before = db.run_state_get(run_id)
        denied = svc.start(None, resume_run_id=run_id, checkpoint_answers={"sub[a]:cp": "sim"})
        assert "ambiguous" in denied["error"]
        assert '"address": ["sub[a]:cp"]' in denied["error"]
        assert '"address": ["a", "cp"]' in denied["error"]
        assert db.run_state_get(run_id) == before
        missing = svc.start(None, resume_run_id=run_id)
        if legacy_payload and root_first:
            # No template also describes a pre-#78 child question. With calls
            # present, even a root-id match must re-ask to establish ownership.
            assert "error" not in missing
            assert svc.status(run_id, wait=True, timeout=10)["checkpoint"]["answer_address"] == first
        else:
            assert json.dumps(first) in missing["error"]
        launched = svc.start(None, resume_run_id=run_id, checkpoint_answers=answer(first))
        assert "error" not in launched
        next_pause = svc.status(run_id, wait=True, timeout=10)
        assert next_pause["status"] == "paused"
        second = next_pause["checkpoint"]["answer_address"]
        assert second != first
        svc.start(None, resume_run_id=run_id, checkpoint_answers=answer(second))
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete" and done["cells_replayed"] >= 1
    finally:
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("root_id", ['["a","cp"]', 'checkpoint:/a/cp', 'a/~"\\:\n☃'])
def test_ids_that_look_like_protocol_and_json_stay_literal(tmp_path, root_id):
    db = SessionDB(":memory:")
    try:
        spec = {**CHILD, "nodes": [{**CHILD["nodes"][0], "id": root_id}]}
        assert run(db, spec, answers=answer([root_id])).outputs == {root_id: "sim"}
        assert run(db, spec).cells_replayed == 1
    finally:
        db.close()


def test_nested_punctuation_collision_uses_call_ids_without_loading_templates():
    spec = parent("a", "a]:x")
    db = SessionDB(":memory:")
    core = _core(db, lambda _: pytest.fail("no leaves"))
    try:
        engine = WorkflowEngine(
            core, budget=Budget(), checkpoint_answers={"sub[a]:x]:cp": "sim"},
            loader=lambda _: pytest.fail("address resolution must not load templates"),
        )
        with pytest.raises(ValueError, match="ambiguous"):
            engine.run(validate_spec(spec), {})
    finally:
        core.shutdown()
        db.close()


def test_two_punctuated_nested_addresses_need_independent_answers(tmp_path):
    child = {**CHILD, "nodes": [{**CHILD["nodes"][0], "id": "x]:cp"}]}
    spec = parent("a", "a]:x")
    spec["nodes"][1]["ref"] = "other"
    other = {**CHILD, "meta": {"name": "other", "version": 1}}
    _install(tmp_path, child)
    _install(tmp_path, other)
    db = SessionDB(":memory:")
    svc = _service(db, tmp_path, lambda _: pytest.fail("no leaves"))
    try:
        run_id = svc.start(spec, checkpoint_answers=answer(["a", "x]:cp"]))["run_id"]
        paused = svc.status(run_id, wait=True, timeout=10)
        assert paused["checkpoint"]["answer_address"] == ["a]:x", "cp"]
        svc.start(None, resume_run_id=run_id, checkpoint_answers=answer(["a]:x", "cp"]))
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()
        db.close()


def test_unattributable_old_question_is_reasked_without_assigning_its_default(tmp_path):
    child = {**CHILD, "nodes": [{"id": "x]:cp", "type": "checkpoint", "prompt": "First?"}]}
    spec = parent("a", "a]:x")
    _install(tmp_path, child)
    db = SessionDB(":memory:")
    store = RunStateStore(db)
    store.save(
        run_id="old", status="paused", pause_reason="checkpoint", spec=spec,
        checkpoint={"node_id": "sub[a]:x]:cp", "template": "child", "prompt": "Old?", "default": "sim"},
    )
    svc = _service(db, tmp_path, lambda _: pytest.fail("no leaves"))
    try:
        refused = svc.start(None, resume_run_id="old", checkpoint_answers=answer(["a", "x]:cp"]))
        assert "Resume without checkpoint_answers" in refused["error"]
        assert "error" not in svc.start(None, resume_run_id="old")
        paused = svc.status("old", wait=True, timeout=10)
        assert paused["checkpoint"]["answer_address"] == ["a", "x]:cp"]
        assert "default" not in paused["checkpoint"]
        assert paused["status"] == "paused"
        assert db._connection.execute("SELECT count(*) FROM workflow_node_cache").fetchone()[0] == 0
    finally:
        svc.shutdown()
        store.shutdown()
        db.close()


def test_preview_cannot_spread_one_answer_to_another_gate(tmp_path):
    db = SessionDB(":memory:")
    spec = collision(True)
    child = {**CHILD, "nodes": [*CHILD["nodes"], {"id": "leaf", "type": "agent", "prompt": "${cp}"}]}
    try:
        preview = preview_resume(
            db, "run", validate_spec(spec), {}, loader=lambda _: child,
            checkpoint_answers=answer(["sub[a]:cp"]),
        )
        assert {row["node_id"] for row in preview["unknown"]} == {"sub[a]:leaf"}
        with pytest.raises(ValueError, match="ambiguous"):
            preview_resume(db, "run", validate_spec(spec), checkpoint_answers={"sub[a]:cp": "sim"})
    finally:
        db.close()


@pytest.mark.parametrize("answers", [
    ["yes"], [{"address": [], "answer": "sim"}],
    [{"address": ["a", "b", "cp"], "answer": "sim"}],
    [{"address": [""], "answer": "sim"}], [{"address": [1], "answer": "sim"}],
    [{"address": ["cp"]}], [{"address": ["cp"], "answer": "sim", "extra": 1}],
    answer(["cp"]) * 2,
])
def test_bad_structured_answers_are_refused_before_launch(tmp_path, answers):
    db = SessionDB(":memory:")
    svc = _service(db, tmp_path, lambda _: pytest.fail("no leaves"))
    try:
        assert "error" in svc.start(CHILD, checkpoint_answers=answers)
        assert db._connection.execute("SELECT count(*) FROM workflow_run_state").fetchone()[0] == 0
    finally:
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("default", [False, True])
def test_unambiguous_legacy_pending_question_survives_upgrade(tmp_path, nested, default):
    child = {**CHILD, "nodes": [{"id": "cp", "type": "checkpoint", "prompt": "Proceed?"}]}
    spec = parent("a") if nested else child
    pending = {"node_id": "sub[a]:cp" if nested else "cp", "prompt": "Proceed?"}
    if nested:
        pending["template"] = "child"
    if default:
        pending["default"] = None  # null is an answer, not an absent value
    _install(tmp_path, child)
    path = tmp_path / "old.db"
    db = SessionDB(path)
    store = RunStateStore(db)
    store.save(run_id="old", status="paused", pause_reason="checkpoint", spec=spec, checkpoint=pending)
    store.shutdown()
    db.close()
    db = SessionDB(path)
    svc = _service(db, tmp_path, lambda _: pytest.fail("no leaves"))
    try:
        response = svc.start(
            None, resume_run_id="old",
            checkpoint_answers=None if default else {pending["node_id"]: None},
        )
        assert "error" not in response
        result = svc.status("old", wait=True, timeout=10)
        # Existing rollup semantics: null is cached but counts as null output.
        expected_status = "degraded" if nested else "failed"
        assert result["status"] == expected_status
        assert result["outputs"] == ({"a": {"cp": None}} if nested else {"cp": None})
        assert svc.start(None, resume_run_id="old")["cache_preview"]["replay"] == 1
        assert svc.status("old", wait=True, timeout=10)["status"] == expected_status
    finally:
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("value", ["abort", {"model": "looks-like-route"}, None, "", False])
def test_structured_answers_preserve_arbitrary_human_values(tmp_path, value):
    db = SessionDB(":memory:")
    child = {**CHILD, "nodes": [{"id": "cp", "type": "checkpoint", "prompt": "Value?"}]}
    try:
        assert run(db, child, answers=answer(["cp"], value)).outputs == {"cp": value}
        assert run(db, child).cells_replayed == 1
    finally:
        db.close()


def test_rejected_structured_answer_still_requires_a_human_retry(tmp_path):
    db = SessionDB(":memory:")
    child = {**CHILD, "nodes": [{**CHILD["nodes"][0], "on_reject": "pause"}]}
    _install(tmp_path, child)
    svc = _service(db, tmp_path, lambda _: pytest.fail("no leaves"))
    try:
        run_id = svc.start(parent("a"), checkpoint_answers=answer(["a", "cp"], "não"))["run_id"]
        paused = svc.status(run_id, wait=True, timeout=10)
        assert paused["status"] == "paused"
        assert paused["checkpoint"]["rejected"] == "'não'"
        assert paused["checkpoint"]["answer_address"] == ["a", "cp"]
        assert "error" in svc.start(None, resume_run_id=run_id)
        svc.start(None, resume_run_id=run_id, checkpoint_answers=answer(["a", "cp"]))
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()
        db.close()


def test_structured_checkpoint_channel_cannot_answer_a_route_fault():
    prior = DurableRun(
        run_id="r", status="paused", pause_reason=ROUTE_FAULT,
        route_fault={"node_id": "cp", "provider": "anthropic", "model": "dead"},
    )
    assert route_answer("r", answer(["cp"], "abort"), False, prior).error
    assert route_answer("r", {"cp": "abort"}, False, prior).abort_node == "cp"


def test_pre78_child_default_never_becomes_a_guarded_root_approval(tmp_path):
    """Reviewer P1: lack of template is NOT root-question provenance."""
    spec = parent("a")
    root = {**CHILD["nodes"][0], "prompt": "Approve ROOT?"}
    spec["nodes"][0]["depends_on"] = ["cp"]
    spec["nodes"].insert(0, root)
    _install(tmp_path, CHILD)
    path = tmp_path / "pre78.db"
    db = SessionDB(path)
    key = content_hash("parent", 1, "cp", "checkpoint", "Approve ROOT?")
    db.cache_put("old", key, "cp", '"sim"', "complete")
    store = RunStateStore(db)
    store.save(
        run_id="old", status="paused", pause_reason="checkpoint", spec=spec,
        checkpoint={"node_id": "cp", "prompt": "Old CHILD question?", "default": "sim"},
    )
    before = db.cache_get("old", key)
    assert before["node_scope_json"] is None
    store.shutdown()
    db.close()
    db = SessionDB(path)
    svc = _service(db, tmp_path, lambda _: pytest.fail("no leaves"))
    try:
        refused = svc.start(None, resume_run_id="old", checkpoint_answers=answer(["cp"]))
        assert "Resume without checkpoint_answers" in refused["error"]
        assert "error" not in svc.start(None, resume_run_id="old")
        paused = svc.status("old", wait=True, timeout=10)
        assert paused["status"] == "paused"
        assert paused["checkpoint"]["prompt"] == "Approve ROOT?"
        assert paused["checkpoint"]["answer_address"] == ["cp"]
        assert "structured answer_address" in paused["checkpoint"]["cache_compatibility"]
        assert "default" not in paused["checkpoint"]
        assert paused["outputs"] == {"cp": None}
        assert db.cache_get("old", key) == before  # no manufactured scope proof
        assert "error" not in svc.start(None, resume_run_id="old", checkpoint_answers=answer(["cp"]))
        child_pause = svc.status("old", wait=True, timeout=10)
        assert child_pause["checkpoint"]["answer_address"] == ["a", "cp"]
        assert child_pause["outputs"]["cp"] == "sim"
        assert db.cache_get("old", key)["node_scope_json"] == "[]"  # only fresh answer proves it
    finally:
        svc.shutdown()
        db.close()
