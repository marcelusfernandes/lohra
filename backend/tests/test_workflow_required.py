"""`required: true` really stops a run (issue #15, decision of the owner).

The field has been accepted by the validator since the harness existed and read
by NOTHING: a node marked `required` that resolved to `null` was tolerated
exactly like an optional one, the run kept scheduling, and the verdict came back
`degraded`. Spec 07 §7.4 promised the opposite ("the run fails loudly"), and
the builtin skill admitted the gap in writing — a schema that suggests an
operational guarantee the runtime does not apply.

What is pinned here:

- a `required` node that resolves to null ABORTS the run: nothing after it runs,
  every node that did not run gets a fault that names WHY, and the verdict is
  `failed` (never `degraded`, which reads as "partial results you can use");
- the default (`required: false`) is byte-for-byte the permissive behaviour of
  before — this is opt-in, and the dogfood specs that rely on a run surviving a
  dead node keep working;
- `required` is NOT part of a cell's identity: flipping it must not re-bill a
  resume for cells that already completed;
- a stop that is NOT a failure (quota/budget/checkpoint pause) is never
  mislabelled as a required failure — the run is paused and will come back;
- and the two shapes `required` deliberately does NOT reach today (a `parallel`
  with a dead branch, a `workflow` node whose child degraded) are pinned as the
  gaps they are, so a later change to either is a decision, not a surprise.
"""

import pytest

from lohra.workflow import library
from lohra.workflow.accounting import RunResult, derive_status
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.progress import SKIPPED
from lohra.workflow.required import skip_fault
from lohra.workflow.schema import ValidationError, validate_spec
from tests.test_workflow_dogfood_fixes import _counting_core, _run_then_resume, db  # noqa: F401
from tests.test_workflow_quota import _core, _quota_responder


def _faults(result):
    return "\n".join(result.faults)


def _engine(core, **kwargs):
    return WorkflowEngine(core, budget=Budget(), **kwargs)


def _silent_on(name):
    """A responder that answers nothing for one node — the WF-7 null path, with
    no sleeps and no injected exceptions."""

    def responder(prompt):
        return "" if name in prompt else "R"

    return responder


# `d` is INDEPENDENT and, by Kahn + spec order, scheduled right after `a`:
# the discriminator between "skipped because its upstream died" and "skipped
# because the run was aborted".
_REQUIRED_DAG = {
    "meta": {"name": "req", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "alpha", "required": True},
        {"id": "b", "type": "agent", "prompt": "beta ${a}"},
        {"id": "c", "type": "agent", "prompt": "gamma", "depends_on": ["a"]},
        {"id": "d", "type": "agent", "prompt": "delta"},
    ],
}
_OPTIONAL_DAG = {
    "meta": {"name": "req", "version": 1},
    "nodes": [{**node, **({} if node["id"] != "a" else {"required": False})}
              for node in _REQUIRED_DAG["nodes"]],
}


def _run(db, spec_dict, responder, **kwargs):  # noqa: F811
    core = _core(db, responder)
    try:
        return _engine(core, **kwargs).run(validate_spec(spec_dict), {})
    finally:
        core.shutdown()


# --- the semantics -----------------------------------------------------------


def test_a_required_null_aborts_the_run_and_fails_it(db):  # noqa: F811
    result = _run(db, _REQUIRED_DAG, _silent_on("alpha"))
    assert result.outputs["a"] is None
    # Nothing after it ran — not even the independent node.
    assert set(result.outputs) == {"a"}
    assert result.status == "failed"  # not "degraded": there is nothing to use
    assert "a: required node resolved to null" in _faults(result)
    assert result.required_failure == "a"


def test_a_skipped_node_says_whether_it_depended_on_the_failure(db):  # noqa: F811
    result = _run(db, _REQUIRED_DAG, _silent_on("alpha"))
    assert "b: skipped: required upstream 'a' failed" in _faults(result)
    assert "c: skipped: required upstream 'a' failed" in _faults(result)
    # `d` never depended on `a` — claiming it did would send the author hunting
    # for an edge that does not exist.
    assert "d: skipped: run aborted by failed required node 'a'" in _faults(result)


