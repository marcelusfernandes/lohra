"""Issue #74 — a human gate that can say NO, and a completeness check that counts.

Two holes in the rigor nodes, both of the same shape: the harness accepted an
answer without ever reading it.

- ``checkpoint`` cached whatever the human typed and let it flow downstream as
  the node's output. Answering "não, cancele" therefore APPROVED the run and
  spawned the dependent leaf with the refusal interpolated into its prompt.
  Now a node may declare ``accept`` (the answers that release it) and
  ``on_reject`` (``fail`` — the default, fail-closed — or ``pause`` to ask
  again).
- ``completeness_check`` answered the fixed ``{complete, missing}`` and nobody
  looked at ``complete``. A node marked ``required: true`` now fails the run
  when the critic says the work is incomplete — with the dict PRESERVED, since
  the gap list is exactly the thing the next stretch needs.

The experiment (RED) tests live at the top of the file; everything below them is
what the fix has to keep true.
"""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import ValidationError, validate_spec
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


def _ok_responder():
    def responder(_prompt):
        return "R"

    return responder


def _counting():
    calls = []

    def responder(prompt):
        calls.append(prompt)
        return "R"

    return calls, responder


def _reject_spec(*, required: bool) -> dict:
    """`cp` gates `go`; `side` depends on nothing.

    The third node is load-bearing for the UNREQUIRED case: with only `cp` and
    `go`, a rejection nulls both and ``derive_status`` reads "everything nulled"
    as ``failed``, which would hide the difference ``required`` is supposed to
    make."""
    node = {"id": "cp", "type": "checkpoint", "prompt": "Ship it?", "accept": ["sim"]}
    if required:
        node["required"] = True
    return {
        "meta": {"name": "cpguard", "version": 1},
        "nodes": [
            node,
            {"id": "go", "type": "agent", "prompt": "Execute: ${cp}"},
            {"id": "side", "type": "agent", "prompt": "unrelated"},
        ],
    }


# --- the H4 witness: what a checkpoint with no `accept` does (unchanged) -----


