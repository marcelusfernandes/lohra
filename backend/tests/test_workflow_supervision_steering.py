"""Anti-drift contract: workflow_steer guidance (RUN_GUIDANCE + builtin SKILL.md).

The steering vocabulary is a contract between three surfaces \u2014 the tool
description, RUN_GUIDANCE and the workflow-authoring skill \u2014 and the numbers
it teaches are pinned by ``lohra/workflow/steering.py``. This module keeps
them from drifting apart: when a constant, a ceiling or an outcome name
changes here, the guidance and the skill must change with it, and vice versa.
"""

import re
from pathlib import Path

import pytest

from lohra.skills.store import builtin_root
from lohra.workflow.steering import (
    MAX_CORRECTIONS_PER_LEAF,
    MAX_EXTERNAL_STEERS_PER_LEAF,
    MAX_EXTERNAL_STEERS_PER_RUN,
)
from lohra.workflow.tools import RUN_GUIDANCE


@pytest.fixture(scope="module")
def skill_body() -> str:
    path = Path(builtin_root()) / "workflow-authoring" / "SKILL.md"
    return path.read_text(encoding="utf-8")


def squash(text: str) -> str:
    """The contract is the phrase, not the line wrap: compare unwrapped."""
    return " ".join(text.split())


def test_skill_exists_and_mentions_steer(skill_body):
    assert "workflow_steer" in skill_body


# -- RUN_GUIDANCE teaches the steering contract -----------------------------


class TestRunGuidanceSteering:
    def test_placement_after_identity_and_read_section(self):
        identity = RUN_GUIDANCE.find("Identity has two levels")
        read = RUN_GUIDANCE.find("Audit is metadata-only")
        steering = RUN_GUIDANCE.find("STEERING A LIVE LEAF")
        closer = RUN_GUIDANCE.find("For choosing between the node types")
        assert 0 <= identity < steering and 0 <= read < steering
        assert 0 < steering < closer  # inside the guidance, before the closer

    def test_discovery_via_audit_local_live_occurrence(self):
        assert "workflow_audit" in RUN_GUIDANCE
        assert "sub_id" in RUN_GUIDANCE
        assert "live execution occurrence" in RUN_GUIDANCE
        assert "THIS process" in RUN_GUIDANCE
        assert "segment_id, attempt, turn" in RUN_GUIDANCE

    def test_exact_occurrence_only_fail_closed(self):
        assert "ONLY that exact observed occurrence" in RUN_GUIDANCE
        for refused in ("stale", "ambiguous", "cache replay", "durable-only", "another process"):
            assert refused in RUN_GUIDANCE, refused
        assert "REJECTED" in RUN_GUIDANCE

    def test_queued_is_not_read_or_delivery(self):
        assert "Queued acceptance is NOT read and NOT delivery" in RUN_GUIDANCE
        assert "BETWEEN loop iterations" in RUN_GUIDANCE
        assert "preempts" in RUN_GUIDANCE
        assert "FROZEN prompt" in RUN_GUIDANCE

    def test_limits_match_steering_constants(self):
        assert f"{MAX_EXTERNAL_STEERS_PER_LEAF} external steer per leaf" in RUN_GUIDANCE
        assert f"{MAX_EXTERNAL_STEERS_PER_RUN} per run" in RUN_GUIDANCE
        assert f"{MAX_CORRECTIONS_PER_LEAF} CUMULATIVE corrections per leaf" in RUN_GUIDANCE
        assert "schema-retry" in RUN_GUIDANCE
        assert "DURABLE ceiling across resume/restart" in RUN_GUIDANCE

    def test_outcomes_and_slot_semantics(self):
        for outcome in ("accepted", "read", "discarded", "rejected", "exhausted"):
            assert outcome in RUN_GUIDANCE
        assert "SPENDS the slot" in RUN_GUIDANCE
        assert "RESTORES the slot" in RUN_GUIDANCE
        assert "rolled back" in RUN_GUIDANCE

    def test_sup01_workaround_and_structural_escape(self):
        assert "SUP-01" in RUN_GUIDANCE
        assert "WORKAROUND" in RUN_GUIDANCE
        assert "GLOBAL" in RUN_GUIDANCE  # the run-level brake
        assert "STRUCTURAL" in RUN_GUIDANCE
        assert "workflow_cancel" in RUN_GUIDANCE
        assert "SMALL CAUSAL correction" in RUN_GUIDANCE

    def test_tool_surface_named(self):
        # The guidance must name the call shape the agent will actually make.
        assert "workflow_steer(run_id, sub_id, segment_id, attempt, turn, text)" in RUN_GUIDANCE
        assert "match atomically at enqueue" in RUN_GUIDANCE