def test_a_transitive_dependent_is_named_as_one():
    spec = validate_spec(
        {
            "meta": {"name": "chain"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "one", "required": True},
                {"id": "b", "type": "agent", "prompt": "two ${a}"},
                {"id": "c", "type": "agent", "prompt": "three ${b}"},
            ],
        }
    )
    assert skip_fault(spec, "a", spec.node("c")) == "c: skipped: required upstream 'a' failed"


def test_the_default_stays_permissive(db):  # noqa: F811
    """The whole backward-compat promise in one test: no `required`, no change."""
    result = _run(db, _OPTIONAL_DAG, _silent_on("alpha"))
    assert result.outputs["a"] is None
    assert result.outputs["c"] == "R" and result.outputs["d"] == "R"
    assert result.status == "degraded"
    assert result.required_failure is None
    assert "skipped" not in _faults(result)


def test_a_spec_that_never_mentions_required_is_unchanged(db):  # noqa: F811
    """The stronger half of the promise: the field ABSENT, not set to false.
    Every spec authored before issue #15 is this shape."""
    absent = {
        **_REQUIRED_DAG,
        "nodes": [{k: v for k, v in node.items() if k != "required"}
                  for node in _REQUIRED_DAG["nodes"]],
    }
    result = _run(db, absent, _silent_on("alpha"))
    assert result.outputs["a"] is None
    assert result.outputs["b"] is None  # an upstream null still fails ITS node closed
    assert result.outputs["c"] == "R" and result.outputs["d"] == "R"
    assert result.status == "degraded"
    assert result.required_failure is None


def test_a_required_node_that_succeeds_changes_nothing(db):  # noqa: F811
    result = _run(db, _REQUIRED_DAG, lambda prompt: "R")
    assert result.status == "complete"
    assert result.required_failure is None
    assert set(result.outputs) == {"a", "b", "c", "d"}


def test_derive_status_puts_a_required_failure_above_a_partial_run():
    result = RunResult(nodes_total=4, null_count=1, faults=["a: required node resolved to null"])
    assert derive_status(result) == "degraded"
    assert derive_status(RunResult(**{**result.__dict__, "required_failure": "a"})) == "failed"


# --- what a required failure must NOT be confused with -----------------------


def test_a_quota_pause_on_a_required_node_is_not_a_required_failure(db):  # noqa: F811
    """A pause is resumable; a required failure is terminal. Reading a 429 as
    "the required node failed" would tell the author to fix a spec that is fine
    and would stop the auto-resume from ever coming back."""
    spec = {
        "meta": {"name": "q"},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "go", "required": True},
            {"id": "b", "type": "agent", "prompt": "then ${a}"},
        ],
    }
    result = _run(db, spec, _quota_responder)
    assert result.status == "paused"
    assert result.required_failure is None
    assert "skipped" not in _faults(result)


def test_a_cancelled_run_is_not_relabelled_a_required_failure(db):  # noqa: F811
    core = _core(db, _silent_on("alpha"))
    engine = _engine(core)
    try:
        engine.request_cancel()
        result = engine.run(validate_spec(_REQUIRED_DAG), {})
        assert result.status == "cancelled"
        assert result.required_failure is None
    finally:
        core.shutdown()


# --- identity, resume and the library ----------------------------------------


def test_required_is_not_part_of_the_cell_identity(db):  # noqa: F811
    """`required` says what to do about a null; it does not change what the leaf
    is asked. A resume that flips it must REPLAY, never re-bill."""
    hashes: dict[str, list[str]] = {}

    def collect(spec_dict, key):
        core = _core(db, lambda prompt: "R")
        engine = _engine(core)
        seen: list[str] = []
        original = engine.cell_hash
        engine.cell_hash = lambda *parts: seen.append(original(*parts)) or seen[-1]
        try:
            engine.run(validate_spec(spec_dict), {})
        finally:
            core.shutdown()
        hashes[key] = seen

    collect(_REQUIRED_DAG, "required")
    collect(_OPTIONAL_DAG, "optional")
    assert hashes["required"] == hashes["optional"]