def test_without_accept_any_answer_is_still_the_output(db, tmp_path):
    """The behaviour the issue reported, pinned as the BASELINE it stays.

    No ``accept`` means the harness has been told nothing about what a "yes"
    looks like, so it keeps reading every answer as the node's output — this is
    the path every existing spec is on, and the guard must not move it. It is
    also the witness for H4: the refusal reaches the dependent leaf as text."""
    calls, responder = _counting()
    spec = {
        "meta": {"name": "cpbase", "version": 1},
        "nodes": [
            {"id": "cp", "type": "checkpoint", "prompt": "Ship it?"},
            {"id": "go", "type": "agent", "prompt": "Execute: ${cp}"},
        ],
    }
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(spec, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não, cancele"})
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"]["cp"] == "não, cancele"
        assert calls == ["Execute: não, cancele"]
    finally:
        svc.shutdown()


# --- experiment (i): a rejected checkpoint must not release the run ---------


def test_a_rejected_checkpoint_never_spawns_the_dependent(db, tmp_path):
    """DESIRED: "não, cancele" is not in ``accept``, so the gate stays shut.

    Today the answer is cached verbatim and interpolated into the dependent's
    prompt — the leaf runs on the strength of a refusal."""
    calls, responder = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_reject_spec(required=False), {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não, cancele"})
        done = svc.status(run_id, wait=True, timeout=10)
        assert not any("Execute:" in prompt for prompt in calls)
        assert done["outputs"]["cp"] is None
        assert done["outputs"]["go"] is None
        assert done["status"] == "degraded"  # `side` still produced something
        assert any(
            "cp" in fault and "rejected" in fault and "não, cancele" in fault
            for fault in done["faults_total"]
        )
    finally:
        svc.shutdown()


def test_a_rejected_required_checkpoint_fails_the_run(db, tmp_path):
    """DESIRED: the same rejection on a ``required`` gate ends the run (§7.4)."""
    calls, responder = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_reject_spec(required=True), {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não, cancele"})
        done = svc.status(run_id, wait=True, timeout=10)
        assert not any("Execute:" in prompt for prompt in calls)
        assert done["outputs"]["cp"] is None
        assert done["outputs"].get("go") is None  # skipped: it never ran at all
        assert done["status"] == "failed"
        assert done["required_failure"] == "cp"
    finally:
        svc.shutdown()


# --- experiment (ii): `complete: false` on a required check fails the run ----


_GAPS_SPEC = {
    "meta": {"name": "gaps", "version": 1},
    "nodes": [
        {
            "id": "c",
            "type": "completeness_check",
            "task": "List every config file.",
            "results": "${args.found}",
            "required": True,
        }
    ],
}


def test_a_required_completeness_check_fails_the_run_on_gaps(db):
    """DESIRED: ``complete: false`` on a ``required`` node is a failed run — and
    the dict SURVIVES, because ``missing`` is what the next stretch reads."""
    core = _core(db, lambda _p: json.dumps({"complete": False, "missing": ["x"]}))
    try:
        spec = validate_spec(_GAPS_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"found": ["setup.cfg"]})
        assert result.status == "failed"
        assert result.required_failure == "c"
        assert result.outputs["c"] == {"complete": False, "missing": ["x"]}
        assert any("completeness" in fault and "x" in fault for fault in result.faults)
    finally:
        core.shutdown()

# --- `on_reject: pause` — ask the same question again ----------------------


_PAUSE_SPEC = {
    "meta": {"name": "cppause", "version": 1},
    "nodes": [
        {
            "id": "cp",
            "type": "checkpoint",
            "prompt": "Ship it?",
            "accept": ["sim"],
            "on_reject": "pause",
        },
        {"id": "go", "type": "agent", "prompt": "Execute: ${cp}"},
    ],
}


def test_on_reject_pause_asks_the_same_question_again_and_says_why(db, tmp_path):
    """A human who is asked twice has to see WHY, or the second pause reads as
    a lost answer. The rejected answer rides in the payload; it is never the
    node's output."""
    calls, responder = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_PAUSE_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não"})
        again = svc.status(run_id, wait=True, timeout=10)
        assert again["status"] == "paused"
        assert again["checkpoint"] == {
            "node_id": "cp", "prompt": "Ship it?", "rejected": "não"
        }
        assert calls == []
    finally:
        svc.shutdown()


def test_a_second_answer_that_is_accepted_finishes_the_run(db, tmp_path):
    """The rejection is not a dead end: the gate is still a gate, and a YES on
    the next resume releases exactly the run that was waiting."""
    calls, responder = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_PAUSE_SPEC, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não"})
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": " SIM "})
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"]["cp"] == " SIM "  # the answer verbatim, not normalised
        assert calls == ["Execute:  SIM "]
    finally:
        svc.shutdown()


def test_a_rejected_answer_is_never_cached(db, tmp_path):
    """`cache_answer` is what retires a question for good. A refused answer must
    not retire it — a later resume has to be able to ask again and get a YES."""
    svc = _service(db, tmp_path, _ok_responder())
    try:
        run_id = svc.start(_reject_spec(required=True), {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não"})
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "failed"
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "sim"})
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"]["cp"] == "sim"
    finally:
        svc.shutdown()


# --- how an answer is MATCHED ---------------------------------------------


def _accept_run(db, tmp_path, answer):
    calls, responder = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_reject_spec(required=False), {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": answer})
        return svc.status(run_id, wait=True, timeout=10)
    finally:
        svc.shutdown()


@pytest.mark.parametrize("answer", ["sim", "SIM", "  Sim  "])
def test_case_and_surrounding_space_never_decide_a_human_gate(db, tmp_path, answer):
    """`.strip().lower()` on both sides — the convention the rest of the harness
    already reads human words with. A gate that turned on a trailing space would
    be a gate nobody could answer."""
    assert _accept_run(db, tmp_path, answer)["outputs"]["cp"] == answer


@pytest.mark.parametrize("answer", [{"ok": True}, ["sim"], None, 1])
def test_a_non_string_answer_is_never_accepted_by_accident(db, tmp_path, answer):
    """`checkpoint_answers` carries any JSON value. Comparing through ``str``
    keeps the gate readable, and nothing that is not the declared word gets in."""
    done = _accept_run(db, tmp_path, answer)
    assert done["outputs"]["cp"] is None
    assert any("rejected by human" in fault for fault in done["faults_total"])


# --- the ledger --------------------------------------------------------------


def test_a_rejected_checkpoint_lands_node_failed_in_the_ledger(tmp_path):
    """No new event type (§11 is a closed set): a rejection nulls the node
    without pausing, which is exactly what `node.failed` already means."""
    database = SessionDB(str(tmp_path / "audit.db"))
    calls, responder = _counting()
    svc = _service(database, tmp_path, responder)
    try:
        run_id = svc.start(_reject_spec(required=False), {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não"})
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "degraded"
        assert svc._audit.flush(timeout=5)
        events = database.audit_query(run_id, limit=200)["events"]
        failed = [
            event
            for event in events
            if event["event_type"] == "node.failed"
            and event["identity"]["node_path"] == ["cp"]
        ]
        assert len(failed) == 1
    finally:
        svc.shutdown()
        database.close()


# --- one level down ----------------------------------------------------------


def test_a_rejected_checkpoint_inside_a_nested_workflow_aborts_the_parent(db):
    """`checkpoint_answers` is shared with the nested engine by reference, so a
    nested gate reads its own answer — and its rejection has to travel the same
    road every nested `required` failure does."""
    child = {
        "meta": {"name": "child", "version": 1},
        "nodes": [
            {
                "id": "cp",
                "type": "checkpoint",
                "prompt": "Ship it?",
                "accept": ["sim"],
                "required": True,
            }
        ],
    }
    parent = {
        "meta": {"name": "parent", "version": 1},
        "nodes": [
            {"id": "sub", "type": "workflow", "ref": "child"},
            {"id": "after", "type": "agent", "prompt": "done"},
        ],
    }
    core = _core(db, _ok_responder())
    try:
        engine = WorkflowEngine(
            core,
            budget=Budget(),
            loader=lambda ref: child if ref == "child" else None,
            checkpoint_answers={"cp": "não"},
        )
        result = engine.run(validate_spec(parent, supported_types=SUPPORTED_NODE_TYPES), {})
    finally:
        core.shutdown()
    assert result.status == "failed"
    assert result.required_failure == "sub[child]:cp"
    assert any("rejected by human" in fault for fault in result.faults)
    assert "after" not in result.outputs


# --- the validator refuses the three footguns --------------------------------


def _issues(node: dict) -> str:
    spec = validate_spec(
        {"meta": {"name": "v", "version": 1}, "nodes": [node]},
        supported_types=SUPPORTED_NODE_TYPES,
    )
    assert isinstance(spec, ValidationError), "expected the spec to be refused"
    return spec.message


def test_a_spec_may_declare_accept_and_on_reject():
    spec = validate_spec(
        {
            "meta": {"name": "v", "version": 1},
            "nodes": [
                {
                    "id": "cp",
                    "type": "checkpoint",
                    "prompt": "Ship?",
                    "accept": ["sim"],
                    "on_reject": "pause",
                    "default": "SIM",
                }
            ],
        },
        supported_types=SUPPORTED_NODE_TYPES,
    )
    assert not isinstance(spec, ValidationError)


@pytest.mark.parametrize("accept", [[], ["  "], "sim", [3]])
def test_an_accept_that_lists_nothing_usable_is_refused(accept):
    """`accept: []` reads as "nothing releases this gate", which is a gate no
    human can ever pass — an author-time error is far cheaper than a run."""
    assert "accept" in _issues(
        {"id": "cp", "type": "checkpoint", "prompt": "Ship?", "accept": accept}
    )


def test_on_reject_without_accept_is_refused():
    message = _issues(
        {"id": "cp", "type": "checkpoint", "prompt": "Ship?", "on_reject": "pause"}
    )
    assert "on_reject" in message and "accept" in message


def test_an_unknown_on_reject_is_refused_rather_than_clamped():
    """Falling back to `fail` would apply a policy the author did not choose."""
    message = _issues(
        {
            "id": "cp",
            "type": "checkpoint",
            "prompt": "Ship?",
            "accept": ["sim"],
            "on_reject": "abort",
        }
    )
    assert "on_reject" in message and "fail" in message and "pause" in message


def test_a_default_the_gate_would_reject_is_refused():
    """An unattended resume answers with the default — a default outside
    `accept` is a node that fails every time nobody is watching."""
    message = _issues(
        {
            "id": "cp",
            "type": "checkpoint",
            "prompt": "Ship?",
            "accept": ["sim"],
            "default": "talvez",
        }
    )
    assert "default" in message and "accept" in message


def test_a_null_default_is_judged_by_the_same_comparison_the_runtime_uses():
    """`default: null` is a legal default (WF-10). The validator and the runtime
    read it through ONE function, so a spec that validates can never reject its
    own default at run time."""
    assert "default" in _issues(
        {"id": "cp", "type": "checkpoint", "prompt": "Ship?", "accept": ["sim"], "default": None}
    )
    spec = validate_spec(
        {
            "meta": {"name": "v", "version": 1},
            "nodes": [
                {
                    "id": "cp",
                    "type": "checkpoint",
                    "prompt": "Ship?",
                    "accept": ["None"],
                    "default": None,
                }
            ],
        },
        supported_types=SUPPORTED_NODE_TYPES,
    )
    assert not isinstance(spec, ValidationError)


# --- `completeness_check` + `required` ---------------------------------------


def _gaps_spec(*, required: bool) -> dict:
    node = {
        "id": "c",
        "type": "completeness_check",
        "task": "List every config file.",
        "results": "${args.found}",
    }
    if required:
        node["required"] = True
    return {"meta": {"name": "gaps", "version": 1}, "nodes": [node]}


def test_gaps_on_a_check_that_is_not_required_change_nothing(db):
    """Opt-in, like `required` itself: a critic nobody declared indispensable
    reports its gaps and the run carries on, exactly as before."""
    core = _core(db, lambda _p: json.dumps({"complete": False, "missing": ["x"]}))
    try:
        spec = validate_spec(_gaps_spec(required=False), supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"found": []})
        assert result.status == "complete"
        assert result.required_failure is None
    finally:
        core.shutdown()


def test_a_required_check_that_finds_nothing_missing_passes(db):
    core = _core(db, lambda _p: json.dumps({"complete": True, "missing": []}))
    try:
        spec = validate_spec(_gaps_spec(required=True), supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"found": ["a"]})
        assert result.status == "complete"
        assert result.required_failure is None
    finally:
        core.shutdown()


def test_the_gap_fault_names_the_first_three_and_counts_the_rest():
    """Faults are prose an agent relays; forty gaps would bury the rollup. The
    full list stays in `outputs`, which is why the dict is preserved."""
    from lohra.workflow.required import completeness_fault

    assert completeness_fault("c", ["a", "b"]) == (
        "c: completeness check found gaps: ['a', 'b'] — run aborted (required: true)"
    )
    assert "(+2 more)" in completeness_fault("c", ["a", "b", "c", "d", "e"])


def test_only_an_explicit_false_counts_as_incompleteness():
    """A missing or unreadable `complete` is not a claim that work is undone —
    reading one as a failure would abort runs on a critic's malformed answer."""
    from lohra.workflow.nodes import Node
    from lohra.workflow.required import completeness_gaps

    node = Node("c", "completeness_check", {"required": True})
    assert completeness_gaps(node, {"complete": False, "missing": ["x"]}) == ["x"]
    assert completeness_gaps(node, {"complete": False}) == []  # gaps, unnamed
    assert completeness_gaps(node, {"complete": True, "missing": []}) is None
    assert completeness_gaps(node, {"missing": ["x"]}) is None
    assert completeness_gaps(node, "not a dict") is None
    assert completeness_gaps(Node("a", "agent", {}), {"complete": False}) is None


# --- the two interactions that could re-open the hole ------------------------


_PAUSE_WITH_DEFAULT = {
    "meta": {"name": "cppd", "version": 1},
    "nodes": [
        {
            "id": "cp",
            "type": "checkpoint",
            "prompt": "Ship it?",
            "accept": ["sim"],
            "on_reject": "pause",
            "default": "sim",
            "required": True,
        },
        {"id": "go", "type": "agent", "prompt": "Execute: ${cp}"},
    ],
}


def test_a_declared_default_never_answers_a_question_a_human_just_refused(db, tmp_path):
    """The one path that would undo the whole guard.

    A ``default`` exists so an UNATTENDED resume can carry on. Re-offering it
    after a human answered NO would let the very next bare resume say YES on
    their behalf — the harness overruling the human, which is the single thing a
    checkpoint exists to prevent. So the re-pause drops the default and the
    plain resume is refused, exactly as a checkpoint with no default is."""
    calls, responder = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_PAUSE_WITH_DEFAULT, {})["run_id"]
        first = svc.status(run_id, wait=True, timeout=10)
        assert first["checkpoint"]["default"] == "sim"  # offered on the FIRST ask
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não"})
        again = svc.status(run_id, wait=True, timeout=10)
        assert again["status"] == "paused"
        assert "default" not in again["checkpoint"]
        assert again["checkpoint"]["rejected"] == "não"
        out = svc.start(None, {}, resume_run_id=run_id)  # a bare, unattended resume
        assert "HUMAN" in out["error"] and "checkpoint_answers" in out["error"]
        assert calls == []
    finally:
        svc.shutdown()


def test_a_required_gate_that_pauses_on_rejection_is_paused_not_failed(db, tmp_path):
    """`required` says what a NULL node costs, and a pause nulls the node too.
    Calling that "required failed" would bury a resume that is already waiting —
    the same rule a quota or budget pause has always had (§7.4)."""
    svc = _service(db, tmp_path, _ok_responder())
    try:
        run_id = svc.start(_PAUSE_WITH_DEFAULT, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"cp": "não"})
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "paused"
        assert "required_failure" not in done
    finally:
        svc.shutdown()
