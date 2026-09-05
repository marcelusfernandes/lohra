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
- ``model_not_found`` — the provider does not HAVE this model (#85). The same
  determinism reached from the other side: the request names a slug that does
  not exist, so every later leaf on that route asks for the same nonexistent
  thing and is told so again. The harness may CORRECT it first — one
  substitution from the operator's tier map, `model_substitution.py`, checked
  before this pause — but a correction that is not available, not authorized or
  itself dead leaves the route as gone as a refused credential, and the run must
  stop rather than schedule every remaining node onto it.
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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lohra.providers.errors import AUTH_FAILED, MODEL_NOT_FOUND
from lohra.workflow.leaf_retry import LEAF_ERROR, NO_RESPAWN_KINDS
from lohra.workflow.nodes import NODE_SPECS, ROUTING_FIELDS

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
# The kinds a DECLARED series can never be evidence about. Subtracted BY NAME
# rather than left to `NO_RESPAWN_KINDS` alone: both exclusions here are
# deterministic-within-the-route deaths that pause on their own grounds
# (branch (a) below), and deriving this set from the re-spawn set means the
# next kind added there silently makes its pause unreachable — which is
# exactly what #85 did to `model_not_found` before this line named it.
_NEVER_A_SERIES = frozenset(NO_RESPAWN_KINDS - {AUTH_FAILED, MODEL_NOT_FOUND})

# Appended when the dead route lives one level down, inside a `workflow` node's
# TEMPLATE. Without it the remedy is a lie by omission: the agent would go
# looking for a `model:` field in the spec it is about to resume with, and the
# node that died is not in that document at all.
NESTED_ROUTE_TAIL = (
    " — CAVEAT: this node lives inside the nested template {template!r}, NOT in "
    "the spec a resume sends. Editing the parent spec cannot move that route: "
    "the template itself has to change (workflow_templates), or the human does. "
    "A checkpoint_answers route answer for it is REFUSED for the same reason."
)

ROUTE_FAULT_HINT = (
    "a route this run depends on is DEAD, so the run stopped instead of "
    "scheduling more nodes onto it; nothing resumes it on its own (no "
    "resume_at, no auto-resume). Read the 'route' field — it names the "
    "provider, the model, the node and the failure kind. ANSWER IT BY COMMAND, "
    "on the SAME run: run_workflow(resume_run_id=..., checkpoint_answers="
    '{"<route.node_id>": {"provider": "...", "model": "...", "effort": "..." '
    "(optional)}}) re-routes that ONE node in the spec already on file, and "
    'checkpoint_answers={"<route.node_id>": "abort"} cancels the run instead. '
    "Every completed cell replays from the cache, so only the node that died is "
    "paid for again. You may choose the new route YOURSELF only within the SAME "
    "provider and the SAME credential/billing route, with catalog evidence and "
    "never onto a costlier model; a different provider, a different billing "
    "route, an unknown or higher cost, and any refused credential (401/403) are "
    "the HUMAN's decision — report the dead route and the case for a change, and "
    "pass back only what the human answered verbatim. The answer moves ONLY that "
    "node's provider/model/effort: to change anything else, send the whole "
    "adapted spec instead (run_workflow(resume_run_id=..., spec=<adapted "
    "spec>)) — one channel per resume, never both. If the dead node routes by "
    "'tier', answer with BOTH 'provider' and 'model': a model alone leaves the "
    "tier's provider in place and the node dies on the same route again"
)


# What the OPERATOR's route envelope (#63) had to say about this dead route,
# appended to the remedy so the reader is not left wondering whether the file
# they wrote was even consulted. Keyed by the outcome word ``routes.py`` owns.
ENVELOPE_TAILS = {
    "no_envelope": (
        " — the operator's route envelope (~/.lohra/workflow_routes.json) lists "
        "no alternative for this route, so nothing was tried automatically. "
        "Adding one there pre-authorizes the harness to move a node off this "
        "route WITHOUT waking anybody; a spec can never grant that."
    ),
    "unpriced": (
        " — the operator's route envelope lists an alternative, and it was "
        "REFUSED because a per-token list price is missing on one side or the "
        "other (a dynamic provider with no entry in ~/.lohra/pricing.json, or a "
        "subscription plan, which has no per-token bill at all). The harness "
        "never re-routes onto a bill it cannot read: price both routes in "
        "pricing.json, or answer this pause yourself."
    ),
    "costlier": (
        " — the operator's route envelope lists an alternative and it bills MORE "
        "per token than the route that died, so it was refused: the envelope may "
        "only make a run cheaper or equal, never costlier. Listing a cheaper "
        "route, or answering this pause, are the two ways forward."
    ),
    "gated": (
        " — the operator's route envelope lists an alternative and its provider "
        "could not be built: no credential for it, or (for openai-codex) no "
        "subscription opted in. The envelope never escalates into a provider the "
        "operator has not enabled — `lohra auth enable` and a key are the "
        "operator's to give."
    ),
    "exhausted": (
        " — the operator's route envelope was tried and its allowance is SPENT "
        "for this run: one pre-authorized fallback per dead route, and "
        "max_fallbacks_per_run for the run as a whole. A second automatic guess "
        "at the same dead route is exactly what that bound exists to refuse, "
        "because every other node still pointed at this route needs the same fix "
        "— send an adapted spec that moves them all, or answer this pause."
    ),
    "ineligible": (
        " — the operator's route envelope does not move a node of this TYPE. v1 "
        "re-routes an `agent` node only: its cell key carries the resolved route "
        "unconditionally, so the move lands in a NEW cell and leaves the one the "
        "dead route wrote exactly as replayable as it was. A rigor node keys on "
        "its routing only when it DECLARES any — without one, re-routing it "
        "would poison that cell — and even with one, its strategy owns its own "
        "leaf loop, which v1 does not re-enter. Author the route and adapt the "
        "spec, or answer this pause."
    ),
    "nested": (
        " — the dead route lives one level down, inside a `workflow` node's "
        "TEMPLATE, and the operator's route envelope will not move it: that node "
        "is not in the spec this run persists, so no resume could carry a new "
        "route forward and calling it re-routed would be a false fact. Adapt the "
        "template (workflow_templates) — the same refusal a checkpoint_answers "
        "route answer gets, for the same reason."
    ),
    "run_stopped": (
        " — this run was ALREADY stopping when the route died (another node's "
        "pause, or a cancel), so the operator's route envelope was not spent on "
        "it: a fresh leaf bought for work nothing will schedule is not a remedy. "
        "Read the reason the run actually stopped; the envelope is still intact "
        "for the resume."
    ),
}


def route_fault_hint(payload: dict[str, Any] | None) -> str:
    """The remedy, told for THIS pause: the doctrine, plus the caveat a nested
    route needs. One function, so every consumer (the rollup, the durable line,
    ``watch``) says the same thing about the same run."""
    payload = payload or {}
    template = payload.get("template")
    hint = (
        ROUTE_FAULT_HINT + NESTED_ROUTE_TAIL.format(template=template)
        if template
        else ROUTE_FAULT_HINT
    )
    # ...and what the OPERATOR's envelope said, when there was one to ask (#63).
    # A word, looked up — never prose from the payload, which would let an
    # unknown outcome write whatever it liked into the remedy.
    return hint + ENVELOPE_TAILS.get(payload.get("envelope"), "")


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
    if error_kind in (AUTH_FAILED, MODEL_NOT_FOUND):
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
    last_error: Any = None,
) -> dict[str, Any]:
    """What the pause carries into the durable line.

    The route comes from what the leaf REALLY ran on (the core's collect dict),
    never from ``node.fields``: a node on the run's default names no model, and
    a payload that omits the dead route is exactly the payload nobody can act
    on. ``node_id`` is the node an author could EDIT — the pipeline's own id, not
    the ``pl#3#0`` cell that died inside it (that one stays in ``cause``, where
    it says which item and stage, not where to look).

    ``cause`` is the verdict text and ``last_error`` the provider's own words for
    the death that ended it. Both are carried, because on the exhaustion branch
    the verdict alone is a tautology — "re-spawns exhausted" says the series ran
    out, never WHY — and ``error_kind`` is ``None`` for exactly the failures the
    classifier could not name, which is the case that needs the prose most.
    Bounded like every other quoted cause."""
    return {
        "node_id": node_id,
        "provider": provider,
        "model": model,
        "error_kind": error_kind,
        "cause": str(cause)[:MAX_FAULT_CAUSE_CHARS],
        "last_error": str(last_error)[:MAX_FAULT_CAUSE_CHARS] if last_error else None,
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


# --- the ANSWER: a route_fault pause is a checkpoint answered by COMMAND ------
#
# Decisão 1 do dono (#43): a pausa deixa de exigir que o agente re-autore a spec
# inteira. O humano responde uma ROTA (ou "abort") e o harness adapta o nó morto
# na spec PERSISTIDA. The channel is the one that already exists —
# ``checkpoint_answers={node_id: answer}`` — because a route_fault pause IS a
# checkpoint in every way that matters: it waits for a decision no amount of
# time supplies, the authority is the human's, and the agent's job is to relay
# the answer verbatim. A second, parallel parameter would have been a second
# vocabulary for the same act.

# The word that cancels instead of re-routing. Only ever special on a run PAUSED
# at ``route_fault`` and only for the node that pause names: to a ``checkpoint``
# node "abort" is an ordinary human answer and stays one.
ROUTE_ABORT = "abort"

# What a route answer may move: the concrete route, and nothing else.
#
# ``tier`` is deliberately NOT here even though it is routing vocabulary. A tier
# is resolved through the operator's map and an explicit ``model`` on the node
# WINS over it — so answering ``{"tier": "big"}`` for a node that already names a
# model would change the spec, change nothing about the route, and re-pay the
# node to die exactly as before. A silent no-op is the one answer shape this
# channel must not accept.
ROUTE_ANSWER_FIELDS = ("provider", "model", "effort")

# ...and of those, the two that actually constitute a ROUTE. An answer that moves
# only ``effort`` leaves the run pointed at the route that just refused it.
ROUTE_IDENTITY_FIELDS = ("provider", "model")


@dataclass(frozen=True)
class RouteAnswer:
    """A human's verbatim reply to a ``route_fault`` pause: a new route, or
    ``abort``. Never both, never neither — ``parse_route_answer`` returns a
    didactic string instead of building an ambiguous one."""

    abort: bool = False
    route: Mapping[str, str] = field(default_factory=dict)


def looks_like_route_answer(answer: Any) -> bool:
    """Is this the SHAPE of a route answer? (Not: is it a valid one.)

    Used to tell an answer meant for a dead route from an answer meant for a
    ``checkpoint`` node, so a route answer sent to a run that is not paused on a
    route can be refused with the reason instead of being cached as some node's
    output. Deliberately shape-only and deliberately narrow: a bare string is
    never enough (a human answering a checkpoint may well write "abort")."""
    return (
        isinstance(answer, dict)
        and bool(answer)
        and all(key in ROUTE_ANSWER_FIELDS for key in answer)
    )


def parse_route_answer(answer: Any) -> RouteAnswer | str:
    """``RouteAnswer`` | a didactic refusal.

    Never raises and never guesses: everything this rejects, it rejects by
    naming what was sent and what the two accepted shapes are. The refusals are
    the whole point of the channel — an answer the harness half-understood would
    re-pay a node to die on a route nobody chose."""
    if isinstance(answer, str):
        if answer.strip().lower() == ROUTE_ABORT:
            return RouteAnswer(abort=True)
        return (
            f"{answer.strip()[:80]!r} is not an answer this pause understands. A "
            'route_fault pause takes either a route — {"provider": "...", '
            '"model": "...", "effort": "..." (optional)} — or the single word '
            '"abort" to cancel the run.'
        )
    if not isinstance(answer, dict):
        return (
            f"a route_fault answer must be an object naming the new route or the "
            f'word "abort"; got {type(answer).__name__}'
        )
    unknown = [key for key in answer if key not in ROUTE_ANSWER_FIELDS]
    if unknown:
        tier = " ('tier' is resolved by the operator's map and is ignored where the "
        tier += "node already names a model — answer with the concrete 'model'/'provider')"
        return (
            f"a route answer may only move {', '.join(ROUTE_ANSWER_FIELDS)} on the "
            f"node that died; {', '.join(sorted(map(str, unknown)))} is not routing"
            f"{tier if 'tier' in unknown else ''}. To change anything else "
            "(prompt, depends_on, schema, the DAG itself) send the whole adapted "
            "spec instead: run_workflow(resume_run_id=..., spec=<adapted spec>)."
        )
    bad = [
        key
        for key, value in answer.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if bad:
        return (
            f"every field of a route answer must be a non-empty string; "
            f"{', '.join(sorted(bad))} is not"
        )
    route = {key: str(value).strip() for key, value in answer.items()}
    if not any(key in route for key in ROUTE_IDENTITY_FIELDS):
        return (
            "a route answer has to name the new route: give 'provider' and/or "
            "'model' (an 'effort' alone leaves the run on the route that just "
            'died). To cancel the run instead, answer "abort".'
        )
    return RouteAnswer(route=route)


def same_dead_route(route: Mapping[str, str], payload: Mapping[str, Any]) -> bool:
    """Would this answer put the node back on the route that just died?

    Only ever True when the payload actually NAMES both halves of the dead route:
    a leaf that ran on the run's default may report no provider or no model, and
    refusing an answer on the strength of a route nobody can name would be a
    guess — the one thing this channel refuses to make."""
    if any(payload.get(field_name) is None for field_name in ROUTE_IDENTITY_FIELDS):
        return False
    if "effort" in route and payload.get("error_kind") != AUTH_FAILED:
        # A knob really did move, and for an unclassified death that is enough to
        # be worth one more attempt — the same call at another effort is a
        # different call. NOT for ``auth_failed``: a credential the provider
        # refused is refused at every effort, so letting the same
        # provider/model through on a changed knob would buy a second, certain
        # death at full price. The one shape whose verdict is deterministic
        # within a run is the one shape that gets no second chance.
        return False
    return all(
        route.get(field_name, payload.get(field_name)) == payload.get(field_name)
        for field_name in ROUTE_IDENTITY_FIELDS
    )


def nested_route_refusal(template: Any) -> str:
    """A dead route one level down, inside a ``workflow`` node's TEMPLATE (v1:
    no). The parent spec a resume sends does not contain that node at all, so
    there is nothing here to edit — saying "re-routed" would be a false fact."""
    return (
        f"the route lives in template {template!r}; adapt the template "
        "(workflow_templates) and resume — the node that died is not in the spec "
        "this run persists, so a checkpoint_answers route answer cannot move it"
    )


def apply_route_answer(
    spec_dict: Any, node_id: str, route: Mapping[str, str]
) -> dict[str, Any] | str:
    """The persisted spec with ONE node re-routed — a NEW dict, or a refusal.

    Immutable by construction: the incoming spec, its node list and the node
    itself are all left exactly as they were, and the caller gets a fresh object
    graph down to the edited node. The run's own line is what carries it
    forward, so a mutation here would silently rewrite the document a concurrent
    reader (``cache_preview``, the rollup, another process's ``status``) is
    holding.

    Refuses, didactically, anything the edit could not honestly make:
    - a node id no top-level node carries (the payload always names an AUTHORED
      id, so this is a spec that moved under the pause);
    - a node TYPE that has no routing at all (``pipeline`` stages and
      ``parallel`` branches are prompts, not nodes: the validator would reject
      the field, and accepting it here would only move the error);
    - a RIGOR node that declares no routing of its own — see below."""
    if not isinstance(spec_dict, dict) or not isinstance(spec_dict.get("nodes"), list):
        return (
            "the spec on file for this run has no 'nodes' list to re-route — "
            "resume with an explicit adapted spec instead"
        )
    nodes: list[Any] = spec_dict["nodes"]
    index = next(
        (
            position
            for position, node in enumerate(nodes)
            if isinstance(node, dict) and node.get("id") == node_id
        ),
        None,
    )
    if index is None:
        return (
            f"no node {node_id!r} in the spec on file for this run, so there is "
            "nothing to re-route — resume with an explicit adapted spec instead"
        )
    node = nodes[index]
    node_type = str(node.get("type") or "")
    type_spec = NODE_SPECS.get(node_type)
    if type_spec is None or not set(ROUTING_FIELDS) <= type_spec.field_names():
        return (
            f"node {node_id!r} is a {node_type or 'typeless'!r} node and takes no "
            "routing fields at all — its leaves run on the session's own model "
            "(a pipeline's stages and a parallel's branches are prompts, not "
            "nodes). Re-route the node that OWNS them, or send an adapted spec."
        )
    if node_type != "agent" and not any(name in node for name in ROUTING_FIELDS):
        # POLICY, not a cache fact: adding a route here WOULD move the cell
        # identity (``strategies._routing_identity`` keys a rigor node's cell on
        # its routing exactly when the node declares any). What it would not do
        # is answer the question honestly — a rigor node that declared nothing
        # ran on the RUN's default, so the route named in the payload is the
        # session's, not something this spec ever chose, and re-routing this one
        # node leaves every other default-routed node pointed at the same dead
        # route. Authoring the route explicitly is the act that makes it a
        # decision, and that is a spec, not an answer.
        return (
            f"node {node_id!r} is a {node_type} node that declares no route of "
            "its own — its leaves ran on the RUN's default, which is what died. "
            "A one-node answer cannot fix a default every other node also uses: "
            "resume with an explicit adapted spec that authors the route "
            "(run_workflow(resume_run_id=..., spec=<adapted spec>))."
        )
    return {
        **spec_dict,
        "nodes": [*nodes[:index], {**node, **route}, *nodes[index + 1 :]],
    }


def apply_reroutes(spec_dict: Any, reroutes: Any) -> Any:
    """The persisted spec with every ENVELOPE re-route of this stretch folded in
    (#63) — a NEW dict, or the original when there is nothing to fold.

    The in-memory half of a re-route dies with the stretch; this is what makes it
    survive one. Without it a run that was re-routed and then paused for some
    OTHER reason would resume onto the dead route: the cache would replay the
    cells the new route produced (their hash carries it), and every cell still to
    come would be scheduled on the route the operator had already replaced.

    Reuses ``apply_route_answer`` verbatim rather than editing nodes here, so
    both channels — the human's ``checkpoint_answers`` and the operator's
    envelope — put a route into a spec through exactly one piece of code, with
    exactly one set of refusals. A refusal is SKIPPED rather than raised: the
    re-route already happened, its fault already says so, and failing the persist
    over it would throw away the whole line. It cannot happen for an entry the
    engine produced (only a top-level ``agent`` node is ever offered one), which
    is precisely why it is safe to treat as unreachable rather than as an error.

    Idempotent: applying the same route twice yields an equal document, so a
    stretch that persists several times folds the same re-routes each time.
    """
    if not isinstance(reroutes, list) or not reroutes:
        return spec_dict
    applied = spec_dict
    for entry in reroutes:
        if not isinstance(entry, dict):
            continue
        node_id = entry.get("node_id")
        route = {
            key: value for key, value in entry.items()
            if key in ROUTE_ANSWER_FIELDS and isinstance(value, str)
        }
        if not isinstance(node_id, str) or not route:
            continue
        adapted = apply_route_answer(applied, node_id, route)
        if isinstance(adapted, dict):
            applied = adapted
    return applied


def reroute_fault(
    node_id: str, payload: Mapping[str, Any], route: Mapping[str, str]
) -> str:
    """The run's own record that this node's route was MOVED, and through what.

    It names the CHANNEL, never an author. The harness observes a resume, not
    who typed it — and ``ROUTE_FAULT_HINT`` explicitly lets the agent pick the
    new route itself inside the same provider and billing route, so "a human
    chose this" would be a fact the record cannot check and is sometimes plainly
    false. What it CAN say is true of every re-route: it arrived through
    ``checkpoint_answers``, and the harness never picked it.

    Carried in ``prior_faults`` (like the orphan-recovery fault), so it is
    reported for the whole run and discounted from the verdict: the re-route is
    the remedy, not a lesson about the spec, and a run that recovers on it must
    still be able to seal ``complete``."""
    was = route_label(payload.get("provider"), payload.get("model"))
    now = route_label(
        route.get("provider", payload.get("provider")),
        route.get("model", payload.get("model")),
    )
    effort = f" (effort: {route['effort']})" if "effort" in route else ""
    return (
        f"{node_id}: re-routed after a route_fault pause — {was} -> {now}{effort}; "
        "answered through checkpoint_answers (the command channel), never chosen "
        "by the harness"
    )


def route_change(
    payload: Mapping[str, Any], route: Mapping[str, str]
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """The same two facts ``reroute_fault`` says in prose — as DATA (#64).

    ``before`` is the route the node actually ran on (from the pause payload,
    which reports what the leaf USED, not what the spec declared); ``after`` is
    that route with the answer applied. A field the answer did not move is
    carried forward rather than dropped: an answer naming only a ``model``
    leaves the node on the provider it already had, and an ``after`` that hid
    that would read as a route half of which nobody knows.

    Pure and channel-agnostic, so #63's envelope can derive its event from the
    same function the command channel uses — one derivation, one meaning.
    """
    before = {name: payload.get(name) for name in ROUTE_ANSWER_FIELDS}
    after = {name: route.get(name, payload.get(name)) for name in ROUTE_ANSWER_FIELDS}
    return before, after


def abort_fault(node_id: str, payload: Mapping[str, Any]) -> str:
    """...and the record that the answer was to STOP instead. ``cancelled``, not
    ``failed``: nothing about the spec was refuted — somebody read the dead route
    and decided the run was not worth another one.

    Names the CHANNEL rather than an author, for the reason ``reroute_fault``
    gives. Names the TEMPLATE too when the dead route was one level down: the
    node id is namespaced (``sub[ref]:node``) and points at nothing in the spec
    this run persists, so a reader of the cancelled line would otherwise have to
    guess where that route lived."""
    label = route_label(payload.get("provider"), payload.get("model"))
    template = payload.get("template")
    where = f" (inside template {template!r})" if template else ""
    return (
        f"{node_id}: route_fault answered \"abort\" through checkpoint_answers "
        f"(the command channel) — {label}{where} stays dead and the run was "
        "cancelled instead of re-routed"
    )