def test_a_resume_of_a_run_failed_by_required_respawns_only_what_is_missing(db):  # noqa: F811
    """The completed cells are still cells: an abort is not a reason to re-pay."""
    replies = iter(["", "", "LATE"])  # a nulls twice (WF-7 retry), then answers
    spec = {
        "meta": {"name": "resume-req", "version": 1},
        "nodes": [
            {"id": "first", "type": "agent", "prompt": "one"},
            {"id": "gatekeeper", "type": "agent", "prompt": "two", "required": True},
            {"id": "last", "type": "agent", "prompt": "three ${gatekeeper}"},
        ],
    }

    def responder(prompt):
        return next(replies, "R") if "two" in prompt else "R"

    r1, after_first, r2, after_second = _run_then_resume(db, spec, responder, "run-required")
    assert r1.status == "failed" and r1.required_failure == "gatekeeper"
    assert "last" not in r1.outputs
    assert r2.status == "complete"
    assert r2.outputs["last"] == "R"
    # `first` replayed from the cache; only the gate and its dependent ran again.
    assert after_second - after_first == 2


def test_a_run_failed_by_required_is_never_certified_as_a_template(db, tmp_path):  # noqa: F811
    result = RunResult(nodes_total=2, null_count=1, required_failure="a", status="failed")
    library.record_outcome(str(tmp_path), _REQUIRED_DAG, result, tokens_total=10, faults_total=[])
    assert not (tmp_path / "workflows" / "templates").exists()


# --- observability -----------------------------------------------------------


def test_a_skipped_node_is_reported_as_skipped_not_as_a_null(db):  # noqa: F811
    events: list[tuple[str, dict]] = []
    result = _run(
        db, _REQUIRED_DAG, _silent_on("alpha"),
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    states = {p["node_id"]: p["state"] for kind, p in events if kind == "node"}
    assert states["a"] == "null"
    assert states["b"] == states["c"] == states["d"] == SKIPPED
    # A skipped node never ran, so it is not a leaf that died: null_rate must
    # keep meaning "how much of what RAN came back empty".
    assert result.null_count == 1


def test_the_progress_snapshot_settles_a_skipped_node(db):  # noqa: F811
    core = _core(db, _silent_on("alpha"))
    engine = _engine(core)
    try:
        engine.run(validate_spec(_REQUIRED_DAG), {})
        snapshot = engine.progress_snapshot()
    finally:
        core.shutdown()
    states = {node["id"]: node["state"] for node in snapshot["nodes"]}
    assert states == {"a": "null", "d": SKIPPED, "b": SKIPPED, "c": SKIPPED}
    assert snapshot["done"] == 4 and snapshot["pending"] == 0


# --- the validator -----------------------------------------------------------


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, None, []])
def test_a_non_boolean_required_is_refused_at_author_time(value):
    spec = validate_spec(
        {
            "meta": {"name": "bad"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "go", "required": value}],
        }
    )
    assert isinstance(spec, ValidationError)
    assert "required" in spec.message


def test_a_boolean_required_is_accepted():
    for value in (True, False):
        spec = validate_spec(
            {
                "meta": {"name": "ok"},
                "nodes": [{"id": "a", "type": "agent", "prompt": "go", "required": value}],
            }
        )
        assert not isinstance(spec, ValidationError)
        assert spec.nodes[0].required is value


# --- the two shapes `required` does NOT reach (pinned as gaps) ---------------


