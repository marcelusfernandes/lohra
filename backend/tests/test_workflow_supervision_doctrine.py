"""Anti-drift contract for the active-supervision doctrine (SUP-01).

Pins the CURRENT state of the workflow tool guidance and the workflow-authoring
skill so a wording cleanup cannot silently turn bounded recovery into either
report-only behaviour or unbounded autonomy.

Surfaces under test:
  - ``lohra.workflow.tools``: ``RUN_GUIDANCE`` (the run_workflow description),
    ``_STATUS_SCHEMA['description']``, and the ``_RUN_SCHEMA`` parameter
    descriptions for ``checkpoint_answers`` and ``token_budget``.
  - the ``workflow-authoring`` builtin skill body.

Deliberately NOT tested: full prose, capitalisation, or sentence shape. Where a
phrase is load-bearing we match on lowercased text; everything else is matched
on short, stable fragments. Numeric limits are imported from production code —
if the constant moves, this file says so loudly.

Note on "attempts": ``MAX_RESUME_ATTEMPTS`` is a CAP on the run's own
auto-resume retries, never a guarantee that 5 resumes happen — the run stops
as soon as the provider recovers, and the skill must say "up to". These tests
pin the cap-vs-guarantee framing.
"""

from pathlib import Path

import pytest

from lohra.agent.limits import MAX_AUTHORED_MAX_ITERATIONS
from lohra.skills.store import SkillStore, builtin_root
from lohra.workflow import tools as wt
from lohra.workflow.autoresume import MAX_RESUME_ATTEMPTS, MIN_RESUME_DELAY
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.runstate_store import pause_fields

SKILL_NAME = "workflow-authoring"

# --- the surfaces, combined --------------------------------------------------

STATUS_DESC = wt._STATUS_SCHEMA["description"]
CHECKPOINT_ANSWERS_DESC = wt._RUN_SCHEMA["parameters"]["properties"]["checkpoint_answers"][
    "description"
]
TOKEN_BUDGET_DESC = wt._RUN_SCHEMA["parameters"]["properties"]["token_budget"]["description"]
TOKEN_BUDGET_HINT = pause_fields("paused", TOKEN_BUDGET_EXHAUSTED, None, 0, None)["hint"]
CHECKPOINT_HINT = pause_fields(
    "paused", CHECKPOINT, None, 0, {"node_id": "approve", "prompt": "Proceed?"}
)["hint"]

# The combined TOOL surface: everything the model reads straight off the tool
# schemas. Negative checks run against this, not against the skill (the skill
# legitimately discusses resuming with a bigger cap as a *human* decision).
TOOL_SURFACE = "\n".join(
    (
        wt.RUN_GUIDANCE,
        STATUS_DESC,
        CHECKPOINT_ANSWERS_DESC,
        TOKEN_BUDGET_DESC,
        TOKEN_BUDGET_HINT,
        CHECKPOINT_HINT,
    )
)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


@pytest.fixture
def skill_body() -> str:
    skill = SkillStore(Path("/nonexistent-home"), builtin_roots=(builtin_root(),)).get(SKILL_NAME)
    assert skill is not None
    return skill.body


# --- production constants the doctrine leans on ------------------------------


def test_production_limits_are_what_the_doctrine_quotes():
    """If these move, every '128' / '5 attempts' / 'at least a minute' claim in
    the guidance and the skill is now a lie. Update the prose with the code."""
    assert MAX_AUTHORED_MAX_ITERATIONS == 128
    assert MAX_RESUME_ATTEMPTS == 5
    assert MIN_RESUME_DELAY == 60.0


