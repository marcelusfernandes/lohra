"""Wave 9, E1 part 2 (issue #50) — lint warnings feed the insight store.

Coordinator decision on #50 (2026-09-05, last comment): wire LINT WARNINGS
(``nested_id_type_ignored``, ``disconnected_dag``, issue #49's rule surface)
as insight candidates; do NOT wire the other candidate triggers the census
enumerated (checkpoint rejection, required/completeness failure, route_fault
correction, aggregation holes) — those are attribution-indeterminate by the
taxonomy's own fail-closed design (see the census,
scratchpad/w9/exp-e1b.md).

Today (before this slice) a spec that ACCEPTS with a lint warning (the
``spec_warnings = lint_warnings(parsed)`` call in ``service.py``, downstream
of ``validate_spec`` succeeding) never reaches ``db.insights`` at all — only
an outright REJECTED spec does, via ``_record_spec_candidate``. This is the
RED: an authored, explicit spec that lints ``nested_id_type_ignored`` starts
successfully (a warning, not a refusal) but records nothing, even though it
carries the exact same structural evidence
(``mechanism="validation"`` + ``SIGNAL_SPEC_SHAPE`` + ``rule:<rule>``) the
taxonomy already resolves to ``AGENCY``.
"""

from __future__ import annotations

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService
from tests.test_loop import FakeClient, _text_response

# Two nodes, no depends_on/${ref} anywhere -> disconnected_dag. "fan" also
# carries a branch with 'id' -> nested_id_type_ignored on "fan". Both lint
# rules fire from ONE accepted spec, on purpose: it lets one test assert
# "one candidate per distinct rule" without two separate runs.
TWO_WARNING_SPEC = {
    "meta": {"name": "demo"},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "fan", "type": "parallel", "branches": [{"id": "x", "prompt": "also go"}]},
    ],
}

# Only disconnected_dag: two agent nodes, no edge between them.
ONE_WARNING_SPEC = {
    "meta": {"name": "demo"},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "also go"},
    ],
}

# No warnings at all: single node.
CLEAN_SPEC = {
    "meta": {"name": "demo"},
    "nodes": [{"id": "a", "type": "agent", "prompt": "do ${args.task}"}],
}


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _child_factory(reply="ok"):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response(reply)] * 8),
        )

    return factory


def _service(db, tmp_path, reply="ok"):
    return WorkflowService(base_child_factory=_child_factory(reply), db=db, home=tmp_path)


def test_lint_warning_from_an_authored_spec_records_one_candidate_per_rule(db, tmp_path):
    """RED: today this asserts 0 == 2 (nothing is recorded from a warning)."""
    svc = _service(db, tmp_path)
    try:
        out = svc.start(TWO_WARNING_SPEC, {}, agency_authored=True)
        assert "error" not in out
        assert len(out.get("warnings", [])) == 2  # both lint rules fired
        rows = db.insights.list()
        assert len(rows) == 2, f"expected one candidate per distinct rule, got {rows!r}"
        rules_seen = set()
        for row in rows:
            assert row["kind"] == "candidate"
            assert row["mechanism"] == "validation"
            assert row["responsibility"] == "agency"  # recomputed by the store's gate
            assert row["status"] == "lint_warning"
            # A warning is weaker evidence than an outright refusal (1.0): the
            # taxonomy's own floor for a learnable agency verdict, never higher.
            assert row["confidence"] == 0.8
            assert 0 < len(row["summary"]) <= 500
            rules_seen.add(
                "disconnected_dag" if "disconnected" in row["summary"] else "nested_id_type_ignored"
            )
        assert rules_seen == {"disconnected_dag", "nested_id_type_ignored"}
    finally:
        svc.shutdown()


def test_repeated_lint_warning_is_deduplicated_with_hits(db, tmp_path):
    """Two separate runs of the SAME warning -> one row, hits == 2 (E1's
    structural fingerprint merges the repeat, exactly like a repeated
    rejected-spec candidate already does)."""
    svc = _service(db, tmp_path)
    try:
        svc.start(ONE_WARNING_SPEC, {}, agency_authored=True)
        svc.start(ONE_WARNING_SPEC, {}, agency_authored=True)
        rows = db.insights.list()
        assert len(rows) == 1
        assert rows[0]["hits"] == 2
    finally:
        svc.shutdown()


def test_clean_spec_records_nothing_from_lint(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        out = svc.start(CLEAN_SPEC, {"task": "x"}, agency_authored=True)
        assert "warnings" not in out
        assert db.insights.count() == 0
    finally:
        svc.shutdown()


def test_lint_warning_without_agency_authored_flag_records_nothing(db, tmp_path):
    """Operator/test call (agency_authored default False) is not attributed —
    same fail-closed provenance gate `_record_spec_candidate` already uses."""
    svc = _service(db, tmp_path)
    try:
        out = svc.start(ONE_WARNING_SPEC, {})
        assert len(out.get("warnings", [])) == 1
        assert db.insights.count() == 0
    finally:
        svc.shutdown()


def test_lint_warning_on_persisted_spec_replay_records_nothing(db, tmp_path):
    """A resume WITHOUT an explicit spec replays the run's PERSISTED spec —
    authored by a past turn, never the current one — even when
    ``agency_authored=True`` is passed and the replay still lints warnings."""
    svc = _service(db, tmp_path)
    try:
        run_id = svc.start(ONE_WARNING_SPEC, {}, agency_authored=True)["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        assert db.insights.count() == 1  # the original authored start recorded one

        out = svc.start(None, {}, resume_run_id=run_id, agency_authored=True)
        assert "error" not in out
        assert len(out.get("warnings", [])) == 1  # the replay still lints the same warning
        assert db.insights.count() == 1  # but records nothing new
    finally:
        svc.shutdown()


def test_store_failure_on_lint_candidate_is_swallowed(db, tmp_path, monkeypatch, caplog):
    import logging

    svc = _service(db, tmp_path)
    try:

        def broken_record(**kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(db.insights, "record", broken_record)
        with caplog.at_level(logging.WARNING):
            out = svc.start(ONE_WARNING_SPEC, {}, agency_authored=True)
        assert "error" not in out
        assert len(out.get("warnings", [])) == 1
    finally:
        svc.shutdown()
