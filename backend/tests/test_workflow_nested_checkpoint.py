"""Issue #78 — a checkpoint inside a nested template is a DIFFERENT question.

H8: ``checkpoint_answers`` reaches the nested engine with the child's node ids
spelled exactly like the parent's, so a parent ``cp`` and a template's ``cp``
share one answer. With #74 turning ``checkpoint`` into an approval gate the
collision stopped being cosmetic: one "sim" opened BOTH gates, and because the
pause latch is first-wins the child's question was never even shown to the
human who supposedly approved it.

The experiment (RED) is the first two tests; everything below them is what the
fix has to keep true. The key a human answers a nested gate with is namespaced
by the parent's ``workflow`` NODE — ``sub[<workflow node id>]:<node id>`` — the
one identity the validator keeps unique inside a spec. The adversarial review of
the first cut caught why it cannot be the template ref: two `workflow` nodes
running the SAME template with different args are two different questions, and
one ref-keyed answer opened both.

Note the shape it shares with, and the content it does NOT share with, the
namespace ``fold_nested`` gives a nested run's faults, costs, route faults and
`required` failures: those stay keyed by the TEMPLATE (``sub[<ref>]:…``), which
is what a reader of a rollup needs. An answer needs the opposite — the CALLER,
not the callee. ``template`` rides in the pause payload so the two meet.
"""

import ast
import json
import pathlib
import re

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
        "node_id": "sub[sub]:cp",
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
            "node_id": "sub[sub]:cp",
            "prompt": "CHILD: delete prod?",
            "template": "child",
        }
    finally:
        svc.shutdown()


# --- the adversarial review's repro (HIGH-1) ---------------------------------


_TWO_CALLS = {
    "meta": {"name": "two-calls", "version": 1},
    "nodes": [
        {"id": "a", "type": "workflow", "ref": "danger", "args": {"target": "staging"}},
        {
            "id": "b",
            "type": "workflow",
            "ref": "danger",
            "args": {"target": "PROD"},
            "depends_on": ["a"],
        },
    ],
}
_DANGER = {
    "meta": {"name": "danger", "version": 1},
    "nodes": [
        {
            "id": "cp",
            "type": "checkpoint",
            "prompt": "delete ${args.target}?",
            "accept": ["sim"],
        }
    ],
}