def test_guidance_quotes_the_real_ceiling_and_formula():
    """max_iterations: raise once to min(N+4, 128) — the ceiling quoted in the
    guidance must be the harness's real one."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "min(n+4,128)" in g.replace(" ", "")
    assert "1-128" in g
    assert "128 is the ceiling" in g


def test_skill_names_the_schema_cap_from_production(skill_body):
    s = _norm(skill_body)
    assert "absolute schema cap of 128" in s
    assert "capped at **128**" in s


def test_skill_says_5_attempts_is_a_cap_not_a_guarantee(skill_body):
    """'up to 5 auto-resume attempts' — a cap on retries, not a promise of five.
    A guarantee-shaped phrasing ('5 attempts', 'will retry 5 times') would make
    the agent wait for resumes that never come."""
    s = _norm(skill_body)
    assert "up to 5 attempts" in s
    assert "5 auto-resume attempts" in s


# --- the loop, the brakes, and what counts -----------------------------------


def test_guidance_teaches_the_loop_and_its_direction():
    g = _norm(wt.RUN_GUIDANCE)
    assert "watch -> diagnose -> adapt -> resume" in g


def test_guidance_pins_attempt_brakes():
    """ONE per (run, cause, target); at most 3 per run."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "one attempt per (run, cause," in g
    assert "at most 3 per run" in g


def test_guidance_requires_recording_before_and_after_a_workaround():
    """Anti-drift: a workaround is a judged act, not a flail. BEFORE adapting,
    the diagnosis, the (run, cause, target) key, the change and its incremental
    cost estimate go to the trace/log; AFTER it settles, the outcome,
    fingerprint and actual cost are recorded. And every autonomous adaptation
    must be reversible + budgeted + recorded — unrecorded or irreversible
    'fixes' are out of bounds."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "record a workaround" in g
    assert "before adapting" in g
    assert "the key (run, cause," in g
    assert "estimate" in g
    assert "after it settles" in g
    assert "fingerprint" in g
    assert "reversible, budgeted and recorded" in g


def test_guidance_defines_k2_as_run_level_across_successive_workarounds():
    """K=2 is RUN-LEVEL, not per-key: fingerprint after each SETTLED workaround;
    two SUCCESSIVE settled workarounds whose own post-fingerprint equals their
    own pre-fingerprint open a GLOBAL brake. Polls mid-run don't count. The per-key cap
    stays at one attempt — K=2 is a second, wider brake, not a replacement."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "k=2 brake is run-level" in g
    assert "settled workarounds" in g
    assert "pre-workaround progress fingerprint" in g
    assert "post-workaround fingerprint equals its own pre-fingerprint" in g
    assert "two successive no-progress workarounds" in g
    assert "global brake" in g
    assert "still running don't count" in g
    assert "per-key cap stays at one attempt" in g


def test_guidance_gates_slug_correction_on_list_models_and_same_route():
    """A bad model SLUG — never an 'invalid model/provider swap' — may be
    corrected only after list_models, on the same provider + credential/billing
    route, and only with evidence of a fixed-price subscription or with pricing
    metadata/preauthorization showing the cost is not higher.
    Since list_models reports no prices, an unpreauthorized API-key route
    escalates. Crossing provider/route stays human."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "bad model slug" in g
    assert "only after list_models" in g
    assert "same provider" in g
    assert "credential/billing route" in g
    assert "fixed-price subscription" in g
    assert "configured default" not in g
    assert "preauthorization" in g
    assert "does not report prices" in g
    assert "escalates" in g
    assert "human call" in g
    # The old over-broad framing must not come back.
    assert "invalid model/provider" not in g


def test_guidance_scopes_optional_provider_parameter_fixes():
    """An unsupported OPTIONAL provider parameter (e.g. 'effort') may be dropped
    only when the user never required it and the goal is unchanged; otherwise
    it is a human decision."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "optional provider parameter" in g
    assert "never required it" in g
    assert "human" in g


def test_guidance_pins_the_max_iterations_formula_and_the_ceiling_escalation():
    g = _norm(wt.RUN_GUIDANCE)
    assert "raised once to min(n+4, 128)" in g.replace("min(n + 4, 128)", "min(n+4, 128)")
    assert "never more" in g
    # The formula applies ONLY below the ceiling: N >= 128 escalates, no resume.
    assert "only when n < 128" in g
    assert "no resume" in g
    assert "escalate to a" in g


# --- human boundaries --------------------------------------------------------


def test_guidance_names_the_always_human_list():
    g = _norm(wt.RUN_GUIDANCE)
    for phrase in (
        "always human",
        "token budget",
        "checkpoint answers",
        "credentials",
        "permissions",
        "scope",
        "irreversible actions",
        "provider or billing route",
    ):
        assert phrase in g, phrase


def test_token_budget_param_desc_says_raising_is_human_only():
    d = _norm(TOKEN_BUDGET_DESC)
    assert "human authorization" in d
    assert "never do it on your own judgment" in d


def test_token_budget_increase_is_human_everywhere_on_the_tool_surface():
    """The agent may size the initial cap while authoring, but no surface may
    let it increase an already-started run's cap on its own."""
    for surface in (wt.RUN_GUIDANCE, STATUS_DESC):
        s = _norm(surface)
        assert "human decision" in s or "human's call" in s
    guidance = _norm(wt.RUN_GUIDANCE)
    assert "any increase to a run's token budget" in guidance
    assert "never an agent one" in guidance


def test_checkpoint_default_is_human_supplied_or_absent():
    """Anti-drift: the checkpoint node is a HUMAN gate. A 'default' may exist
    only when the human operator explicitly supplied it before the run; the
    agent never invents a default. The old loose phrasing ('or give it a
    default') must never return — it reads as license to author one."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "checkpoint: ask a human" in g
    assert "every answer was supplied" in g
    assert "only when the human explicitly gave you that default" in g
    assert "never invents a default" in g
    # the drifted phrase, exactly:
    assert "or give it a default" not in g
    assert "give it a" not in g
    # the status surface carries the same rule
    assert "never author an answer or a default yourself" in _norm(STATUS_DESC)


def test_checkpoint_answers_param_desc_requires_verbatim_human_answers():
    d = _norm(CHECKPOINT_ANSWERS_DESC)
    assert "human supplied verbatim" in d
    assert "never infers" in d
    assert "paraphrases" in d
    assert "invents" in d
    assert "never" in d and "'default' answer" in d


# --- quota: resume_at semantics ----------------------------------------------


def test_guidance_respects_resume_at_and_escalates_when_null():
    g = _norm(wt.RUN_GUIDANCE)
    assert "respect its resume_at" in g
    assert "wait it out" in g
    assert "never launch a competing resume" in g
    assert "resume_at is null" in g
    assert "escalate to a human" in g


def test_status_schema_carries_the_same_quota_semantics():
    d = _norm(STATUS_DESC)
    assert "if resume_at is set, wait it out" in d
    assert "do not compete with the run's own auto-resume" in d
    assert "escalate to a human" in d


def test_quota_brief_never_tells_the_agent_to_resume_early():
    """Anti-drift: earlier drafts said 'resume it early' / 'resume with a bigger
    cap' on quota. The tool surface must keep the agent WAITING (quota
    auto-resumes itself) and must never frame a competing or early resume as
    the agent's move."""
    combined = _norm(TOOL_SURFACE)
    assert "resume it early" not in combined
    assert "resume with a bigger cap" not in combined


