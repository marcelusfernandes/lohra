"""Issue #78 — a checkpoint inside a nested template is a DIFFERENT question.

H8: ``checkpoint_answers`` reaches the nested engine with the child's node ids
spelled exactly like the parent's, so a parent ``cp`` and a template's ``cp``
share one answer. With #74 turning ``checkpoint`` into an approval gate the
collision stopped being cosmetic: one "sim" opened BOTH gates, and because the
pause latch is first-wins the child's question was never even shown to the
human who supposedly approved it.

The experiment (RED) is the first two tests; everything below them is what the
fix has to keep true. The key a human answers a nested gate with is namespaced
the way ``fold_nested`` already namespaces a nested run's faults and costs:
``sub[<ref>]:<node id>``.
"""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import SUPPORTED_NODE_TYPES
from tests.test_workflow_operability import _service
from tests.test_workflow_pipeline import ScriptedClient

DEFAULT_MODEL = "claude-opus-4-8"


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, responder):
    def factory():
        return Agent(
            model=DEFAULT_MODEL,
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=4)


def _ok(_prompt):
    return "R"


def _install(home, spec):
    """Put a template where the service's loader will find it."""
    directory = home / "workflows" / "templates"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{spec['meta']['name']}.json").write_text(
        json.dumps(spec), encoding="utf-8"
    )


def _run(db, parent, child, answers):
    core = _core(db, _ok)
    try:
        return WorkflowEngine(
            core,
            budget=Budget(),
            loader={child["meta"]["name"]: child}.get,
            checkpoint_answers=answers,
        ).run(validate_spec(parent, supported_types=SUPPORTED_NODE_TYPES), {})
    finally:
        core.shutdown()


# The reviewer's repro, verbatim: same node id on both levels, both guarded.
_CHILD = {
    "meta": {"name": "child", "version": 1},
    "nodes": [
        {
            "id": "cp",
            "type": "checkpoint",
            "prompt": "CHILD: delete prod?",
            "accept": ["sim"],
        },
        {"id": "do", "type": "agent", "prompt": "Delete given ${cp}"},
    ],
}
_PARENT = {
    "meta": {"name": "parent", "version": 1},
    "nodes": [
        {
            "id": "cp",
            "type": "checkpoint",
            "prompt": "PARENT: ok to start?",
            "accept": ["sim"],
        },
        {"id": "sub", "type": "workflow", "ref": "child", "depends_on": ["cp"]},
    ],
}
_PLAIN_PARENT = {
    "meta": {"name": "plain-parent", "version": 1},
    "nodes": [{"id": "sub", "type": "workflow", "ref": "child"}],
}


# --- the experiment (RED) ----------------------------------------------------


def test_a_parent_answer_never_opens_a_nested_gate_of_the_same_id(db):
    """H8, half one: one "sim" must not approve two questions.

    The parent's gate opens on the answer it was given; the child's gate has
    been given nothing, so the run STOPS there and asks."""
    result = _run(db, _PARENT, _CHILD, {"cp": "sim"})

    assert result.status == "paused", (
        "one answer opened both gates — outputs=" f"{result.outputs!r}"
    )
    assert result.outputs["cp"] == "sim"  # ...the PARENT's gate, and only it
    assert result.checkpoint == {
        "node_id": "sub[child]:cp",
        "prompt": "CHILD: delete prod?",
        "template": "child",
    }


def test_a_nested_pause_names_the_template_it_is_asking_from(db, tmp_path):
    """H8, half two: the payload of a child-only pause has to say WHERE the
    question lives — a bare ``cp`` points at nothing in the spec the resume
    replays, and is the key that answers the parent's gate."""
    _install(tmp_path, _CHILD)
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_PLAIN_PARENT, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused" and out["reason"] == CHECKPOINT
        assert out["checkpoint"] == {
            "node_id": "sub[child]:cp",
            "prompt": "CHILD: delete prod?",
            "template": "child",
        }
    finally:
        svc.shutdown()


# --- what the namespaced key buys -------------------------------------------


