"""Wave 9 slice E2 (issue #51) — structured evidence end to end.

Today (before this slice) ``WorkflowService.recent_insights`` collapses every
stored candidate row down to its ``summary`` string, discarding the causal
class the row already carries (``mechanism``, ``responsibility``,
``confidence``, ``status``, and ``hits`` once E1 lands). The consumer
(``workflow_templates`` in list mode, ``lohra/workflow/tools.py``) receives
prose it cannot filter by class.

This is the RED contract test named by the issue: it pins the TARGET shape
(structured dict), which fails today because the current return is
``list[str]``. Two companion tests pin the rest of the design: the
agent-facing rendering keeps the summary prose with a compact class tag
appended, and a row without ``hits`` (the common case until E1 lands)
projects without inventing the key.
"""

from __future__ import annotations

import json

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService
from lohra.workflow.tools import WorkflowTool
from tests.test_loop import FakeClient, _text_response

# No ``meta.name`` — rejected by validate_spec, which records a candidate
# (see test_workflow_spec_candidate.py) with a KNOWN class: mechanism
# "validation", responsibility "agency" (recomputed by the store),
# confidence 1.0, status "invalid_spec".
INVALID_SPEC = {"meta": {}, "nodes": []}


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


def test_recent_insights_returns_structured_dicts_not_bare_summaries(db, tmp_path):
    """RED contract test named by issue #51's E2 hypothesis comment."""
    svc = _service(db, tmp_path)
    try:
        WorkflowTool(svc).run({"spec": INVALID_SPEC})  # records one candidate
        insights = svc.recent_insights()
        assert len(insights) == 1
        insight = insights[0]
        assert isinstance(insight, dict), (
            f"expected a structured dict, got {type(insight).__name__}: {insight!r}"
        )
        for field in ("summary", "mechanism", "responsibility", "confidence", "status"):
            assert field in insight, f"missing {field!r} in {insight!r}"
        assert insight["mechanism"] == "validation"
        assert insight["responsibility"] == "agency"
        assert insight["confidence"] == 1.0
        assert insight["status"] == "invalid_spec"
    finally:
        svc.shutdown()


def test_templates_tool_renders_summary_plus_compact_class_tag(db, tmp_path):
    """The agent-facing text keeps reading as prose: unchanged summary, plus
    a compact ``[responsibility · mechanism · confidence · status]`` tag."""
    svc = _service(db, tmp_path)
    try:
        WorkflowTool(svc).run({"spec": INVALID_SPEC})
        row = db.insights.list()[0]  # ground truth, independent of recent_insights' shape
        out = json.loads(WorkflowTool(svc).templates({}))
        lines = out["insights"]
        assert len(lines) == 1
        line = lines[0]
        assert isinstance(line, str), "the prompt must still read as prose, not raw JSON"
        assert line.startswith(row["summary"]), "original summary text must survive unchanged"
        assert "[agency · validation · 1.0 · invalid_spec]" in line
    finally:
        svc.shutdown()


def test_recent_insights_projection_omits_hits_when_the_row_lacks_it(db, tmp_path, monkeypatch):
    """``hits`` (E1, parallel slice) is OPTIONAL: present only when the row
    already carries it — never defaulted, so absence stays visibly absent."""
    svc = _service(db, tmp_path)
    try:
        row_without_hits = {
            "summary": "s1",
            "mechanism": "validation",
            "responsibility": "agency",
            "confidence": 1.0,
            "status": "invalid_spec",
        }
        row_with_hits = dict(row_without_hits, summary="s2", hits=3)
        monkeypatch.setattr(
            svc._db.insights,
            "list",
            lambda limit=20: [row_without_hits, row_with_hits],
        )
        insights = svc.recent_insights()
        assert isinstance(insights[0], dict)
        assert "hits" not in insights[0]
        assert isinstance(insights[1], dict)
        assert insights[1]["hits"] == 3
    finally:
        svc.shutdown()