def test_dynamic_pause_hints_preserve_the_human_boundary(skill_body):
    budget = _norm(TOKEN_BUDGET_HINT)
    checkpoint = _norm(CHECKPOINT_HINT)
    assert "human" in budget and "authorize" in budget
    assert "report" in budget
    assert all(word in checkpoint for word in ("human", "supplied", "verbatim"))
    assert "never invents" in checkpoint
    assert '"default": "go"' not in skill_body
    assert "configured default" not in _norm(skill_body)


def test_no_surface_makes_the_agent_the_human_watchpoint():
    """Anti-drift: the stderr/CLI note is for the OPERATOR, phrased so the
    human isn't 'waiting on YOU'. None of the tool surfaces may say that."""
    assert "waiting on you" not in _norm(TOOL_SURFACE)


# --- skill contracts that stay (SUP-01 + cost allowance + circuit brake) -----


def test_skill_pins_the_agent_human_causal_boundary(skill_body):
    agent_owned = (
        "stale process",
        "model slug",
        "provider parameter",
        "max_iterations",
        "transient provider",
    )
    human_owned = (
        "token budget",
        "checkpoint",
        "scope",
        "credentials",
        "irreversible",
    )
    for phrase in (*agent_owned, *human_owned):
        assert phrase in skill_body, phrase


def test_skill_pins_attempt_non_progress_and_cost_brakes(skill_body):
    # whitespace-robust checks (case-insensitive for prose)
    s = _norm(skill_body)
    for phrase in (
        "one automatic workaround per `(run, cause, target)`",
        "post-workaround fingerprint equals its own pre-workaround fingerprint",
        "`min(6,000 tokens, 25% of the original `token_budget`)`",
        "no explicit `token_budget`",
    ):
        assert phrase in s, phrase
    # circuit states are case-sensitive
    for phrase in ("OPEN", "HALF-OPEN", "CLOSED"):
        assert phrase in skill_body, phrase


def test_skill_forbids_blind_retries_and_names_existing_harness_limits(skill_body):
    required = (
        "zero blind retries",
        "5 auto-resume attempts",
        "absolute schema cap of 128",
        "does not launch a competing resume",
    )
    s = _norm(skill_body)
    for phrase in required:
        assert phrase in s, phrase


def test_skill_builds_the_progress_fingerprint_without_the_cost(skill_body):
    """Tokens spent is a cost line, not a progress line — a wedged run spends
    plenty and moves nothing."""
    for phrase in (
        "status/reason",
        "done/running/pending",
        "per-node states",
        "faults",
        "cost is tracked separately",
    ):
        assert phrase in skill_body, phrase


def test_skill_keeps_the_circuit_brake_behavioural(skill_body):
    """The skill describes a circuit brake in policy terms only — no surface may
    claim the harness implements it."""
    for phrase in ("CLOSED", "OPEN", "HALF-OPEN"):
        assert phrase in skill_body
    assert "nothing in the harness enforces" in _norm(skill_body)


def test_skill_keeps_quota_off_the_agent_desk(skill_body):
    s = _norm(skill_body)
    assert "does not launch a competing resume" in s
    assert "watch, not to pile on" in s