def test_the_namespaced_answer_opens_the_child_and_nothing_else(db, tmp_path):
    """The whole road, end to end: the pause names a key, that key answers it,
    and the resume's preview knows the cell it just made computable."""
    _install(tmp_path, _CHILD)
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_PLAIN_PARENT, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"

        launched = svc.start(
            None, {}, resume_run_id=run_id,
            checkpoint_answers={"sub[child]:cp": "sim"},
        )
        assert "error" not in launched
        # The preview reads the answer under the SAME key the engine will, so
        # the nested gate is a known cell rather than an unknowable root.
        unknown = launched["cache_preview"].get("unknown", [])
        assert not [u for u in unknown if "sub[child]" in str(u.get("node_id"))]

        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"]["sub"] == {"cp": "sim", "do": "R"}
    finally:
        svc.shutdown()


def test_a_bare_answer_leaves_the_child_paused_and_says_which_key(db, tmp_path):
    """The other direction: the parent's spelling never reaches a nested gate.

    Today's refusal is the one this keeps — an answer that matches no pending
    checkpoint is a resume that would re-pause on the same node, and the launch
    says so didactically instead of pretending it did something."""
    _install(tmp_path, _CHILD)
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_PLAIN_PARENT, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"

        out = svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "sim"})

        assert "sub[child]:cp" in out["error"]
        assert "CHILD: delete prod?" in out["error"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
    finally:
        svc.shutdown()


def test_the_accept_guard_bites_through_the_namespaced_key(db):
    """#74's guard is unchanged by the namespacing — a "não" under the nested
    key still rejects, still faults under the nested namespace, and a
    ``required`` nested gate still aborts the parent."""
    child = {
        "meta": {"name": "child", "version": 1},
        "nodes": [
            {
                "id": "cp",
                "type": "checkpoint",
                "prompt": "CHILD: delete prod?",
                "accept": ["sim"],
                "required": True,
            }
        ],
    }
    result = _run(db, _PLAIN_PARENT, child, {"sub[child]:cp": "não"})

    assert result.status == "failed"
    assert result.required_failure == "sub[child]:cp"
    assert "sub[child]: cp: checkpoint rejected by human: 'não'" in result.faults


def test_a_cached_parent_answer_never_replays_as_the_childs(db, tmp_path):
    """The cell namespace, pinned: same node id AND same question on both
    levels, so only ``spec_identity`` (the child's ``meta.name``/``version``)
    separates the two cache cells. The parent's answer is cached; the child's
    gate must still stop the run."""
    twin_child = {
        "meta": {"name": "twin", "version": 1},
        "nodes": [{"id": "cp", "type": "checkpoint", "prompt": "Proceed?", "accept": ["sim"]}],
    }
    twin_parent = {
        "meta": {"name": "twin-parent", "version": 1},
        "nodes": [
            {"id": "cp", "type": "checkpoint", "prompt": "Proceed?", "accept": ["sim"]},
            {"id": "sub", "type": "workflow", "ref": "twin", "depends_on": ["cp"]},
        ],
    }
    _install(tmp_path, twin_child)
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(twin_parent, {}, checkpoint_answers={"cp": "sim"})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert out["checkpoint"]["node_id"] == "sub[twin]:cp"
        assert out["outputs"]["cp"] == "sim"
    finally:
        svc.shutdown()


def test_a_nested_default_still_answers_an_unattended_resume(db, tmp_path):
    """A ``default`` is filled in from the PAYLOAD's node_id (WF-10), so it has
    to keep working through the namespaced spelling."""
    child = {
        "meta": {"name": "child", "version": 1},
        "nodes": [{"id": "cp", "type": "checkpoint", "prompt": "Proceed?", "default": "yes"}],
    }
    _install(tmp_path, child)
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_PLAIN_PARENT, {})["run_id"]
        paused = svc.status(run_id, wait=True, timeout=10)
        assert paused["checkpoint"]["node_id"] == "sub[child]:cp"
        assert paused["checkpoint"]["default"] == "yes"

        svc.start(None, {}, resume_run_id=run_id)  # no answers at all

        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"]["sub"] == {"cp": "yes"}
    finally:
        svc.shutdown()