def test_a_parallel_with_a_dead_branch_is_not_a_required_failure(db):  # noqa: F811
    """A fan-out resolves to a LIST (with nulls in it), never to null — so
    `required` cannot see a partially-dead fan-out. A per-branch failure marker
    was once considered for this (`min_success_ratio`) but removed unimplemented
    (issue #15: the engine never enforced it and the spec left its semantics
    ambiguous — spec 07 §7.4). The documented way to close this gap is a `gate`
    or `completeness_check` node, marked `required`, that reads the fan-out
    result itself; this test pins that the gap still exists without one."""
    spec = {
        "meta": {"name": "fan"},
        "nodes": [
            {
                "id": "p",
                "type": "parallel",
                "required": True,
                "branches": [
                    {"prompt": "alpha"},
                    {"prompt": "beta"},
                ],
            },
            {"id": "after", "type": "agent", "prompt": "done"},
        ],
    }
    result = _run(db, spec, _silent_on("alpha"))
    assert result.outputs["p"] == ["", "R"]  # a list with a dead branch in it
    assert result.required_failure is None
    assert result.outputs["after"] == "R"  # the run kept going
    assert result.status == "complete"  # and nothing even reads as degraded


def test_a_nested_workflow_whose_child_degraded_is_not_a_required_failure(db, tmp_path):  # noqa: F811
    """A `workflow` node returns the child's OUTPUTS dict — never null — so a
    child that merely degraded cannot trip the parent's `required`. A child
    whose own `required` node failed is a different matter (below)."""
    child = {
        "meta": {"name": "child", "version": 1},
        "nodes": [{"id": "inner", "type": "agent", "prompt": "alpha"}],
    }
    parent = {
        "meta": {"name": "parent", "version": 1},
        "nodes": [
            {"id": "sub", "type": "workflow", "ref": "child", "required": True},
            {"id": "after", "type": "agent", "prompt": "done"},
        ],
    }
    core = _core(db, _silent_on("alpha"))
    try:
        engine = _engine(core, loader=lambda ref: child if ref == "child" else None)
        result = engine.run(validate_spec(parent), {})
    finally:
        core.shutdown()
    assert result.outputs["sub"] == {"inner": None}  # a dict, not null
    assert result.required_failure is None
    assert result.outputs["after"] == "R"


def test_a_required_failure_inside_a_nested_workflow_aborts_the_parent(db, tmp_path):  # noqa: F811
    """The nested engine folds its metrics into the parent's rollup so nested
    failures stay visible; a nested REQUIRED failure has to travel the same
    road, or `required` would be silently unenforceable one level down."""
    child = {
        "meta": {"name": "child", "version": 1},
        "nodes": [{"id": "inner", "type": "agent", "prompt": "alpha", "required": True}],
    }
    parent = {
        "meta": {"name": "parent", "version": 1},
        "nodes": [
            {"id": "sub", "type": "workflow", "ref": "child"},
            {"id": "after", "type": "agent", "prompt": "done"},
        ],
    }
    core = _core(db, _silent_on("alpha"))
    try:
        engine = _engine(core, loader=lambda ref: child if ref == "child" else None)
        result = engine.run(validate_spec(parent), {})
    finally:
        core.shutdown()
    assert result.status == "failed"
    assert result.required_failure == "sub[child]:inner"
    assert "after" not in result.outputs
    assert "after: skipped" in _faults(result)


def test_the_engine_still_caches_a_run_that_a_required_node_aborted(db):  # noqa: F811
    """An abort must not throw away the cells that DID complete — that is what
    makes the resume above cost two leaves instead of three."""
    counter = [0]
    core = _counting_core(db, _silent_on("beta"), counter)
    spec = {
        "meta": {"name": "cached", "version": 1},
        "nodes": [
            {"id": "one", "type": "agent", "prompt": "alpha"},
            {"id": "two", "type": "agent", "prompt": "beta", "required": True},
        ],
    }
    try:
        engine = _engine(core, cache=NodeCache(db, "run-cached-abort"))
        result = engine.run(validate_spec(spec), {})
    finally:
        core.shutdown()
    assert result.status == "failed"
    rows = db._connection.execute(
        "SELECT node_id FROM workflow_node_cache WHERE run_id = ?", ("run-cached-abort",)
    ).fetchall()
    assert [row["node_id"] for row in rows] == ["one"]


# --- anti-drift: the skill may no longer call `required` a no-op -------------