# -- SKILL.md mirrors the same contract --------------------------------------


class TestSkillMirror:
    def test_occurrence_and_fail_closed_identity(self, skill_body):
        body = squash(skill_body)
        assert "live execution occurrence" in body
        assert "only that exact observed occurrence" in body
        for refused in ("stale", "ambiguous", "cache replay", "durable-only", "another process"):
            assert refused in body, refused
        assert "segment_id" in body
        assert "attempt" in body and "turn" in body
        assert "match atomically at enqueue" in body

    def test_queued_semantics(self, skill_body):
        body = squash(skill_body)
        assert "not read and not delivery" in body
        assert "between loop iterations" in body
        assert "frozen prompt" in body
        assert "never preempts" in body

    def test_limits_and_outcomes(self, skill_body):
        body = squash(skill_body)
        assert f"{MAX_EXTERNAL_STEERS_PER_LEAF} external steer per leaf" in body
        assert f"{MAX_EXTERNAL_STEERS_PER_RUN} per run" in body
        assert f"{MAX_CORRECTIONS_PER_LEAF} cumulative corrections per leaf" in body
        assert "durable across resume/restart" in body
        assert "crash after durable reservation is fail-closed" in body
        for outcome in ("`accepted`", "`read`", "`discarded`", "`rejected`", "`exhausted`"):
            assert outcome in body
        assert "spends** the slot" in body
        assert "restores** the slot" in body

    def test_workaround_doctrine(self, skill_body):
        body = squash(skill_body)
        assert "WORKAROUND" in body
        assert "SUP-01" in body
        assert "**`workflow_cancel` + a corrected re-run**" in body
        assert "small causal correction" in body


# -- the audit vocabulary is CLOSED over exactly these five ------------------


class TestAuditVocabulary:
    def test_event_types_closed_set_contains_exactly_five_steering(self):
        from lohra.workflow.audit import _EVENT_TYPES

        steering = {t for t in _EVENT_TYPES if t.startswith("steering.")}
        assert steering == {
            "steering.accepted",
            "steering.read",
            "steering.discarded",
            "steering.rejected",
            "steering.exhausted",
        }

    def test_every_outcome_taught_by_the_guidance_is_a_closed_type(self):
        from lohra.workflow.audit import _EVENT_TYPES

        for outcome in ("accepted", "read", "discarded", "rejected", "exhausted"):
            assert f"steering.{outcome}" in _EVENT_TYPES

    def test_exhaustion_reasons_survive_metadata_sanitization(self):
        from lohra.workflow.audit import _safe_metadata

        for reason in ("leaf_limit", "run_limit", "correction_limit"):
            assert _safe_metadata(reason, key="reason") == reason


# -- the three surfaces agree with the constants -----------------------------


class TestCrossSurfaceConsistency:
    def test_tool_description_agrees_with_guidance_limits(self):
        from lohra.workflow.tools import _STEER_SCHEMA

        desc = _STEER_SCHEMA["description"]
        assert f"{MAX_EXTERNAL_STEERS_PER_LEAF} external steer per" in desc
        assert f"{MAX_EXTERNAL_STEERS_PER_RUN} per run" in desc
        assert f"{MAX_CORRECTIONS_PER_LEAF} total corrections" in desc
        assert "durably across resume/restart" in desc

    def test_guidance_and_skill_teach_the_same_limits(self, skill_body):
        assert f"{MAX_EXTERNAL_STEERS_PER_LEAF} external steer per leaf" in squash(RUN_GUIDANCE)
        assert f"{MAX_EXTERNAL_STEERS_PER_LEAF} external steer per leaf" in squash(skill_body)

    def test_steering_module_docstring_pins_the_contract(self):
        src = (Path("lohra") / "workflow" / "steering.py").read_text(encoding="utf-8")
        for constant in (
            "MAX_CORRECTIONS_PER_LEAF",
            "MAX_EXTERNAL_STEERS_PER_LEAF",
            "MAX_EXTERNAL_STEERS_PER_RUN",
        ):
            assert re.search(rf"^{constant} = \d+$", src, re.M), constant
