"""SUP-04 anti-drift contracts for the cheapest safe pivot path."""

from pathlib import Path

import pytest

from lohra.skills.store import SkillStore, builtin_root
from lohra.workflow.tools import RUN_GUIDANCE


@pytest.fixture(scope="module")
def skill_body() -> str:
    store = SkillStore(Path("/nonexistent-home"), builtin_roots=(builtin_root(),))
    skill = store.get("workflow-authoring")
    assert skill is not None
    return skill.body


def test_tool_guidance_names_adapted_same_run_resume_and_cache_identity():
    assert "ADAPTED-SPEC PIVOT" in RUN_GUIDANCE
    assert "same resume_run_id" in RUN_GUIDANCE
    assert "meta.name and meta.version" in RUN_GUIDANCE
    assert "new run_id reuses NO cells" in RUN_GUIDANCE


def test_tool_guidance_keeps_route_and_budget_decisions_human():
    pivot = RUN_GUIDANCE.split("ADAPTED-SPEC PIVOT", 1)[1]
    assert "same provider and credential/billing route" in pivot
    assert "never raise token_budget" in pivot
    assert "provider or billing route" in pivot
    assert "human" in pivot


def test_skill_compares_adapted_resume_new_run_and_steering(skill_body):
    pivot = " ".join(skill_body.split("### Pivoting a stopped run", 1)[1].replace("**", "").split())
    assert "Adapted same-run resume" in pivot
    assert "Fresh run" in pivot and "zero cell-cache reuse" in pivot
    assert "Steering" in pivot and "frozen route" in pivot


def test_skill_documents_quota_ownership_and_prompt_cache_limit(skill_body):
    pivot = " ".join(skill_body.split("### Pivoting a stopped run", 1)[1].replace("**", "").split())
    assert "auto-resume owns the wait" in pivot
    assert "never launch a competing resume" in pivot
    assert "may preserve the provider-side prompt cache" in pivot
    assert "retention is provider-specific" in pivot
    assert "provider-reported historical cache reads" in pivot
    assert "cannot predict whether a future request is still warm" in pivot


def test_skill_pins_cell_identity_and_granularity(skill_body):
    pivot = " ".join(skill_body.split("### Pivoting a stopped run", 1)[1].replace("**", "").split())
    assert "meta.name` and `meta.version" in pivot
    assert "change only the affected node fields" in pivot
    assert "Each aggregate node" in pivot
    assert "nested workflow node is deterministic control, not a parent cache cell" in pivot
    assert "pipeline" in pivot and "item, stage" in pivot


def test_skill_requires_supervision_record_around_the_pivot(skill_body):
    pivot = " ".join(skill_body.split("### Pivoting a stopped run", 1)[1].replace("**", "").split())
    assert "pre-pivot fingerprint" in pivot
    assert "reused versus re-executed" in pivot
    assert "actual incremental tokens" in pivot
