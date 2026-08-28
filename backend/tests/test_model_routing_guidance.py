"""Anti-drift contract: the AUTHORING guidance actually teaches model routing.

Fatia C ships no runtime code — it teaches the agent to use the catalog that
fatias A/B already built. So the only thing that can rot is the TEXT, and the
only way it can rot is silently. These tests pin the claims that would mislead
the author if they drifted:

1. the guidance names the tool that EXISTS — pinned against the registry key, so
   a rename of ``list_models`` cannot leave guidance and skill pointing at a tool
   nobody registers while the suite stays green;
2. every NUMBER and every slug the skill quotes is derived here from the code's
   own constants (the "default 8 that was really 90" lesson: a doc number typed
   by hand is a doc number that goes stale);
3. the two claims the runtime makes FALSE if the text drifts back: a refused
   provider nulls the node instead of faulting it, and a confirmation checkpoint
   must declare no ``default`` (a default is auto-answered on a plain resume).

None of this claims the harness validates a slug at authoring time — it does
not; ``model``/``effort``/``provider`` are free fields.
"""

import json
import re
from pathlib import Path

import pytest

from lohra.catalog.tool import DEFAULT_LIMIT, MAX_LIMIT, register_list_models_tool_schema
from lohra.providers.base import get_provider_profile
from lohra.skills.store import SkillStore, builtin_root
from lohra.subscription.provider import CODEX_PROVIDER, DEFAULT_CODEX_MODEL
from lohra.tools.registry import registry
from lohra.workflow.tools import RUN_GUIDANCE

SKILL_NAME = "workflow-authoring"
# The one API-key slug the skill's mixed-provider example names literally.
EXAMPLE_SLUG = "claude-sonnet-4-6"
EXAMPLE_PROVIDER = "anthropic"
# The example spec these tests make claims about, found by its meta name.
EXAMPLE_RUN = "mixed-provider-notes"
_JSON_FENCE = re.compile(r"```json\n(.*?)```", re.DOTALL)


@pytest.fixture
def skill_body() -> str:
    store = SkillStore(Path("/nonexistent-home"), extra_roots=(), builtin_roots=(builtin_root(),))
    skill = store.get(SKILL_NAME)
    assert skill is not None, "the builtin workflow-authoring skill must ship in the package"
    return skill.body


def _example_spec(skill_body: str) -> dict:
    """The mixed-provider example, parsed out of the skill by its meta name."""
    for raw in _JSON_FENCE.findall(skill_body):
        spec = json.loads(raw)
        if spec.get("meta", {}).get("name") == EXAMPLE_RUN:
            return spec
    raise AssertionError(f"the skill no longer ships the {EXAMPLE_RUN!r} example")


# --- (a) the guidance points at the tool that exists ------------------------


def test_the_documented_tool_name_is_the_registry_key():
    # The buraco this file exists to close: a rename of the tool would leave both
    # texts naming a tool nothing registers. Importing the module is not enough —
    # registration is an explicit call.
    register_list_models_tool_schema()
    assert "list_models" in registry.names_in_toolset("catalog")


def test_run_guidance_tells_the_author_to_call_list_models():
    assert "list_models" in RUN_GUIDANCE


def test_run_guidance_says_the_catalog_is_not_an_allow_list():
    # The fields are free text; only `tier` is a closed enum. An author who reads
    # the list as an allow-list would refuse a perfectly legal slug.
    assert "allow-list" in RUN_GUIDANCE


def test_run_guidance_mentions_mixing_providers_in_one_dag():
    # Pin the CLAIM, not just the provider name: one DAG, different providers.
    assert "openai-codex" in RUN_GUIDANCE
    assert "SAME DAG" in RUN_GUIDANCE
    assert "DIFFERENT providers" in RUN_GUIDANCE


def test_skill_documents_list_models(skill_body):
    # Backticked: the skill documents the TOOL, not the words in passing.
    assert "`list_models`" in skill_body


def test_skill_documents_the_subscription_provider(skill_body):
    assert "openai-codex" in skill_body


# --- (b) every quoted number and slug comes from the code's constants -------


def test_skill_quotes_the_real_catalog_limits(skill_body):
    # Derived, never typed: if DEFAULT_LIMIT/MAX_LIMIT move, this fails before a
    # reader is taught a number the tool no longer honours.
    assert f"**{DEFAULT_LIMIT}**" in skill_body, "the skill must quote the real default limit"
    assert f"**{MAX_LIMIT}**" in skill_body, "the skill must quote the real max limit"


def test_the_example_slug_is_a_model_this_install_declares(skill_body):
    # A pin on the EXAMPLE, not a claim about validation: nothing checks `model`
    # at authoring time. This only keeps the skill from teaching a dead slug.
    assert EXAMPLE_SLUG in skill_body
    profile = get_provider_profile(EXAMPLE_PROVIDER)
    assert profile is not None
    assert EXAMPLE_SLUG in profile.fallback_models


def test_the_skill_names_the_real_cross_provider_codex_default(skill_body):
    # `configure_for` falls back to fallback_models[0] for a cross-provider node
    # with no `model` — a FIXED slug, not `codex_default_model()` (what the
    # catalog reports). The skill warns about exactly that gap, so the slug it
    # names has to be the one the pool would actually use.
    # `openai-codex` is not in the general provider registry — the pool builds
    # CODEX_PROVIDER for it directly, so that is the profile to read.
    assert CODEX_PROVIDER.name == "openai-codex"
    assert CODEX_PROVIDER.fallback_models[0] == DEFAULT_CODEX_MODEL
    assert DEFAULT_CODEX_MODEL in skill_body


# --- (c) the two claims the runtime would make false ------------------------


def test_the_skill_says_a_refused_provider_nulls_instead_of_faulting(skill_body):
    # strategies.run_agent catches ProviderError and returns None WITHOUT
    # record_fault. Saying "fault" here would send the author looking in the one
    # place the cause never lands.
    section = skill_body[skill_body.index("### Choosing models from the catalog") :]
    section = section[: section.index("### ", 5)]
    assert "`faults`" in section and "`null`" in section


def test_the_confirmation_checkpoint_declares_no_default(skill_body):
    # launch._resolve auto-answers a pending checkpoint that declared a `default`
    # on a plain resume_run_id. A gate that exists to make a human confirm the
    # routing must therefore declare none.
    spec = _example_spec(skill_body)
    checkpoints = [n for n in spec["nodes"] if n["type"] == "checkpoint"]
    assert checkpoints, "the confirmation example must still hold a checkpoint"
    for node in checkpoints:
        assert "default" not in node, (
            f"checkpoint {node['id']!r} declares a default — a plain resume would auto-approve it"
        )


def test_the_cross_provider_node_names_its_model(skill_body):
    # The section's own lesson: a cross-provider node that omits `model` silently
    # takes a fixed fallback slug. The example must not teach the omission.
    spec = _example_spec(skill_body)
    for node in spec["nodes"]:
        if node.get("provider"):
            assert node.get("model"), f"node {node['id']!r} names a provider but no model"
