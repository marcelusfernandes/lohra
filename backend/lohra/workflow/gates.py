"""Gating node types (M7 fatia B): `gate`, `completeness_check`, `checkpoint`.

Three nodes that all answer the same question — "is this good enough to go on?"
— at three different levels of trust:

- ``gate`` asks a MODEL: draft, review, revise, bounded. The rigor of a
  judge_panel at a fraction of the width, for work that has one right shape
  rather than many candidate answers.
- ``completeness_check`` asks a model one fixed question ("what is missing?")
  and returns a fixed shape, so a ``loop_until_dry`` body or a downstream ref
  can branch on it without every author re-inventing the schema.
- ``checkpoint`` asks a HUMAN: it spawns nothing at all, pauses the run
  resumably, and reports what it is waiting for. The answer arrives on the
  resume and is cached like any completion, so the same question is never asked
  twice.

This module deliberately imports neither ``engine`` nor ``strategies`` AT MODULE
LEVEL: it is imported BY strategies (to fill the STRATEGIES table) and its pause
reason is imported by the engine, exactly the way ``budget`` owns
TOKEN_BUDGET_EXHAUSTED. The routing helpers these two gates share with the other
rigor nodes therefore come in through a LOCAL import inside the functions — the
one direction that keeps the import DAG acyclic (the same move ``strategies``
itself makes for ``engine``).
"""

from __future__ import annotations

from typing import Any

from lohra.workflow.namespacing import checkpoint_key
from lohra.workflow.nodes import checkpoint_accepts, checkpoint_on_reject, gate_attempts
from lohra.workflow.prompts import as_text, branch_prompt, strict_prompt, with_schema_hint
from lohra.workflow.route_fault import MAX_FAULT_CAUSE_CHARS
from lohra.workflow.validation import is_empty_output

# The pause reason for a run stopped at a HUMAN gate (WF-10) — a fourth sibling
# of quota_exhausted / token_budget_exhausted / user_requested. Same resumable
# stop, and again a different remedy: only an answer moves this one.
CHECKPOINT = "checkpoint"

# Forced verdict for a gate's reviewer leaf.
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "feedback": {"type": "string"}},
    "required": ["ok"],
}
# Forced answer for a completeness critic.
_COMPLETENESS_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["complete", "missing"],
}

# What a rejected draft is told on its FRESH re-spawn. The header is what the
# next attempt is recognised by (a steer would inherit whatever wedged the
# first attempt — the same reasoning as the empty-output retry).
_REVISION = (
    "REVISION REQUESTED — a reviewer rejected your previous answer:\n{feedback}\n"
    "Produce a new answer that fixes this. Do not defend the old one."
)
# A reviewer that could not be read is a REJECTION, never a pass (fail-closed).
_UNREADABLE_VERDICT = (
    "The reviewer did not approve it and gave no usable reason. Try a materially "
    "different answer rather than a reworded one."
)
_EMPTY_DRAFT = "Your previous attempt produced no answer at all. Produce a real one."


def _verdict_prompt(validator: Any, candidate: Any) -> str:
    return (
        f"{as_text(validator)}\n\nCANDIDATE:\n{as_text(candidate)}\n\n"
        'Respond with ONLY JSON: {"ok": <true|false>, "feedback": "<what to fix>"}.'
    )


def _feedback_of(verdict: Any) -> str:
    """What the next draft is told. Fail-closed: an unreadable verdict still
    produces usable instructions instead of an empty steer."""
    if isinstance(verdict, dict):
        feedback = verdict.get("feedback")
        if isinstance(feedback, str) and feedback.strip():
            return feedback
    return _UNREADABLE_VERDICT


def _approved(verdict: Any) -> bool:
    """Only an explicit ``ok: true`` passes. A dead reviewer, a non-dict answer
    or a missing field is a rejection — the same rule ``verify`` uses when every
    skeptic died: silence is not evidence."""
    return isinstance(verdict, dict) and verdict.get("ok") is True


