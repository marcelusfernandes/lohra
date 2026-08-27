"""Tests for self-improvement: outcomes -> memory/templates + rollup (Milestone J)."""

import json

from lohra.workflow import library, rollup
from lohra.workflow.engine import RunResult

_SPEC = {
    "meta": {"name": "triage", "description": "find + verify bugs"},
    "nodes": [
        {"id": "scan", "type": "agent", "prompt": "x"},
        {"id": "check", "type": "verify", "finding": "${scan}", "skeptics": 3},
    ],
}


def _result(*, status="complete", null_count=0, nodes_total=2, **kw):
    return RunResult(status=status, null_count=null_count, nodes_total=nodes_total, **kw)


# --- rollup ---


def test_rollup_includes_null_rate():
    r = _result(null_count=1, nodes_total=4, cap_trips=1, engine_faults=0, validation_retries=2)
    out = rollup.summarize("run-1", "degraded", r)
    assert out["null_rate"] == 0.25
    assert out["cap_trips"] == 1 and out["validation_retries"] == 2
    assert out["nodes_total"] == 4


def test_rollup_handles_no_result():
    out = rollup.summarize("run-1", "failed", None, error="boom")
    assert out == {"run_id": "run-1", "status": "failed", "error": "boom"}


# --- clean run -> template (no memory prior) ---


def test_clean_run_saved_as_template(tmp_path):
    library.record_outcome(tmp_path, _SPEC, _result(status="complete", null_count=0))
    templates = library.list_templates(tmp_path)
    assert [t["name"] for t in templates] == ["triage"]
    assert templates[0]["description"] == "find + verify bugs"
    # the full spec is retrievable and re-runnable
    assert library.get_template(tmp_path, "triage") == _SPEC
    # a clean run leaves no failure prior
    assert library.recent_insights(tmp_path) == []


def test_get_unknown_template_is_none(tmp_path):
    assert library.get_template(tmp_path, "nope") is None
    assert library.list_templates(tmp_path) == []


# --- problematic run -> memory prior (no template) ---


def test_degraded_run_writes_insight_prior(tmp_path):
    library.record_outcome(tmp_path, _SPEC, _result(status="degraded", null_count=2, nodes_total=2))
    insights = "\n".join(library.recent_insights(tmp_path))
    assert "[triage]" in insights and "degraded" in insights and "null_rate" in insights
    # priors go to the dedicated insights file, NOT the curated MEMORY.md
    assert not (tmp_path / "memories" / "MEMORY.md").exists()
    # a degraded run is NOT saved as a reusable template
    assert library.list_templates(tmp_path) == []


def test_high_null_rate_complete_run_is_not_a_template(tmp_path):
    # completed but mostly-null -> a prior, not a template
    library.record_outcome(tmp_path, _SPEC, _result(status="complete", null_count=2, nodes_total=2))
    assert library.list_templates(tmp_path) == []
    assert any("[triage]" in ln for ln in library.recent_insights(tmp_path))


def test_insight_priors_are_deduped(tmp_path):
    bad = _result(status="degraded", null_count=2, nodes_total=2)
    for _ in range(5):  # same flaky shape run repeatedly
        library.record_outcome(tmp_path, _SPEC, bad)
    assert len(library.recent_insights(tmp_path)) == 1  # not 5 near-duplicates


def test_record_outcome_never_raises(tmp_path):
    # a malformed spec / odd input must not break a finished run
    library.record_outcome(tmp_path, {"meta": None, "nodes": "weird"}, _result())  # no raise


def test_template_name_is_sanitized(tmp_path):
    spec = {"meta": {"name": "../etc/passwd"}, "nodes": [{"id": "a", "type": "agent", "prompt": "x"}]}
    library.record_outcome(tmp_path, spec, _result(status="complete", null_count=0, nodes_total=1))
    files = list((tmp_path / "workflows" / "templates").glob("*.json"))
    assert len(files) == 1
    assert "/" not in files[0].name and ".." not in files[0].stem


# --- the service writes a template on a clean run (integration light) ---


def test_template_json_is_valid(tmp_path):
    library.record_outcome(tmp_path, _SPEC, _result(status="complete", null_count=0))
    path = tmp_path / "workflows" / "templates" / "triage.json"
    assert json.loads(path.read_text()) == _SPEC
