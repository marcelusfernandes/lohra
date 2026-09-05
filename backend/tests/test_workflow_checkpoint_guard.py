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