def test_two_calls_on_one_template_ask_their_own_questions(db, tmp_path):
    """Why the key is the ``workflow`` NODE and not the template ref.

    Two nodes run the SAME template with different args — "delete staging?" and
    "delete PROD?". They are two questions and a human has to answer both. Keyed
    by the ref they would share one key, so approving staging would approve PROD
    silently; keyed by the node they cannot, because the validator keeps node ids
    unique inside a spec."""
    _install(tmp_path, _DANGER)
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_CALLS, {})["run_id"]
        first = svc.status(run_id, wait=True, timeout=10)["checkpoint"]
        assert first["prompt"] == "delete staging?"

        # Answered with the key the pause ITSELF named — so this half of the
        # test is about the sharing, not about the spelling. Approving staging
        # must move the run ON to the second question, never answer it.
        svc.start(
            None, {}, resume_run_id=run_id,
            checkpoint_answers={first["node_id"]: "sim"},
        )
        second = svc.status(run_id, wait=True, timeout=10)
        assert second["status"] == "paused", (
            "approving staging also approved PROD — outputs="
            f"{second.get('outputs')!r}"
        )
        assert second["checkpoint"]["prompt"] == "delete PROD?"

        # ...and only now the spelling: the key is per CALL, not per template.
        assert first == {
            "node_id": "sub[a]:cp", "prompt": "delete staging?", "template": "danger",
        }
        assert second["checkpoint"] == {
            "node_id": "sub[b]:cp", "prompt": "delete PROD?", "template": "danger",
        }

        svc.start(
            None, {}, resume_run_id=run_id,
            checkpoint_answers={"sub[a]:cp": "sim", "sub[b]:cp": "sim"},
        )
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"] == {"a": {"cp": "sim"}, "b": {"cp": "sim"}}
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
            checkpoint_answers={"sub[sub]:cp": "sim"},
        )
        assert "error" not in launched
        # The preview reads the answer under the SAME key the engine will, so
        # the nested gate is a known cell rather than an unknowable root.
        unknown = launched["cache_preview"].get("unknown", [])
        assert not [u for u in unknown if "sub[sub]" in str(u.get("node_id"))]

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

        assert "sub[sub]:cp" in out["error"]
        assert "CHILD: delete prod?" in out["error"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"

        # ...and the OTHER half of the same rule, pinned because it is what
        # makes the refusal readable: the launch refuses on the PENDING key
        # being absent, never on an unmatched key being present. An extra key
        # rides along unread — the behaviour before this slice and after it.
        ok = svc.start(
            None, {}, resume_run_id=run_id,
            checkpoint_answers={"sub[sub]:cp": "sim", "cp": "sim", "bogus": "x"},
        )
        assert "error" not in ok
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
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
    result = _run(db, _PLAIN_PARENT, child, {"sub[sub]:cp": "não"})

    assert result.status == "failed"
    # The two namespaces, side by side and deliberately different: the ANSWER is
    # keyed by the caller (`sub` is the parent's `workflow` node), while what the
    # ROLLUP reports is keyed by the callee (`child` is the template) — a reader
    # of a fault wants to know which template misbehaved, a human answering wants
    # to know which call is asking.
    assert result.required_failure == "sub[child]:cp"
    assert "sub[child]: cp: checkpoint rejected by human: 'não'" in result.faults


def test_a_cached_parent_answer_never_replays_as_the_childs(db, tmp_path):
    """The cell namespace, pinned: same node id AND same question on both
    levels, so only ``spec_identity`` separates the two cache cells. The
    parent's answer is cached; the child's gate must still stop the run.

    Note the precondition this depends on and does NOT prove: the two specs
    carry DIFFERENT ``(meta.name, meta.version)`` — ``twin-parent``/1 against
    ``twin``/1. The next test is what happens when they do not."""
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
        assert out["checkpoint"]["node_id"] == "sub[sub]:cp"
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
        assert paused["checkpoint"]["node_id"] == "sub[sub]:cp"
        assert paused["checkpoint"]["default"] == "yes"

        svc.start(None, {}, resume_run_id=run_id)  # no answers at all

        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"]["sub"] == {"cp": "yes"}
    finally:
        svc.shutdown()


def test_a_template_sharing_the_parents_spec_identity_replays_its_cell(db, tmp_path):
    """A KNOWN LIMITATION, pinned so it is a decision and not a surprise.

    The answer KEY is namespaced (#78), but the cache CELL is namespaced by
    ``spec_identity`` — ``(meta.name, meta.version)`` — and nothing forces a
    template's identity to differ from its caller's. When it does not, and the
    two gates ask a byte-identical question under the same node id, the cell
    hashes are equal: the nested gate HITS the parent's cached answer and
    returns it before ``run_checkpoint`` ever looks at the key. One answer still
    settles both gates.

    Not fixed here: the remedy is to namespace the cell too (or refuse the
    identity collision at validation), which moves every cached cell of every
    nested run and is a slice of its own. It needs an author to write a template
    whose ``meta`` duplicates the spec that calls it, which the harness has no
    reason to produce and the library's ``save`` does not encourage. Recorded in
    the issue's follow-up rather than papered over."""
    twin = {
        "meta": {"name": "recur", "version": 1},
        "nodes": [{"id": "cp", "type": "checkpoint", "prompt": "Proceed?"}],
    }
    caller = {
        "meta": {"name": "recur", "version": 1},  # ...the SAME identity
        "nodes": [
            {"id": "cp", "type": "checkpoint", "prompt": "Proceed?"},
            {"id": "sub", "type": "workflow", "ref": "recur", "depends_on": ["cp"]},
        ],
    }
    _install(tmp_path, twin)
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(caller, {}, checkpoint_answers={"cp": "sim"})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        # What SHOULD happen is a pause at `sub[sub]:cp`. What happens is the
        # cell hit — asserted so a future fix breaks this test loudly.
        assert out["status"] == "complete"
        assert out["outputs"] == {"cp": "sim", "sub": {"cp": "sim"}}
    finally:
        svc.shutdown()


# --- one spelling, machine-checked -------------------------------------------


def _builds_the_prefix(source: str) -> bool:
    """Does this module BUILD a ``sub[...]`` string, in any form?

    Parsed, not grepped, so a comment or a docstring quoting the shape is not a
    finding — only a real string literal. One predicate over every literal the
    module holds (plain strings and the literal parts of f-strings alike), and
    it catches every way Python composes the prefix: the literal ENDS at
    ``sub[`` because something is interpolated or concatenated after it
    (``f"sub[{ref}]:"``, ``"sub[" + ref``), or it carries a ``{}``/``%s``
    placeholder inside the brackets (``"sub[{}]:".format(ref)``).

    Deliberately not "contains ``sub[``": `RUN_GUIDANCE` is one enormous
    implicitly-concatenated f-string that DOCUMENTS the shape to an agent, and
    documentation is not drift. Non-vacuity is asserted by the caller."""
    def flags(value: str) -> bool:
        return value.rstrip().endswith("sub[") or bool(
            re.search(r"sub\[[^\]]*[{%]", value)
        )

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.JoinedStr):
            values = [
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            values = [node.value]
        else:
            continue
        if any(flags(value) for value in values):
            return True
    return False


def test_the_nested_prefix_is_built_in_exactly_one_place():
    """The helper exists so the two namespaces cannot drift apart by one
    character (#78) — a second inline builder anywhere would make that drift
    possible again, so the whole package source is checked, not just asked.

    Recursive over ``lohra/``, not just ``lohra/workflow/``: the prefix is what
    a rollup, a CLI and a tool reply all print, so a second builder is as likely
    to appear outside the harness as inside it."""
    import lohra as package

    root = pathlib.Path(package.__file__).parent
    builders = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if _builds_the_prefix(path.read_text(encoding="utf-8"))
    )
    assert builders == ["workflow/namespacing.py"], builders
    # ...and the check is not vacuous: each shape it claims to catch, caught.
    assert _builds_the_prefix('x = f"sub[{ref}]:{node}"')
    assert _builds_the_prefix('x = "sub[{}]:".format(ref)')
    assert _builds_the_prefix('x = "sub[%s]:" % ref')
    assert _builds_the_prefix('x = "sub[" + ref + "]:"')
    # ...while prose that merely QUOTES the shape is not a finding.
    assert not _builds_the_prefix('GUIDE = "answer under sub[<ref>]:<id>"')
    assert not _builds_the_prefix('def f():\n    """namespaced sub[ref]:node."""')
