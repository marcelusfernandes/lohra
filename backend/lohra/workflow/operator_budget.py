"""The token ceiling the OPERATOR pre-authorized for every workflow run (#47).

``token_budget`` is optional and the AGENT picks it: omitted, a run is unlimited
— and in headless orchestration (``lohra chat --json``, one-shot) nobody is
there to notice. This module is the operator's brake: one number, in tokens,
resolved ONCE per process from the CLI flag ``--token-budget-cap`` or the env
var ``LOHRA_TOKEN_BUDGET_CAP``, and applied to every run the process launches.

It does not bend the doctrine, it instantiates it. "A budget is a HUMAN
decision, never an agent one" always meant a human authorizing the number; a cap
is that authorization given IN ADVANCE, by the human who started the process.
Which is why the cap is a CEILING and never a floor:

- no cap → byte-identical to before (no clamp, no new field anywhere);
- cap alone → the run inherits it as its ceiling;
- both → ``min``, so the agent may ask for LESS but never for more;
- on a resume too: the agent asking again with a larger number is clamped again,
  because the operator sits ABOVE the agent, and the agent is the only "human"
  a resume has.

Resolution follows the house pattern (``resolve_limits``): flag > env > default,
and an unreadable value warns and falls back rather than inventing a ceiling or
killing the process.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)

ENV_TOKEN_BUDGET_CAP = "LOHRA_TOKEN_BUDGET_CAP"
FLAG_TOKEN_BUDGET_CAP = "--token-budget-cap"

# WHERE the ceiling a run actually runs under came from. Provenance, not policy:
# the agent reads it to know whether asking for more would change anything.
# ORIGIN_SPEC vs ORIGIN_INHERITED matters because they are different facts: a
# resume that asked for nothing runs under the ceiling the LEDGER holds — the
# clamped number a previous stretch persisted — not under anything this call
# authored, and labelling that "spec" would credit the agent with a decision it
# never made this launch.
ORIGIN_SPEC = "spec"
ORIGIN_INHERITED = "inherited"
SOURCE_SPEC = ORIGIN_SPEC  # the agent asked for less than the cap; its number stands
SOURCE_INHERITED = ORIGIN_INHERITED  # a resume asked for nothing; the ledger's stands
SOURCE_OPERATOR = "operator_cap"  # nothing was asked at all; the cap is it
SOURCE_CLAMPED = "min(spec,operator_cap)"  # the agent asked for more; clamped

# What a run paused on the operator's ceiling has to be told: the remedy is
# OUTSIDE this process's agent. Saying "ask a human for a bigger token_budget"
# here would send the agent into the exact loop the raise-only rule exists to
# kill — the larger number is clamped back and re-pauses on the first spawn.
#
# Two numbers, stated separately and never merged: {total} is THIS RUN's ceiling
# (persisted, possibly written by another process under another cap) and {cap} is
# THIS PROCESS's operator ceiling. Claiming the run "spent the operator's
# ceiling" would assert a fact nobody here observed — with total=5 and cap=3 the
# run spent 5, not 3.
OPERATOR_PAUSE_HINT = (
    "this run's ceiling is {total} tokens and this process's operator ceiling is "
    "{cap}, so a resume launched from here is clamped to {cap} and would pause "
    "again on the first spawn; nothing resumes it on its own — report the "
    "token_budget/spend fields and the case for more to the HUMAN OPERATOR, who "
    f"raises or unsets {FLAG_TOKEN_BUDGET_CAP} (lohra chat) / "
    f"{ENV_TOKEN_BUDGET_CAP} and relaunches. A relaunch alone does NOT unstick "
    "it: the resume must also carry run_workflow(resume_run_id=..., "
    "token_budget=<above what the run already spent>), because the ceiling it "
    "would otherwise inherit is the spent one"
)

# The run is at or over the operator's ceiling: EVERY launch from this process is
# refused before it spawns, whatever paused it. A quota pause here is the zombie
# case — its auto-resume fires, is refused, and never increments an attempt, so a
# `resume_at` in the reply would be a promise this process cannot keep.
OPERATOR_SPENT_HINT = (
    "this run has already spent {spent} tokens, at or over this process's "
    "operator ceiling of {cap}: every launch from here is clamped to {cap} and "
    "REFUSED before it spawns — an auto-resume, if one was armed, fails the same "
    "way and never gets anywhere, so nothing is coming. The HUMAN OPERATOR "
    f"raises or unsets {FLAG_TOKEN_BUDGET_CAP} (lohra chat) / "
    f"{ENV_TOKEN_BUDGET_CAP} and relaunches; the resume then also needs "
    "run_workflow(resume_run_id=..., token_budget=<above the {spent} already "
    "spent>), since the ceiling it would inherit is the spent one"
)

# The same fact on the refusal path (``refuse_spent_budget``): a resume asked for
# a ceiling the operator's cap will not grant, and the run has already spent it.
OPERATOR_REFUSAL = (
    "the operator's pre-authorized ceiling for this process is {cap} tokens, so a "
    "larger 'token_budget' is clamped back to it and would pause again on the "
    f"first spawn — only the HUMAN OPERATOR can raise it ({FLAG_TOKEN_BUDGET_CAP} "
    f"on lohra chat, or {ENV_TOKEN_BUDGET_CAP}); report the numbers and wait"
)


@dataclass(frozen=True)
class AppliedBudget:
    """The ceiling a launch runs under, plus where it came from.

    ``as_dict`` is None whenever no cap is in force: a process with no operator
    ceiling reports exactly what it reported before this existed."""

    total: int | None
    source: str | None
    operator_cap: int | None

    def as_dict(self) -> dict | None:
        if self.operator_cap is None:
            return None
        return {
            "total": self.total,
            "source": self.source,
            "operator_cap": self.operator_cap,
        }


def apply_operator_cap(
    token_budget: int | None, cap: int | None, *, origin: str = ORIGIN_SPEC
) -> AppliedBudget:
    """Clamp the ceiling a launch asked for to the operator's, if there is one.

    Pure and total: no cap gives back the request untouched (the byte-identical
    path), and the source labels which of the two numbers is actually in force —
    ``spec`` when the agent asked for less, so the reader is not told the
    operator is barring something it is not.

    ``origin`` says where ``token_budget`` came from: the spec of THIS call, or
    the ledger a resume inherits from. It only shapes the label, never the
    number."""
    if cap is None:
        return AppliedBudget(total=token_budget, source=None, operator_cap=None)
    if token_budget is None:
        return AppliedBudget(total=cap, source=SOURCE_OPERATOR, operator_cap=cap)
    if cap < token_budget:
        return AppliedBudget(
            total=cap, source=f"min({origin},operator_cap)", operator_cap=cap
        )
    return AppliedBudget(total=token_budget, source=origin, operator_cap=cap)


def cap_binds(total: int | None, cap: int | None) -> bool:
    """True when the operator's ceiling is the one no agent-side number can pass.

    The discriminator for every remedy text: with a cap of 200k over a run whose
    ceiling is 150k, a human CAN still authorize a bigger token_budget and it
    will work — the ordinary hint is right there. Only ``cap <= total`` means
    the agent has nothing left to ask for."""
    return cap is not None and total is not None and cap <= total


def normalize_operator_cap(cap: int | None, *, where: str) -> int | None:
    """A ceiling built by hand (a library caller, a test, an embedder) crossing
    into the service. ``resolve_operator_token_cap`` already refuses a bad flag
    or env value, but the constructor is its own boundary: 0 or a negative would
    read as "cap everything at nothing" and pause every run on its first spawn.
    Fail-open like every other resolution here — warn, and run uncapped."""
    if cap is None:
        return None
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        logger.warning(
            "ignoring operator token cap %r at %s: must be a whole number of "
            "tokens >= 1; running with no operator cap",
            cap,
            where,
        )
        return None
    return cap


def resolve_operator_token_cap(
    flag: int | None = None, env: Mapping[str, str] | None = None
) -> int | None:
    """The operator's ceiling: the flag, else the env var, else no ceiling.

    Same precedence and the same fail-open as ``resolve_limits``/``LOHRA_AUDIT``:
    an unreadable value warns and is IGNORED — the resolution then continues down
    the chain, so a bad flag falls back to the env var rather than wiping out a
    ceiling the operator really did set. Inventing a ceiling out of a typo would
    pause real runs; refusing to start would make a stray env var fatal.
    """
    environ = os.environ if env is None else env
    if flag is not None:
        if isinstance(flag, bool) or not isinstance(flag, int) or flag < 1:
            # Ignored, then FALL THROUGH to the env var — never straight to "no
            # ceiling". A typo'd flag must not silently unset a cap the operator
            # deliberately put in the environment: of the two ways to be wrong,
            # only one lets a run spend more than the human authorized.
            logger.warning(
                "ignoring %s=%r: must be a whole number of tokens >= 1; "
                "falling back to %s / no operator cap",
                FLAG_TOKEN_BUDGET_CAP,
                flag,
                ENV_TOKEN_BUDGET_CAP,
            )
        else:
            return flag
    raw = environ.get(ENV_TOKEN_BUDGET_CAP)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ignoring %s=%r: not an integer; no operator cap", ENV_TOKEN_BUDGET_CAP, raw
        )
        return None
    if value < 1:
        logger.warning(
            "ignoring %s=%r: must be >= 1; no operator cap", ENV_TOKEN_BUDGET_CAP, raw
        )
        return None
    return value