def run_gate(engine: Any, node: Any, context: dict[str, Any]) -> Any:
    """Draft → review → revise until approved or out of attempts (WF-6).

    Each attempt is a FRESH body leaf carrying the reviewer's feedback, then a
    fresh reviewer leaf. Only the APPROVED output is cached: caching a rejected
    draft would freeze the thing the gate exists to prevent into every resume.
    The body supports ``schema``/``schema_ref`` (it is agent-shaped); the routing
    knobs (``model``/``tier``/``effort``/``provider``) live on the NODE and route
    BOTH leaves of every attempt — the draft and the reviewer that judges it —
    because a gate the author cannot route is a gate that always falls back to
    the session's own model."""
    from lohra.workflow.strategies import (  # local: gates.py is imported BY strategies
        _resolve_routing,
        _rigor_configure,
        _routing_identity,
    )

    body = node.fields.get("body") or {}
    prompt = strict_prompt(engine, node.id, branch_prompt(body), context)
    if prompt is None:
        return None  # an upstream null: never draft against the literal "null"
    validator = strict_prompt(engine, node.id, node.fields.get("validator", ""), context)
    if validator is None:
        return None
    schema = engine.resolve_schema(body) if isinstance(body, dict) else None
    attempts = gate_attempts(node.fields)
    model, effort, provider = _resolve_routing(engine, node)
    chash = engine.cell_hash(
        node.id, "gate", prompt, schema, validator, attempts,
        *_routing_identity(node, model, effort, provider),
    )
    hit, cached = engine.cache_lookup(chash, node.id)
    if hit:
        return cached
    # Before the preflight: a gate that cannot be routed spawns nothing, so it
    # must not reserve a width it will never use.
    configure, ok = _rigor_configure(engine, node, model, effort, provider)
    if not ok:
        return None
    # Preflight the whole bounded shape once (the judge_panel rule): every
    # attempt is a body leaf plus a reviewer leaf, and nothing about running
    # half of them is useful if the width was never affordable.
    engine.budget.check_fanout(attempts * 2)

    feedback: str | None = None
    for _attempt in range(attempts):
        text = prompt if feedback is None else f"{prompt}\n\n{_REVISION.format(feedback=feedback)}"
        sub_id = engine.spawn_leaf(
            with_schema_hint(text, schema), configure=configure,
            causal_context=engine.causal_context(
                cell_id=chash, role="gate.draft", attempt=_attempt
            ),
        )
        output = engine.collect_with_schema(sub_id, schema)
        if output is None or is_empty_output(output):
            feedback = _EMPTY_DRAFT  # a dead/silent draft is a failed attempt, not a pass
            continue
        reviewer_id = engine.spawn_leaf(
            _verdict_prompt(validator, output), configure=configure,
            causal_context=engine.causal_context(
                cell_id=chash, role="gate.review", attempt=_attempt
            ),
        )
        verdict = engine.collect_with_schema(reviewer_id, _VERDICT_SCHEMA)
        if _approved(verdict):
            # draft + reviewer: a célula custou DOIS leaves — persistir só o
            # draft faria um resume reconstruir o floor sem o reviewer.
            engine.cache_store(
                chash, node.id, output, engine.leaves_cost([sub_id, reviewer_id]),
                schema=schema, leaf_count=2,
            )
            return output
        feedback = _feedback_of(verdict)
    engine.record_fault(f"gate {node.id}: validator rejected after {attempts} attempt(s)")
    return None


def _completeness_prompt(task: Any, results: Any) -> str:
    return (
        "You are auditing whether a task was fully covered. Name ONLY what is "
        "still missing — never restate what is already there.\n\n"
        f"TASK:\n{as_text(task)}\n\nRESULTS SO FAR:\n{as_text(results)}\n\n"
        'Respond with ONLY JSON: {"complete": <true|false>, "missing": ["<gap>", ...]}.'
    )


def run_completeness_check(engine: Any, node: Any, context: dict[str, Any]) -> Any:
    """One critic leaf answering the fixed ``{complete, missing}`` (spec §8).

    Thin on purpose, like ``verify``: the value is the FIXED shape, so a
    ``loop_until_dry`` body or a downstream ref can branch on it without every
    author re-deriving a schema. A leaf that cannot answer it nulls.

    Cached on the resolved (task, results) like every other cell (WF-28): the
    audit is one leaf, but re-asking it on a resume re-pays for the whole
    harvest it was auditing. The critic is routable like every other rigor node
    (``model``/``tier``/``effort``/``provider`` on the node itself) — a cheap
    auditor over an expensive harvest is the point."""
    from lohra.workflow.strategies import (  # local: gates.py is imported BY strategies
        _resolve_routing,
        _rigor_configure,
        _routing_identity,
    )

    task = strict_prompt(engine, node.id, node.fields.get("task", ""), context)
    if task is None:
        return None
    results = strict_prompt(engine, node.id, node.fields.get("results", ""), context)
    if results is None:
        return None  # nothing audited: never claim a null harvest was complete
    model, effort, provider = _resolve_routing(engine, node)
    chash = engine.cell_hash(
        node.id, "completeness_check", task, results,
        *_routing_identity(node, model, effort, provider),
    )
    hit, cached = engine.cache_lookup(chash, node.id)
    if hit:
        return cached
    configure, ok = _rigor_configure(engine, node, model, effort, provider)
    if not ok:
        return None
    sub_id = engine.spawn_leaf(
        _completeness_prompt(task, results), configure=configure,
        causal_context=engine.causal_context(
            cell_id=chash, role="completeness.review"
        ),
    )
    output = engine.collect_with_schema(sub_id, _COMPLETENESS_SCHEMA)
    engine.cache_store(chash, node.id, output, engine.leaf_cost(sub_id))
    return output