def test_guidance_says_the_doctrine_is_behavioural():
    g = _norm(wt.RUN_GUIDANCE)
    assert "behavioural" in g
    assert "nothing in the harness checks" in g
    assert "workflow-authoring skill" in g


def test_skill_points_back_at_the_doctrine_and_the_guidance_defers_to_it():
    """Single source of truth: the guidance defers to the skill for the full
    table; the skill is the one that elaborates."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "consult it rather than improvising" in g


def test_durable_status_keeps_budget_context_for_human_decision():
    """After process loss, the operator must still see the cap and remaining
    budget needed to decide whether a larger cap is justified."""
    from lohra.workflow.runstate_store import DurableRun, durable_rollup

    out = durable_rollup(
        DurableRun(run_id="r", status="paused", token_budget=1_000),
        spent_total=451,
        stale=False,
    )
    assert out["token_budget"] == {
        "total": 1_000, "spent": 451, "remaining": 549, "overrun": 0
    }


def test_skill_preserves_agent_authorship_of_the_initial_cap(skill_body):
    s = _norm(skill_body)
    assert "estimate before you author" in s
    assert "pass a conservative initial `token_budget`" in s
    assert "increase after the run exists" in s


def test_skill_model_fix_is_same_route_and_fail_closed_without_cost_evidence(skill_body):
    s = _norm(skill_body)
    for phrase in (
        "same provider",
        "credential/billing route",
        "fixed-price subscription",
        "pricing metadata",
        "preauthorization",
        "list_models does not expose pricing",
    ):
        assert phrase in s, phrase


def test_quota_and_non_quota_transient_failures_are_not_conflated():
    combined = _norm(TOOL_SURFACE)
    assert "non-quota transient provider failure" in combined
    assert "quota_exhausted" in combined
    assert "past" in combined and "poll once" in combined


def test_workaround_record_channel_and_loss_policy_are_explicit(skill_body):
    combined = _norm(wt.RUN_GUIDANCE + "\n" + skill_body)
    assert "current conversation" in combined
    assert "record is unavailable" in combined
    assert "escalate" in combined


def test_quota_cap_is_never_worded_as_five_guaranteed_retries(skill_body):
    s = _norm(skill_body)
    for forbidden in ("will retry 5 times", "guarantees 5", "always retries 5"):
        assert forbidden not in s


def test_spent_budget_error_escalates_instead_of_authoring_a_larger_cap():
    from lohra.workflow.spend import refuse_spent_budget

    error = _norm(refuse_spent_budget("r", 300, 451)["error"])
    assert "human" in error and "authorize" in error
    assert "report" in error
    assert "spent * 2" not in error
    assert "resume it with a bigger one" not in error


def test_checkpoint_error_requests_a_verbatim_human_answer_not_your_answer():
    from lohra.workflow.launch import checkpoint_answers
    from lohra.workflow.runstate_store import DurableRun

    _, error = checkpoint_answers(
        answers=None,
        explicit_spec=False,
        resume_run_id="r",
        prior=DurableRun(
            run_id="r",
            status="paused",
            pause_reason=CHECKPOINT,
            checkpoint={"node_id": "approve", "prompt": "Proceed?"},
        ),
    )
    text = _norm(error)
    assert "human" in text and "verbatim" in text
    assert "<your answer>" not in text


# --- SUP-01: degraded / failed / pointer to skill in the status schema --------

def test_status_schema_covers_degraded_status_with_action():
    """The status schema is the ONE surface the agent reads when supervising.
    It must name 'degraded' with an instruction: read faults before trusting
    outputs and say what is missing."""
    d = _norm(STATUS_DESC)
    assert "degraded" in d
    assert "faults" in d
    assert "before trusting" in d


def test_status_schema_covers_degraded_faults_total_on_resumed_runs():
    """A resumed run reports faults_total (everything since launch) plus faults
    (current stretch). The guidance must name both."""
    d = _norm(STATUS_DESC)
    assert "faults_total" in d


def test_status_schema_covers_failed_status_with_action():
    """status 'failed' means every node nulled. The agent must re-author, not
    paper over it with a summary pretending nothing went wrong."""
    d = _norm(STATUS_DESC)
    assert "failed" in d
    assert "re-author" in d
    assert "paper over" in d


def test_status_schema_points_to_workflow_authoring_skill_for_full_doctrine():
    """The status schema must point the agent at the workflow-authoring skill
    for the full doctrine (loop, brakes, agent vs human boundaries). Without
    this pointer, an agent that only polls workflow_status never discovers the
    supervisory doctrine at all."""
    d = _norm(STATUS_DESC)
    assert "workflow-authoring skill" in d
