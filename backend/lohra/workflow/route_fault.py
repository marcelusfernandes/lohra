"""Pausing a run whose ROUTE died, instead of degrading in silence (#43, opção C).

The run that motivated this (``lohra-notion-v4``) lost its Anthropic balance
mid-flight and then kept going for four more nodes: every one of them was
scheduled onto a route already known to be dead, and 55% of the run's tokens
were spent outside any surviving cell. Nothing was hidden — each dead leaf wrote
its fault — but ``degraded`` is a verdict you read AFTER the run, and by then the
money is gone. A pause is the same information delivered while it still buys
something: the finished cells stay in the resume cache, nothing else is
scheduled, and a human (or the agent, inside its own authority) decides what the
next route is.

**The trigger is deliberately NARROW**, because the failure mode of getting this
wrong is worse than the one it fixes: a run stopped by one transient 502 costs
more than a run that degraded. Exactly two shapes qualify:

- ``auth_failed`` — the provider refused this route's credential or its scope.
  The client is built once per route and cached for the life of the pool, so
  within one run the refusal is DETERMINISTIC: every later leaf on that route
  presents the same key and gets the same answer. There is nothing to wait for
  and nothing to retry (``leaf_retry.NO_RESPAWN_KINDS``), which is precisely why
  the run must stop instead of proving it N more times.
- a DECLARED series of same-route re-spawns that exhausted (E1). The author
  wrote ``retries`` on that node, the harness spent every attempt it was given
  on the same route, and all of them died. That is not one unlucky call: it is
  the evidence a bounded retry exists to produce.

Everything else keeps today's behaviour — fault, null, ``degraded``. A single
generic death on a node that never wrote ``retries`` is the transient-502 case,
and the harness has no honest way to tell it from a permanent one (a balance
failure arrives as an unclassified HTTP 400; ``providers/errors.py`` forbids
regex over the provider's prose). So it does not guess.

**Zero new authority.** The pause never re-routes anything, arms no auto-resume
(the allow-list stays quota-only) and carries no ``resume_at``: nothing about a
dead route fixes itself with time. What it produces is a durable payload naming
the dead route and a hint that repeats the SUP-04 boundary verbatim — the agent
may adapt the spec itself only inside the same provider and the same
credential/billing route and never onto a costlier model; a different provider,
a different billing route, an unknown-or-higher cost or a refused credential is
the HUMAN's call. Resuming with the adapted spec on the SAME ``run_id`` replays
every completed cell, so the remedy costs only the node that died.
"""

from __future__ import annotations

from typing import Any

from lohra.providers.errors import AUTH_FAILED
from lohra.workflow.leaf_retry import LEAF_ERROR, NO_RESPAWN_KINDS

# The pause reason. A fifth sibling of quota_exhausted / token_budget_exhausted /
# checkpoint / user_requested: the same resumable stop, and again its own remedy
# — this one is a ROUTE, and no amount of waiting supplies it.
ROUTE_FAULT = "route_fault"

# A dead leaf's cause is quoted into the fault AND into the durable payload the
# agent reads back; bound it so one huge stack trace can't drown either.
MAX_FAULT_CAUSE_CHARS = 200

# Kinds that can never reach the "declared series exhausted" branch, restated
# here as a GUARD rather than as an assumption: each already owns a remedy that
# is not this pause (quota pauses itself and auto-resumes, both timeouts name
# their own knob, a budget waits on a human). ``auth_failed`` is deliberately
# NOT among them — it is branch (a), the one shape that pauses on its own.
_NEVER_A_SERIES = frozenset(NO_RESPAWN_KINDS - {AUTH_FAILED})

ROUTE_FAULT_HINT = (
    "a route this run depends on is DEAD, so the run stopped instead of "
    "scheduling more nodes onto it; nothing resumes it on its own (no "
    "resume_at, no auto-resume). Read the 'route' field — it names the "
    "provider, the model, the node and the failure kind. You may adapt the "
    "spec YOURSELF only within the SAME provider and the SAME "
    "credential/billing route, with catalog evidence and never onto a costlier "
    "model; a different provider, a different billing route, an unknown or "
    "higher cost, and any refused credential (401/403) are the HUMAN's "
    "decision — report the dead route and the case for a change, and act only "
    "on what the human answers verbatim. Either way resume the SAME run with "
    "run_workflow(resume_run_id=..., spec=<the adapted spec>): every completed "
    "cell replays from the cache, so only the node that died is paid for again"
)


def should_pause_on_route_fault(
    node: Any,
    status: str | None,
    error_kind: str | None,
    attempts_declared: bool = False,
    exhausted: bool = False,
) -> bool:
    """Is this death evidence that the ROUTE is gone, not that a call was unlucky?

    Pure: a status, a classified kind and two facts about the series that ran.
    Never a regex over prose — a tool result quoting "401 unauthorized" back at
    us must not stop a healthy run any more than it may pause one.

    ``node`` is consulted only for branch (b): the series must belong to a node
    whose author actually WROTE ``retries``, and this re-reads that from the
    node instead of trusting the caller's word for it (fail-closed — the caller
    that computes ``attempts_declared`` is one refactor away from computing it
    for the wrong node). ``None`` is the honest value for a caller claiming no
    series at all, which is the auth path.
    """
    if status != LEAF_ERROR:
        # Not a leaf that died carrying an exception: an empty answer, a
        # cancelled leaf, a leaf still running. Fail-closed on anything
        # unrecognised — a pause is never a guess.
        return False
    if error_kind == AUTH_FAILED:
        return True
    if not (attempts_declared and exhausted):
        return False
    if error_kind in _NEVER_A_SERIES:
        return False
    fields = getattr(node, "fields", None)
    return isinstance(fields, dict) and "retries" in fields


def route_label(provider: str | None, model: str | None) -> str:
    """The dead route, as a reader can say it out loud.

    Either half can be absent: a node that named no ``provider``/``model`` ran on
    the run's own default, and the core reports what it actually used. Saying
    "None/None" would name nothing at all, so say that instead."""
    if provider and model:
        return f"{provider}/{model}"
    return provider or model or "the run's default route"


def route_fault_payload(
    *,
    node_id: str,
    provider: str | None,
    model: str | None,
    error_kind: str | None,
    cause: str,
) -> dict[str, Any]:
    """What the pause carries into the durable line.

    The route comes from what the leaf REALLY ran on (the core's collect dict),
    never from ``node.fields``: a node on the run's default names no model, and
    a payload that omits the dead route is exactly the payload nobody can act
    on. ``cause`` is the verdict text, bounded like every other quoted cause."""
    return {
        "node_id": node_id,
        "provider": provider,
        "model": model,
        "error_kind": error_kind,
        "cause": str(cause)[:MAX_FAULT_CAUSE_CHARS],
    }


def route_fault_summary(detail: str, payload: dict[str, Any]) -> str:
    """The pause's OWN fault line: the verdict that stopped the run, plus the
    route it stopped on.

    One fault, not two — it is recorded through ``_record_pause_fault``, so a
    later stretch discounts it the way it discounts a quota pause. Counting it
    as a real failure would mark every route-fault resume ``prior_degraded``
    forever and teach ``library`` that the SHAPE was at fault, which is the
    silent degradation this pause exists to replace."""
    label = route_label(payload.get("provider"), payload.get("model"))
    return (
        f"{detail} — run paused (route_fault): {label} is not usable for this "
        "run, so no further node was scheduled onto it"
    )
