"""Tests for self-improvement: outcomes -> memory/templates + rollup (Milestone J)."""

import json

import pytest

from lohra.workflow import library, rollup
from lohra.workflow.accounting import NodeCost, RunResult
from lohra.workflow.schema import ValidationError, validate_spec

_SPEC = {
    "meta": {"name": "triage", "description": "find + verify bugs"},
    "nodes": [
        {"id": "scan", "type": "agent", "prompt": "x"},
        {"id": "check", "type": "verify", "finding": "${scan}", "skeptics": 3},
    ],
}

# E4 (#51): provenance stamps two facts that are always knowable at
# certification (the running harness's version, the certification instant) and
# are otherwise non-deterministic across test runs. Frozen here so the
# exact-equality assertions below stay meaningful.
_HARNESS_VERSION = "test-harness-0.0.0"
_CERTIFIED_AT = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _frozen_provenance_clock(monkeypatch):
    monkeypatch.setattr(library, "_harness_version", lambda: _HARNESS_VERSION)
    monkeypatch.setattr(library, "_certified_at", lambda: _CERTIFIED_AT)


def _result(*, status="complete", null_count=0, nodes_total=2, **kw):
    return RunResult(status=status, null_count=null_count, nodes_total=nodes_total, **kw)


def _provenance(run_id=None, profile=None, routes=None):
    return {
        "run_id": run_id,
        "profile": profile,
        "harness_version": _HARNESS_VERSION,
        "certified_at": _CERTIFIED_AT,
        "routes": routes or {},
    }


def _stamped(
    leaf_respawns,
    artifact_divergences=0,
    replay_divergences=0,
    budget_overrun=0,
    run_id=None,
    profile=None,
    routes=None,
):
    """``_SPEC`` as the library writes it: the spec plus what the certifying run
    cost in extra leaves (Q2, #43), how many artifact claims the harness had to
    correct for it (#45), how many of its cells replayed under another operator
    policy or harness version (#75), how far past its token ceiling it went
    (#71), and where/when/on what it was certified (E4, #51)."""
    return {
        **_SPEC,
        "meta": {
            **_SPEC["meta"],
            "leaf_respawns": leaf_respawns,
            "artifact_divergences": artifact_divergences,
            "replay_divergences": replay_divergences,
            "budget_overrun": budget_overrun,
            "provenance": _provenance(run_id, profile, routes),
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
    # ...and so does a run that stayed inside its token ceiling (#71).
    assert templates[0]["budget_overrun"] == 0
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
    # E4 (#51): unlike the counters above, ``provenance`` is never OMITTED — a
    # template from before this shipped has no run/profile/harness/route to
    # report, and the reader must be told so explicitly rather than left to
    # infer "absent" from a missing key (the exception is deliberate: a
    # missing key here would be indistinguishable from "this reader is old").
    assert entry["provenance"] is None


# --- E4 (#51): provenance -- where/when/on-what a template was certified ---


def test_certified_template_records_provenance(tmp_path):
    """A template saved today knows its run of origin, the harness that
    proved it, when that happened, and the effective route each node ran
    on — not the route the spec DECLARED (tier, an unresolved default), but
    the one ``NodeCost`` recorded actually running that leaf."""
    result = _result(
        status="complete",
        null_count=0,
        node_costs={
            "scan": NodeCost(provider="anthropic", model="claude-haiku-4-5"),
            "check": NodeCost(provider="anthropic", model="claude-opus-4-8"),
        },
    )
    library.record_outcome(tmp_path, _SPEC, result, run_id="run-e4-1")
    stamped = library.get_template(tmp_path, "triage")
    assert stamped["meta"]["provenance"] == _provenance(
        run_id="run-e4-1",
        routes={
            "scan": {"provider": "anthropic", "model": "claude-haiku-4-5"},
            "check": {"provider": "anthropic", "model": "claude-opus-4-8"},
        },
    )


def test_a_node_whose_leaves_disagreed_on_route_reports_neither(tmp_path):
    """``NodeCost.merge`` already answers ``None``/``None`` when one node's
    leaves ran on different routes within the certifying stretch (a fan-out
    whose items were not all rerouted together) — provenance passes that
    honest "unknown, not unset" straight through, never guessing a winner."""
    result = _result(
        status="complete",
        null_count=0,
        node_costs={"scan": NodeCost(provider=None, model=None)},
    )
    library.record_outcome(tmp_path, _SPEC, result, run_id="run-e4-2")
    routes = library.get_template(tmp_path, "triage")["meta"]["provenance"]["routes"]
    assert routes == {"scan": {"provider": None, "model": None}}


def test_list_templates_shows_a_compact_provenance_summary(tmp_path):
    """The list line stays compact (per-node routes are only in the full spec
    ``workflow_templates(name=...)`` returns) — but run_id/harness_version/
    certified_at are worth a glance before an author retrieves the full spec."""
    result = _result(
        status="complete",
        null_count=0,
        node_costs={"scan": NodeCost(provider="anthropic", model="claude-haiku-4-5")},
    )
    library.record_outcome(tmp_path, _SPEC, result, run_id="run-e4-3")
    entry = library.list_templates(tmp_path)[0]
    assert entry["provenance"] == {
        "run_id": "run-e4-3",
        "harness_version": _HARNESS_VERSION,
        "certified_at": _CERTIFIED_AT,
    }
    assert "routes" not in entry["provenance"]
    assert "profile" not in entry["provenance"]


def test_a_same_run_recertification_merges_routes_instead_of_erasing_them(tmp_path):
    """No durable store persists a node's route (unlike leaf_respawns/
    rerouted_nodes/the advisory counters, which service.py folds off the
    run's durable line before calling here) — so a SECOND call under the
    SAME run_id, whose fresh node_costs covers only nodes that ran leaves
    THIS time, must not blank out a route the first call already recorded.
    A cache-replayed stretch (nothing fresh) is the extreme case: routes
    must come back byte-identical, not empty."""
    library.record_outcome(
        tmp_path,
        _SPEC,
        _result(node_costs={"scan": NodeCost(provider="anthropic", model="claude-haiku-4-5")}),
        run_id="run-e4-4",
    )
    # A second stretch of the SAME run: "check" runs fresh, "scan" replays
    # from cache and contributes nothing to this stretch's node_costs.
    library.record_outcome(
        tmp_path,
        _SPEC,
        _result(node_costs={"check": NodeCost(provider="anthropic", model="claude-opus-4-8")}),
        run_id="run-e4-4",
    )
    routes = library.get_template(tmp_path, "triage")["meta"]["provenance"]["routes"]
    assert routes == {
        "scan": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        "check": {"provider": "anthropic", "model": "claude-opus-4-8"},
    }


def test_a_different_runs_certification_replaces_routes_wholesale(tmp_path):
    """A DIFFERENT run_id certifying the same template name is a fresh
    proof, not a continuation — merging across runs would publish a
    Frankenstein route map naming nodes from two different executions."""
    library.record_outcome(
        tmp_path,
        _SPEC,
        _result(node_costs={"scan": NodeCost(provider="anthropic", model="claude-haiku-4-5")}),
        run_id="run-e4-5",
    )
    library.record_outcome(
        tmp_path,
        _SPEC,
        _result(node_costs={"check": NodeCost(provider="anthropic", model="claude-opus-4-8")}),
        run_id="run-e4-6",
    )
    routes = library.get_template(tmp_path, "triage")["meta"]["provenance"]["routes"]
    assert routes == {"check": {"provider": "anthropic", "model": "claude-opus-4-8"}}


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
