"""Wave 9 E3 anti-drift: memory guidance must use the agency/environment
taxonomy decided in #54, never invite "environment quirk" framing.

Owner's decision (#54, 2026-09-05): a nonexistent model slug chosen by the
author is `agency`, not an environment quirk. A provider quota or timeout is
`environment`. Without evidence of the environment, the default is agency.
"""

from lohra.memory.tool import MEMORY_GUIDANCE


def test_guidance_does_not_invite_environment_quirk_framing():
    assert "environment quirk" not in MEMORY_GUIDANCE


def test_guidance_names_agency_class_with_example():
    assert "agency" in MEMORY_GUIDANCE
    assert "model" in MEMORY_GUIDANCE
    assert "doesn't exist" in MEMORY_GUIDANCE or "does not exist" in MEMORY_GUIDANCE


def test_guidance_names_environment_class_with_example():
    assert "environment" in MEMORY_GUIDANCE
    assert "quota" in MEMORY_GUIDANCE or "timeout" in MEMORY_GUIDANCE


def test_guidance_states_default_to_agency_without_environment_evidence():
    assert "evidence" in MEMORY_GUIDANCE and "agency" in MEMORY_GUIDANCE
    lowered = MEMORY_GUIDANCE.lower()
    assert "no evidence" in lowered or "without evidence" in lowered


def test_guidance_still_forbids_task_progress_and_todos():
    assert "task progress" in MEMORY_GUIDANCE
    assert "TODOs" in MEMORY_GUIDANCE
    assert "skills" in MEMORY_GUIDANCE


def test_guidance_still_requires_declarative_facts():
    assert "declarative facts" in MEMORY_GUIDANCE
    assert "not instructions to yourself" in MEMORY_GUIDANCE