def test_the_builtin_skill_documents_required_as_enforced():
    """The skill is what the agent reads before authoring. While `required` was
    inert it said so in writing; now that it aborts a run, the old sentence
    would be worse than no sentence at all."""
    from pathlib import Path

    from lohra.skills.store import builtin_root

    body = (Path(builtin_root()) / "workflow-authoring" / "SKILL.md").read_text(encoding="utf-8")
    assert "`required` (any node):" in body
    assert "the run **stops there**" in body
    # ...and no field is sold as accepted-and-ignored any more (issue #73): `label`
    # and `phase` were REMOVED, `budget` became a real per-node ceiling. An author
    # who reads "still validate but do nothing" would author a field that now fails.
    assert "still validate but do nothing" not in body
    changed = body[body.index("`label` and `phase` were REMOVED"):]
    assert "the validator refuses them" in changed
    assert "per-node token ceiling" in changed
    assert "`required` (on any node)" not in body  # the old admission is gone


def test_the_builtin_skill_no_longer_admits_min_success_ratio_is_inert():
    """issue #15: the field was REMOVED (author gets a didactic validation
    error), not merely left inert — the skill must not keep telling agents it
    is accepted-and-ignored, or an author who reads it will still author it."""
    from pathlib import Path

    from lohra.skills.store import builtin_root

    body = (Path(builtin_root()) / "workflow-authoring" / "SKILL.md").read_text(encoding="utf-8")
    assert "min_success_ratio" not in body


# --- the ledger must carry a skipped node HONESTLY --------------------------


def test_the_audit_vocabulary_knows_the_skipped_state():
    """`_safe_metadata` allow-lists the `state` values a marker may carry. A
    value outside it is replaced by `excluded_by_policy` — which does not just
    lose the word: it counts as a REDACTION in the ledger's field markers, so a
    skipped node would read as content the audit refused to keep. The engine
    comment promises the opposite (the event rides on `node.failed` precisely so
    the real state survives in the data)."""
    from lohra.workflow.audit import _safe_metadata

    assert _safe_metadata({"state": SKIPPED}) == {"state": SKIPPED}


def test_every_progress_state_is_a_word_the_audit_can_keep():
    """Anti-drift: the two vocabularies are written in different modules and the
    node lifecycle event carries one into the other. A new progress state that
    the audit does not know becomes a silent gap in the ledger."""
    from lohra.workflow import progress
    from lohra.workflow.audit import _SAFE_STRING_VALUES

    vocabulary = _SAFE_STRING_VALUES["state"]
    states = {progress.PENDING, progress.RUNNING, progress.COMPLETE,
              progress.NULL, progress.SKIPPED}
    assert states <= vocabulary
    assert set(progress._SETTLED) <= vocabulary


def test_the_rollup_publishes_which_node_ended_the_run(db):  # noqa: F811
    """`RunResult.required_failure` was only ever readable as prose inside a
    fault. The rollup is what the agent and the library actually read."""
    from lohra.workflow.rollup import summarize

    result = _run(db, _REQUIRED_DAG, _silent_on("alpha"))
    assert summarize("r", result.status, result)["required_failure"] == "a"
    clean = _run(db, _REQUIRED_DAG, lambda prompt: "R")
    assert "required_failure" not in summarize("r", clean.status, clean)


def test_a_checkpoint_the_human_rejected_is_not_a_required_failure(db):  # noqa: F811
    """The third shape `required` cannot see, pinned because the skill now
    teaches it: a human answering "no" produces a normal (cached) output, not a
    null. Judging the ANSWER is a `gate`'s job."""
    spec = validate_spec(
        {
            "meta": {"name": "cp"},
            "nodes": [
                {"id": "c", "type": "checkpoint", "prompt": "ship it?", "required": True},
                {"id": "after", "type": "agent", "prompt": "go"},
            ],
        }
    )
    core = _core(db, lambda prompt: "R")
    try:
        engine = _engine(core, checkpoint_answers={"c": "no, rejected"})
        result = engine.run(spec, {})
    finally:
        core.shutdown()
    assert result.outputs["c"] == "no, rejected"  # an output, not a null
    assert result.required_failure is None
    assert result.outputs["after"] == "R" and result.status == "complete"
