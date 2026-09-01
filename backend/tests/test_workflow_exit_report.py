"""What ``lohra chat --json`` learns about this turn's workflow runs (#47).

``collect_turn_workflows`` is the piece the CLI calls BEFORE
``WorkflowService.shutdown()`` — which cancels whatever the turn leaves
running — so the envelope can say what happened instead of the agent finding
out nothing (the durable pause notice is addressed to a next turn that a
one-shot ``--json`` call never has).

Two real ``WorkflowService`` scenarios (not a fake): a run that really paused
on the token budget (the exact case #47 diagnosed), and a run genuinely still
in flight when the turn ends. A third proves the field stays silent for the
ordinary case — a run that finished cleanly reports nothing here, which is
what keeps ``result_json``'s ``workflows`` key absent by default.

Also pins that ``own_run_ids()`` only ever returns runs THIS instance
launched — not the merged cross-store view ``list_runs()`` gives, which would
leak other sessions' runs into this turn's report.
"""

from __future__ import annotations

import threading

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED
from lohra.workflow.exit_report import collect_turn_workflows
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient

LEAF_COST = 8  # one fake turn: 5 input + 3 output tokens

_TWO_NODE = {
    "meta": {"name": "demo", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _service(db, home, responder):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


def _ok(_prompt):
    return "R"


def test_reports_nothing_for_a_run_that_finished_cleanly(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)  # let it actually finish
        assert collect_turn_workflows(svc) == []
    finally:
        svc.shutdown()


def test_reports_paused_run_with_its_pause_reason(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        svc.status(run_id, wait=True, timeout=10)  # let the pause land
        entries = collect_turn_workflows(svc)
        assert entries == [
            {"run_id": run_id, "status": "paused", "pause_reason": TOKEN_BUDGET_EXHAUSTED}
        ]
    finally:
        svc.shutdown()


def test_reports_still_running_run_with_observed_status_and_exit_flag(db, tmp_path):
    """Collected BEFORE shutdown(): the run is still genuinely alive, so this
    reports what was OBSERVED (``"running"``) plus the fact that shutdown() is
    about to cancel it — never a guessed-forward ``"cancelled"``, which could
    in principle be a lie if the run finishes cleanly first."""
    release = threading.Event()
    svc = _service(db, tmp_path, lambda _p: (release.wait(5), "R")[1])
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id)["status"] == "running"  # first leaf still blocked
        entries = collect_turn_workflows(svc)
        assert entries == [{"run_id": run_id, "status": "running", "cancelled_on_exit": True}]
    finally:
        release.set()
        svc.shutdown()


def test_a_run_whose_status_read_raises_is_reported_not_swallowed(db, tmp_path):
    """This is called from ``cli.py``'s ``finally``, right before ``shutdown()``
    and everything after it — a raise here must not skip that cleanup. Each
    run's read is its own failure domain: one bad run degrades to an honest
    entry, the collection (and the caller's ``finally``) keeps going."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]

        def boom(_run_id):
            raise RuntimeError("db is briefly locked")

        svc.status = boom  # type: ignore[method-assign]
        entries = collect_turn_workflows(svc)  # must not raise
        assert entries == [{"run_id": run_id, "status": "unknown", "read_error": "db is briefly locked"}]
    finally:
        del svc.status  # restore the bound method before shutdown() calls it
        svc.shutdown()


def test_own_run_ids_itself_raising_is_swallowed_too():
    """Same failure domain, one level up: a service that cannot even list its
    own runs (whatever the reason) must not take the caller's ``finally`` down
    with it — a duck-typed fake is enough, no engine needed for this one."""

    class _Broken:
        def own_run_ids(self):
            raise RuntimeError("lock held")

    assert collect_turn_workflows(_Broken()) == []


def test_own_run_ids_excludes_runs_this_service_never_launched(db, tmp_path):
    """``list_runs()`` merges in the whole store's recent durable rows; a
    per-turn report must not leak another session's run into this one."""
    launching = _service(db, tmp_path, _ok)
    try:
        other_run_id = launching.start(_TWO_NODE, {})["run_id"]
        launching.status(other_run_id, wait=True, timeout=10)
    finally:
        launching.shutdown()

    this_turn = _service(db, tmp_path, _ok)
    try:
        assert this_turn.own_run_ids() == []
        assert collect_turn_workflows(this_turn) == []
        # sanity: the other run really is visible through the cross-store view
        assert any(row["run_id"] == other_run_id for row in this_turn.list_runs())
    finally:
        this_turn.shutdown()
