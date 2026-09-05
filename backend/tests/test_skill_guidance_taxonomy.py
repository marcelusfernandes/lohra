"""Wave 9 E3 anti-drift: skill guidance must use the agency/environment
taxonomy decided in #54, same as the memory tool's guidance
(test_memory_guidance_taxonomy.py). A skill created to "overcome a
non-obvious error" must not encode a workaround for the author's own
mistake as if it were an environment quirk.

Owner's decision (#54, 2026-09-05): a nonexistent model slug chosen by the
author is `agency`, not an environment quirk. A provider quota or timeout is
`environment`. Without evidence of the environment, the default is agency.
"""

from lohra.skills.tool import _MANAGE_GUIDANCE


def test_guidance_does_not_invite_environment_quirk_framing():
    assert "environment quirk" not in _MANAGE_GUIDANCE
    assert "quirk" not in _MANAGE_GUIDANCE


def test_guidance_names_agency_class_with_example():
    assert "agency" in _MANAGE_GUIDANCE
    assert "model" in _MANAGE_GUIDANCE
    assert "doesn't exist" in _MANAGE_GUIDANCE or "does not exist" in _MANAGE_GUIDANCE


def test_guidance_names_environment_class_with_example():
    assert "environment" in _MANAGE_GUIDANCE
    assert "quota" in _MANAGE_GUIDANCE or "timeout" in _MANAGE_GUIDANCE


def test_guidance_states_default_to_agency_without_environment_evidence():
    assert "evidence" in _MANAGE_GUIDANCE and "agency" in _MANAGE_GUIDANCE
    lowered = _MANAGE_GUIDANCE.lower()
    assert "no evidence" in lowered or "without evidence" in lowered


def test_guidance_still_describes_create_update_delete():
    assert "5+ steps" in _MANAGE_GUIDANCE
    assert "non-obvious errors" in _MANAGE_GUIDANCE
    assert "stale or wrong" in _MANAGE_GUIDANCE
    assert "home skills only" in _MANAGE_GUIDANCE
    assert "scope='project'" in _MANAGE_GUIDANCE
    assert "concise, reusable instructions" in _MANAGE_GUIDANCE