def _quoted(answer: Any) -> str:
    """A rejected answer, quoted and BOUNDED (issue #74).

    ``repr`` because the whitespace and the quoting are the interesting part of
    a human's answer ("' sim '" reads very differently from "sim"), and bounded
    at ``MAX_FAULT_CAUSE_CHARS`` for exactly the reason ``note_checkpoint``
    bounds the question it records: a fault is prose an agent relays, and the
    audit ledger beside it keeps metadata only. The ellipsis is what keeps the
    cut honest — a truncated repr with no marker reads as the whole answer."""
    shown = repr(answer)
    return shown if len(shown) <= MAX_FAULT_CAUSE_CHARS else shown[:MAX_FAULT_CAUSE_CHARS] + "…"


def run_checkpoint(engine: Any, node: Any, context: dict[str, Any]) -> Any:
    """The human gate (WF-10): pause the run and wait for a real answer.

    NEVER spawns a leaf — asking a model to approve on the human's behalf is
    precisely the thing a checkpoint exists to refuse. The answer arrives on the
    resume (``checkpoint_answers``) and is cached as an ordinary completion, so
    a later resume replays it instead of asking again.

    The prompt is RESOLVED before it is asked: the payload has to carry the real
    question, and a resume whose upstream changed is a different question — a
    different cell — which is exactly what the resolved hash expresses.

    An ``accept`` list (issue #74) makes the gate READ the answer instead of
    only recording it: a question a human answered "no" to used to approve the
    run and hand the refusal to the dependent leaf as its prompt. Declaring
    ``accept`` is opt-in — a checkpoint without one keeps taking any answer as
    its output, which is every spec written before this.

    A gate inside a nested template asks and is answered under a NAMESPACED key
    (issue #78): ``sub[<ref>]:<id>``, the spelling ``fold_nested`` already gives
    a nested run's faults and costs. Two levels may name a node ``cp`` without
    knowing about each other, and until this the two shared one answer — with
    ``accept`` in play, a "sim" meant for the parent's "ok to start?" silently
    opened the template's "delete prod?", a question the first-wins pause latch
    never even showed the human. The CELL is namespaced already, by the child's
    own ``spec_identity``; only the key a person types was ambiguous."""
    prompt = strict_prompt(engine, node.id, node.fields.get("prompt", ""), context)
    if prompt is None:
        return None  # an upstream null: fail the node rather than ask about "null"
    # The cell keeps the BARE id: ``cell_hash`` is namespaced by the spec's own
    # (name, version), the preview recomputes it from the same bare id, and the
    # ``node_id`` column is what ``hashes_for_node`` reads. Only the ANSWER and
    # the pause payload take the prefix.
    chash = engine.cell_hash(node.id, "checkpoint", prompt)
    hit, cached = engine.cache_lookup(chash, node.id)
    if hit:
        return cached
    ref = engine.nested_ref
    key = checkpoint_key(ref, node.id)
    payload: dict[str, Any] = {"node_id": key, "prompt": as_text(prompt)}
    if ref:
        # Named outright, like a nested route fault's: the key points at nothing
        # in the spec a resume sends, and a reader told only "node `cp`" would
        # go looking for it there.
        payload["template"] = ref
    accept = node.fields.get("accept")
    # `in`, not .get(): a null default is a default. Never offered on a GUARDED
    # gate: a default answers an unattended resume, so on a gate a person is
    # supposed to open it is a standing YES nobody typed — and the doors back to
    # it (a bare resume rebuilding this payload after a rejection, an
    # explicit-spec resume rebuilding it wholesale) are exactly the ones a "no"
    # travels through. The validator refuses the pair; this is the belt to that
    # brace, for a spec that reached the engine without being validated.
    if "default" in node.fields and not accept:
        payload["default"] = node.fields["default"]
    answers = engine.checkpoint_answers
    if key in answers:
        answer = answers[key]
        if checkpoint_accepts(answer, accept):
            engine.cache_answer(chash, node.id, answer)  # never ask this one again
            return answer
        # A REJECTION. Never cached: caching it would retire the question the
        # human just refused to close, and a `pause` would have nothing left to
        # ask. The node nulls either way; `required` decides what that costs.
        engine.record_fault(f"{node.id}: checkpoint rejected by human: {_quoted(answer)}")
        if checkpoint_on_reject(node.fields) == "pause":
            # Same question, one line of context: a human who is asked twice
            # has to be able to see WHY, or the second pause reads as a lost
            # answer. BOUNDED like the fault, because this payload is persisted
            # and travels through `workflow_status` and `watch`: a paragraph-long
            # "no" must not become a paragraph-long field in all three.
            engine.note_checkpoint(node.id, {**payload, "rejected": _quoted(answer)})
        return None
    engine.note_checkpoint(node.id, payload)
    return None


GATE_STRATEGIES = {
    "gate": run_gate,
    "completeness_check": run_completeness_check,
    "checkpoint": run_checkpoint,
}
