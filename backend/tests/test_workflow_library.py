"""Tests for self-improvement: outcomes -> memory/templates + rollup (Milestone J)."""

import json

from lohra.workflow import library, rollup
from lohra.workflow.accounting import RunResult
from lohra.workflow.schema import ValidationError, validate_spec

_SPEC = {
    "meta": {"name": "triage", "description": "find + verify bugs"},
    "nodes": [
        {"id": "scan", "type": "agent", "prompt": "x"},
        {"id": "check", "type": "verify", "finding": "${scan}", "skeptics": 3},
    ],
}


def _result(*, status="complete", null_count=0, nodes_total=2, **kw):
    return RunResult(status=status, null_count=null_count, nodes_total=nodes_total, **kw)


def _stamped(leaf_respawns, artifact_divergences=0, replay_divergences=0):
    """``_SPEC`` as the library writes it: the spec plus what the certifying run
    cost in extra leaves (Q2, #43), how many artifact claims the harness had to
    correct for it (#45) and how many of its cells replayed under another
    operator policy or harness version (#75)."""
    return {
        **_SPEC,
        "meta": {
            **_SPEC["meta"],
            "leaf_respawns": leaf_respawns,
            "artifact_divergences": artifact_divergences,
            "replay_divergences": replay_divergences,
        },
    }


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
    # A run that needed no re-spawn says so with a number, not with silence.
    assert templates[0]["leaf_respawns"] == 0
    # ...and a run nobody had to advise about says the same, the same way (#45).
    assert templates[0]["artifact_divergences"] == 0
    # the full spec is retrievable and re-runnable
    assert library.get_template(tmp_path, "triage") == _stamped(0)
    # a clean run leaves no failure prior
    assert library.recent_insights(tmp_path) == []


def test_a_recovered_run_certifies_and_says_what_it_cost(tmp_path):
    """Q2 (#43): the whole point of discounting recovered faults is that runs
    like this one reach ``library`` as ``complete``. Certifying them silently
    would trade one dishonesty for another — the template carries the price."""
    library.record_outcome(
        tmp_path,
        _SPEC,
        _result(
            status="complete",
            faults=["scan: leaf error: bad gateway (attempt 1/2)"],
            recovered_faults=["scan: leaf error: bad gateway (attempt 1/2)"],
            leaf_respawns=1,
        ),
        leaf_respawns=1,
    )
    assert library.list_templates(tmp_path)[0]["leaf_respawns"] == 1
    stamped = library.get_template(tmp_path, "triage")
    assert stamped["meta"]["leaf_respawns"] == 1
    # ...and the stamp did not make the template unrunnable: the extra meta key
    # is a literal, which is all ``meta`` was ever required to hold.
    assert not isinstance(validate_spec(stamped), ValidationError)
    # The caller's own spec is untouched — it is the live run's.
    assert "leaf_respawns" not in _SPEC["meta"]


def test_a_template_written_before_the_stamp_says_nothing_rather_than_zero(tmp_path):
    """Q2: "it never re-spawned" and "nobody counted" are different facts."""
    directory = tmp_path / "workflows" / "templates"
    directory.mkdir(parents=True)
    (directory / "legacy.json").write_text(json.dumps(_SPEC), encoding="utf-8")
    entry = library.list_templates(tmp_path)[0]
    assert "leaf_respawns" not in entry
    assert "artifact_divergences" not in entry  # #45, same rule, same reason


def test_get_unknown_template_is_none(tmp_path):
    assert library.get_template(tmp_path, "nope") is None
    assert library.list_templates(tmp_path) == []


# --- problematic run -> no legacy learning, no template -----------------


def test_degraded_run_teaches_the_library_nothing(tmp_path):
    library.record_outcome(tmp_path, _SPEC, _result(status="degraded", null_count=2, nodes_total=2))
    # the legacy insights file is NOT written by problematic runs anymore
    assert library.recent_insights(tmp_path) == []
    assert not (tmp_path / "workflows" / "insights.md").exists()
    # ...and a degraded run is NOT saved as a reusable template
    assert library.list_templates(tmp_path) == []


def test_high_null_rate_complete_run_is_not_a_template(tmp_path):
    # completed but mostly-null -> not certified, and not written either
    library.record_outcome(tmp_path, _SPEC, _result(status="complete", null_count=2, nodes_total=2))
    assert library.list_templates(tmp_path) == []
    assert library.recent_insights(tmp_path) == []
    assert not (tmp_path / "workflows" / "insights.md").exists()


def test_quota_timeout_and_process_loss_outcomes_write_nothing(tmp_path):
    """Quota exhaustion, a pipeline timeout and a lost process all land here as
    a non-complete verdict: none of them may write insights, templates or
    skills — the legacy file (absent or not) stays byte-identical."""
    legacy = tmp_path / "workflows" / "insights.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("- [kept] shape a -> paused.\n", encoding="utf-8")
    before = legacy.read_bytes()
    for status in ("paused", "timeout", "failed", "cancelled"):
        library.record_outcome(
            tmp_path, _SPEC, _result(status=status, faults=[f"{status}: quota/timeout/lost"])
        )
    assert legacy.read_bytes() == before
    assert library.recent_insights(tmp_path) == []
    assert library.list_templates(tmp_path) == []
    assert not (tmp_path / "workflows" / "templates").exists()


def test_a_problematic_run_leaves_a_preexisting_legacy_insights_file_untouched(tmp_path):
    legacy = tmp_path / "workflows" / "insights.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("- [old] shape a -> degraded.\n", encoding="utf-8")
    before = legacy.read_bytes()
    bad = _result(status="degraded", null_count=2, nodes_total=2)
    for _ in range(5):  # same flaky shape run repeatedly
        library.record_outcome(tmp_path, _SPEC, bad)
    assert legacy.read_bytes() == before  # read-only: not appended, truncated, or deleted
    assert library.recent_insights(tmp_path) == []


def test_record_outcome_never_raises(tmp_path):
    # a malformed spec / odd input must not break a finished run
    library.record_outcome(tmp_path, {"meta": None, "nodes": "weird"}, _result())  # no raise


def test_template_name_is_sanitized(tmp_path):
    spec = {
        "meta": {"name": "../etc/passwd"},
        "nodes": [{"id": "a", "type": "agent", "prompt": "x"}],
    }
    library.record_outcome(tmp_path, spec, _result(status="complete", null_count=0, nodes_total=1))
    files = list((tmp_path / "workflows" / "templates").glob("*.json"))
    assert len(files) == 1
    assert "/" not in files[0].name and ".." not in files[0].stem


# --- the service writes a template on a clean run (integration light) ---


def test_template_json_is_valid(tmp_path):
    library.record_outcome(tmp_path, _SPEC, _result(status="complete", null_count=0))
    path = tmp_path / "workflows" / "templates" / "triage.json"
    assert json.loads(path.read_text()) == _stamped(0)
