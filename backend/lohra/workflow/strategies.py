"""Per-node-type strategies (spec §4.2). One function per node type.

Deterministic control flow lives here (engine code); intelligence lives only at
the ``agent`` leaves spawned on the OrchestrationCore. A dead/incomplete leaf
resolves to ``None`` (fail-isolation), distinct from an engine fault. The rigor
patterns (verify/judge_panel/loop_until_dry) aggregate leaf verdicts in CODE.

Every node type in ``NODE_SPECS`` has an entry in ``STRATEGIES`` here — the
gating types (gate / completeness_check / checkpoint) live in ``gates.py`` and
are folded in at the bottom, so this file stays the table plus the fan-out and
rigor patterns.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from typing import Any

from lohra.agent.client_pool import ProviderError, configure_for
from lohra.agent.overrides import make_configure
from lohra.workflow import refs
from lohra.workflow.budget import LifetimeExhausted, TokenBudgetExhausted
from lohra.workflow.gates import GATE_STRATEGIES
from lohra.workflow.leaf_retry import EMPTY_OUTPUT_CORRECTION, run_leaf_with_retries
from lohra.workflow.nodes import DEFAULT_LEAF_MAX_ITERATIONS, node_max_iterations, node_retries
from lohra.workflow.prompts import (
    as_text,
    branch_prompt,
    refuse_aggregate_hole,
    refuse_aggregate_hole_deep,
    strict_prompt,
    with_schema_hint,
)
from lohra.workflow.quiescence import QuiescenceReport, await_quiescence
from lohra.workflow.validation import (
    correction_prompt,
    is_empty_output,
    parse_and_validate,
    synthetic_structured_tool,
)

logger = logging.getLogger(__name__)

LEAF_TIMEOUT = 120.0
PIPELINE_TIMEOUT = 1800.0
MAX_PIPELINE_RETRIES = 2  # per (item, stage), via fresh re-spawn (non-blocking)

# Forced verdict for adversarial skeptics (spec §2.5).
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"refuted": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["refuted"],
}
# Forced score for judges.
_SCORE_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "rationale": {"type": "string"}},
    "required": ["score"],
}


def _leaf_prompts(
    engine: Any, node: Any, field: str, context: dict[str, Any]
) -> list[str] | None:
    """A fan-out container (branches/attempts) → one prompt per leaf, fail-closed.

    The container may itself be a whole-value ref (``branches: "${scan.items}"``);
    anything but a list is an authoring/upstream error, recorded as a fault and
    returned as None — never a silent ``[]`` that reads as "fanned out over
    nothing". Entries that CAME from a ref are untrusted leaf output, so they are
    used as inert literals (never re-scanned for ``${...}``, §2.3); authored
    entries get the usual single pass."""
    raw = node.fields.get(field)
    from_ref = isinstance(raw, str)
    container = refs.resolve_value(raw, context) if from_ref else raw
    if not isinstance(container, list):
        if from_ref and container is None:
            inner = refs.find_refs(raw)
            engine.record_fault(f"{node.id}: upstream null: {inner[0] if inner else raw}")
        else:
            engine.record_fault(
                f"{node.id}: {field} resolved to non-list ({type(container).__name__})"
            )
        return None
    # Fanning out OVER an aggregation's own output: a dead branch/item/round is a
    # hole, and as an inert literal it would be stringified to "null" and spawned
    # as a branch of its own (issue #72). Same guard, same fault, other door.
    if from_ref and refuse_aggregate_hole(engine, node.id, raw, context):
        return None
    prompts: list[str] = []
    for entry in container:
        template = branch_prompt(entry)
        if from_ref:  # untrusted: inert literal, never resolved a second time
            prompts.append(as_text(template))
            continue
        prompt = strict_prompt(engine, node.id, template, context)
        if prompt is None:
            return None
        prompts.append(as_text(prompt))
    return prompts


def _text_field(fields: dict[str, Any], name: str) -> str | None:
    """A non-empty string field, else None (a typed-wrong value is not an override)."""
    value = fields.get(name)
    return value if isinstance(value, str) and value else None


def _leaf_config(engine: Any, node: Any) -> tuple[str | None, str | None, str | None, str | None]:
    """(model, effort, provider, warning) for one routable node (WF-5).

    Type-agnostic on purpose — it reads ``node.fields``, never ``node.type`` — so
    the ``agent`` node and every rigor node resolve their routing the same way.

    A ``tier`` names the OPERATOR's map; an explicit model/effort/provider always
    wins over what the tier resolves to (explicit beats resolved, the same
    precedence as everywhere else here). A tier with no mapping is neither silent
    nor fatal: it returns a warning the caller records as a fault and the node
    runs on the run's default model."""
    tier_name = node.fields.get("tier")
    tier = warning = None
    if isinstance(tier_name, str) and tier_name:
        tiers = engine.tiers
        tier = tiers.get(tier_name) if tiers is not None else None
        if tier is None:
            warning = (
                f"{node.id}: tier {tier_name!r} has no mapping in "
                "~/.lohra/workflow_tiers.json (operator config) — this node ran "
                "on the run's default model"
            )
    return (
        _text_field(node.fields, "model") or (tier.model if tier else None),
        _text_field(node.fields, "effort") or (tier.effort if tier else None),
        _text_field(node.fields, "provider") or (tier.provider if tier else None),
        warning,
    )


# The four fields that say WHERE a node's leaves run (nodes.py ``_ROUTING``).
_ROUTING_FIELDS = ("model", "tier", "effort", "provider")


def _resolve_routing(engine: Any, node: Any) -> tuple[str | None, str | None, str | None]:
    """The node's resolved (model, effort, provider), recording an unmapped tier.

    Call it BEFORE the cache lookup, the order ``run_agent`` uses: the warning is
    about the SPEC the operator has to fix, so a resume that replays the cell owes
    the reader the same line. Once per NODE, never once per leaf — a panel of five
    must not shout the same typo five times."""
    model, effort, provider, warning = _leaf_config(engine, node)
    if warning is not None:  # an unmapped tier: say so, then run anyway
        engine.record_fault(warning)
    return model, effort, provider


def _routing_identity(
    node: Any, model: str | None, effort: str | None, provider: str | None
) -> tuple[Any, ...]:
    """The resolved routing AS PART OF a cell identity — only when the node
    declares it.

    A resume must not replay an answer a different model gave, so a routed node
    carries its resolution in the key. But the cache is persisted and run-scoped:
    a cell written before this existed has no routing in its key, and appending a
    trailing ``(None, None, None)`` to every routing-less node would miss every
    one of those rows and silently re-bill work already paid for. Same rule, same
    reason as ``max_iterations`` in ``run_agent``."""
    if not any(field in node.fields for field in _ROUTING_FIELDS):
        return ()
    return (model, effort, provider)


def _rigor_configure(
    engine: Any, node: Any, model: str | None, effort: str | None, provider: str | None
) -> tuple[Any, bool]:
    """``(configure, ok)`` — the ONE hook every leaf of a rigor node shares.

    Uniform by design: a verify's skeptics, a judge_panel's attempts AND their
    judges AND its synthesis, every round of a loop_until_dry, a gate's draft AND
    its reviewer all run where the NODE says. Different models per GROUP (cheap
    judges over an expensive attempt) is a deliberate NON-GOAL — it would need its
    own override inside ``synthesize``/``body``, not a second hook here.

    ``ok`` is False when a provider override cannot be built: the fault is already
    recorded and the caller nulls the node, so nothing is ever spawned onto a
    provider that does not exist (fail-isolation, exactly like ``run_agent``)."""
    try:
        return (
            configure_for(engine.client_pool, provider=provider, model=model, effort=effort),
            True,
        )
    except ProviderError as exc:
        logger.warning("workflow: %s node %r provider unavailable: %s", node.type, node.id, exc)
        engine.record_fault(f"{node.id}: provider unavailable: {exc}")
        return None, False


def run_agent(engine: Any, node: Any, context: dict[str, Any]) -> Any:
    """Spawn one leaf with the resolved prompt; collect + schema-validate (§5).
    Get-or-spawn: a cached cell replays without spawning (resume, §6).

    A node with `tool_less: true` AND a schema forces structured output via a
    synthetic tool (§5.2) — the leaf needs no tools, so this guarantees the JSON
    on supporting providers; others fall back to the validate+steer path.

    The loop is the operator's ROUTE ENVELOPE (#63) and nothing else: it turns
    once more only when ``take_reroute`` hands back a route the operator listed
    in ``~/.lohra/workflow_routes.json`` for the one that just died. Everything
    that decides whether that happens lives in ``engine._offer_reroute``; what
    happens here is the consequence — the node is rebuilt with the new
    provider/model (a NEW object, never a mutation, so the spec's node is
    untouched), which re-resolves the routing, which moves the CELL HASH. That
    last link is the whole reason the loop is safe: the re-routed attempt is a
    new cell, the dead one stays exactly as replayable as it was, and no cached
    answer is ever attributed to a model that did not give it.
    """
    prompt = strict_prompt(engine, node.id, node.fields.get("prompt", ""), context)
    if prompt is None:
        return None  # an upstream null: fail this node instead of prompting "null"
    schema = engine.resolve_schema(node.fields)
    warned = False
    while True:
        model, effort, provider, warning = _leaf_config(engine, node)
        if warning is not None and not warned:
            # An unmapped tier: say so, then run anyway — ONCE per node, even if
            # the envelope re-routes it. The warning is about the SPEC the
            # operator has to fix, and a second copy would read as a second
            # defect (and would count against a run the re-route rescued).
            engine.record_fault(warning)
            warned = True
        # The RESOLVED model/effort/provider + the lifecycle knobs are part of the
        # cell identity: a resume with any of them changed — including a tier that
        # now maps elsewhere — must NOT replay a result from the old config.
        # ``max_iterations`` joins the tuple ONLY when the node declares it: the
        # cache is persisted and run-scoped, so a run cached before this knob
        # existed must still HIT on a resume after the upgrade. A trailing None for
        # every knob-less node would re-key every cell and silently re-bill them.
        # (``timeout``/``retries`` stay unconditional — their Nones are already
        # baked into every persisted row; making them conditional now would cause
        # exactly that mass invalidation.)
        chash = engine.cell_hash(
            node.id, "agent", prompt, schema, model, effort, provider,
            node.fields.get("timeout"), node.fields.get("retries"),
            *((node.fields["max_iterations"],) if "max_iterations" in node.fields else ()),
        )
        hit, cached = engine.cache_lookup(chash, node.id)
        if hit:
            return cached
        try:
            configure = _node_configure(node, schema, engine.client_pool, model, effort, provider)
        except ProviderError as exc:
            logger.warning("workflow: agent node %r provider unavailable: %s", node.id, exc)
            engine.record_fault(f"{node.id}: provider unavailable: {exc}")
            return None  # fail-isolation: this leaf drops to null, the run continues
        output, cost = run_leaf_with_retries(
            engine, node, prompt, schema, configure, cell_id=chash
        )
        # ``schema=`` so a manifest node gets its declared paths measured by the
        # harness before the cell lands (#45 E4).
        engine.cache_store(chash, node.id, output, cost, schema=schema)
        if output is not None:
            # ...and if a re-route is what got us here, the deaths on the route
            # that is now gone stop counting against the verdict (a no-op for
            # every node the envelope never touched).
            engine.mark_reroute_recovered(node.id)
            return output
        reroute = engine.take_reroute(node.id)
        if reroute is None:
            return None
        if engine.stopped:
            # Re-checked HERE and not only at the offer: the offer and the spawn
            # are not the same instant, and a pause or a cancel that landed in
            # between (a sibling node's route dying on a fan-out, an operator's
            # `workflow_cancel`) means nothing will schedule this leaf's
            # successors either. The slot the ledger already spent stays spent —
            # a granted fallback is never refunded (`route_fallback_try`).
            return None
        # ONE extra leaf, bought for a cell the author wrote once — counted for
        # the same reason a same-route re-spawn is (Q2): a template that says
        # "works, cost 0 re-spawns" over a run that paid for two leaves is the
        # honesty this counter exists to keep. The loop restarts the series at
        # attempt 0, so `run_leaf_with_retries` will never count this one.
        engine.count_leaf_respawn()
        node = replace(node, fields={**node.fields, **reroute})


def _node_configure(
    node: Any,
    schema: dict | None,
    pool: Any,
    model: str | None,
    effort: str | None,
    provider: str | None,
):
    """A configure hook for an agent node: per-leaf provider (cross-provider) +
    model + reasoning effort + iteration cap + forced structured output. None if
    nothing overridden.

    The model/effort/provider are already RESOLVED (``_leaf_config`` folded any
    tier in), so this only builds the hook. Raises ProviderError if a provider
    override can't be resolved."""
    forced = synthetic_structured_tool(schema) if (schema and node.fields.get("tool_less")) else None
    # Only pass the leash when the node ASKED for one: an unset field must leave
    # the child factory's own cap alone (no hook at all -> byte-identical).
    iterations = (
        node_max_iterations(node.fields, DEFAULT_LEAF_MAX_ITERATIONS)
        if "max_iterations" in node.fields
        else None
    )
    return configure_for(
        pool,
        provider=provider,
        model=model,
        effort=effort,
        forced_tool=forced,
        max_iterations=iterations,
    )


def run_parallel(engine: Any, node: Any, context: dict[str, Any]) -> list[Any] | None:
    """BARRIER fan-out: spawn every branch, await all, results in input order.
    A container that doesn't resolve to a list fails the node (§7.5).

    ONE cell for the whole node (§6, WF-28): the branches settle together, and a
    HALF fan-out must never be cached — a resume would read the partial list
    back as a finished node and freeze the dead branch's null into every later
    stretch. A branch that answered ``""`` counts as dead here (WF-7): the
    per-list guard in ``cache_store`` only ever sees the aggregate, so the
    completeness of the branches is this node's own to check. The cell's price
    is what EVERY branch cost, not one of them.

    Progress is published as the branches collect (M6, WF-27): a barrier over
    ten branches used to look identical at branch one and branch nine."""
    prompts = _leaf_prompts(engine, node, "branches", context)
    if prompts is None:
        return None
    total = len(prompts)
    chash = engine.cell_hash(node.id, "parallel", prompts)
    hit, cached = engine.cache_lookup(chash, node.id)
    if hit:
        engine.note_node_items(node.id, total, total)  # replayed whole, so: done
        return cached
    engine.gate_fanout(total)
    engine.note_node_items(node.id, 0, total)  # the width is news the moment it starts
    sub_ids = [
        engine.spawn_leaf(
            prompt, causal_context=engine.causal_context(
                cell_id=chash, role="parallel.branch", branch_path=(index,)
            )
        )
        for index, prompt in enumerate(prompts)
    ]
    outputs: list[Any] = []
    for done, sub_id in enumerate(sub_ids, start=1):
        outputs.append(engine.collect_with_schema(sub_id, None))
        engine.note_node_items(node.id, done, total)
    if all(out is not None and not is_empty_output(out) for out in outputs):
        engine.cache_store(
            chash, node.id, outputs, engine.leaves_cost(sub_ids), leaf_count=len(sub_ids)
        )
    return outputs


def run_verify(engine: Any, node: Any, context: dict[str, Any]) -> Any:
    """Adversarial verify (§2.5): N skeptics each tasked to REFUTE the finding;
    majority-refute kills it. Aggregation is deterministic, in code.

    ONE cell for the whole node (WF-28). A REFUTED finding is a completion too —
    the verdict is the answer, and re-running the panel on a resume would ask a
    stochastic jury the same question twice. Only a FULL panel is cached: a
    skeptic that died is an infrastructure failure, and freezing a verdict
    reached with half the jury is the opposite of what verify is for."""
    finding = strict_prompt(engine, node.id, node.fields.get("finding"), context)
    if finding is None:
        return None  # nothing to verify: never claim a null finding survived
    skeptics = max(1, int(node.fields.get("skeptics", 3)))
    lenses = node.fields.get("lenses") or []
    kill = bool(node.fields.get("kill_if_majority_refute", True))
    model, effort, provider = _resolve_routing(engine, node)
    chash = engine.cell_hash(
        node.id, "verify", finding, skeptics, lenses, kill,
        *_routing_identity(node, model, effort, provider),
    )
    hit, cached = engine.cache_lookup(chash, node.id)
    if hit:
        return cached
    # Before the fan-out gate: a panel that cannot be routed spawns nothing, so
    # it must not spend the run's budget on a width it will never use.
    configure, ok = _rigor_configure(engine, node, model, effort, provider)
    if not ok:
        return None
    engine.gate_fanout(skeptics)

    sub_ids = []
    for i in range(skeptics):
        lens = lenses[i % len(lenses)] if lenses else "general correctness"
        sub_ids.append(engine.spawn_leaf(
            _refute_prompt(finding, lens), configure=configure,
            causal_context=engine.causal_context(
                cell_id=chash, role="verify.skeptic", branch_path=(i,)
            ),
        ))
    verdicts = [engine.collect_with_schema(sub_id, _VERDICT_SCHEMA) for sub_id in sub_ids]

    refuted = sum(1 for v in verdicts if isinstance(v, dict) and v.get("refuted") is True)
    counted = sum(1 for v in verdicts if isinstance(v, dict))
    if counted == 0:
        # No verification happened at all. "Nobody refuted it" is not evidence:
        # zero live skeptics must never approve a finding (fail-closed).
        engine.record_fault(f"verify {node.id}: all skeptics dead (fail-closed)")
        survived = False
    else:
        survived = not (kill and refuted * 2 > counted)
    verdict = {
        "finding": finding if survived else None,
        "survived": survived,
        "refuted": refuted,
        "skeptics": counted,
        "verdicts": verdicts,
    }
    if counted == skeptics:  # the whole jury answered: a real completion
        engine.cache_store(
            chash, node.id, verdict, engine.leaves_cost(sub_ids), leaf_count=len(sub_ids)
        )
    return verdict


def _panel_width(attempts: int, judges: int, synthesize: Any) -> int:
    """Every leaf the panel will ask for: the attempts, their judges, and the
    synthesis (only when there is a real one to run — a panel with nothing to
    synthesize must not be refused for a leaf it never spawns)."""
    return attempts + attempts * judges + (1 if isinstance(synthesize, dict) else 0)


def run_judge_panel(engine: Any, node: Any, context: dict[str, Any]) -> Any:
    """N attempts → parallel judges score each → synthesize from the winner.

    ONE cell for the whole panel (WF-28) — the widest node type there is, and
    the one the dogfood caught a resume re-paying for. Only a panel that ran
    WHOLE is cached: a dead attempt, an unscored one or a mid-flight budget stop
    all mean the winner was crowned against less than the spec asked for, and a
    resume deserves the full panel rather than that verdict frozen in."""
    prompts = _leaf_prompts(engine, node, "attempts", context)
    if prompts is None:
        return None
    judges = max(1, int(node.fields.get("judges", 1)))
    synth = node.fields.get("synthesize")
    model, effort, provider = _resolve_routing(engine, node)
    chash = engine.cell_hash(
        node.id, "judge_panel", prompts, judges, synth,
        *_routing_identity(node, model, effort, provider),
    )
    hit, cached = engine.cache_lookup(chash, node.id)
    if hit:
        return cached
    # ONE routing for all three groups (attempts, judges, synthesis) — see
    # ``_rigor_configure``; per-group models are a named non-goal. Resolved before
    # the preflight so an unroutable panel costs no budget at all.
    configure, ok = _rigor_configure(engine, node, model, effort, provider)
    if not ok:
        return None
    # Preflight the WHOLE shape before anything spawns (WF-8). Gating phase by
    # phase let a structurally oversized panel pay for every attempt and only
    # then trip the cap on the judges — work bought and thrown away. This is the
    # STRUCTURAL axis only (max_fanout / lifetime): affordability stays soft and
    # per-phase on purpose, so a panel that runs out of money mid-flight still
    # crowns the attempts it already paid to score.
    engine.budget.check_fanout(_panel_width(len(prompts), judges, synth))
    engine.gate_fanout(len(prompts))

    attempt_ids = [
        engine.spawn_leaf(
            prompt, configure=configure,
            causal_context=engine.causal_context(
                cell_id=chash, role="judge.attempt", branch_path=(index,)
            ),
        )
        for index, prompt in enumerate(prompts)
    ]
    outputs = [engine.collect_with_schema(sub_id, None) for sub_id in attempt_ids]

    leaves = list(attempt_ids)  # every leaf this panel paid for (the cell's price)
    whole = True  # did the panel really run the shape the spec asked for?
    scored: list[tuple[float, Any]] = []
    for attempt_index, output in enumerate(outputs):
        if output is None:
            whole = False
            continue
        try:
            engine.gate_fanout(judges)
            judge_ids = [
                engine.spawn_leaf(
                    _score_prompt(output), configure=configure,
                    causal_context=engine.causal_context(
                        cell_id=chash, role="judge.score",
                        branch_path=(attempt_index, judge_index),
                    ),
                )
                for judge_index in range(judges)
            ]
        except TokenBudgetExhausted:
            whole = False
            # Out of money mid-panel (the engine latched the pause and wrote its
            # one fault). The attempts already scored are real, billed work:
            # crowning the best of THEM beats nulling the node and throwing the
            # whole panel away. Judging what is left is what we cannot afford.
            break
        leaves.extend(judge_ids)
        scores = [engine.collect_with_schema(sub_id, _SCORE_SCHEMA) for sub_id in judge_ids]
        values = [s["score"] for s in scores if isinstance(s, dict) and "score" in s]
        if not values:
            whole = False
            # Unjudged: a 0.0 here would rank as a real score, so an all-dead panel
            # would still crown an arbitrary attempt. Drop it instead (fail-closed).
            engine.record_fault(f"judge_panel {node.id}: attempt unscored (all judges dead)")
            continue
        scored.append((sum(values) / len(values), output))

    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    winner = scored[0][1]
    if not isinstance(synth, dict):
        if whole:
            engine.cache_store(
                chash, node.id, winner, engine.leaves_cost(leaves), leaf_count=len(leaves)
            )
        return winner
    prompt = strict_prompt(engine, node.id, synth.get("prompt", ""), {**context, "winner": winner})
    if prompt is None:
        return None
    try:
        sub_id = engine.spawn_leaf(
            as_text(prompt) + "\n\nWINNER:\n" + as_text(winner), configure=configure,
            causal_context=engine.causal_context(
                cell_id=chash, role="judge.synthesis"
            ),
        )
    except TokenBudgetExhausted:
        return winner  # the panel's verdict, unsynthesised — still better than null
    leaves.append(sub_id)
    output = engine.collect_with_schema(sub_id, synth.get("schema"))
    if whole:
        engine.cache_store(
            chash, node.id, output, engine.leaves_cost(leaves), leaf_count=len(leaves)
        )
    return output


def run_loop_until_dry(engine: Any, node: Any, context: dict[str, Any]) -> list[Any] | None:
    """Re-run the body until K consecutive empty rounds, max_rounds, or the
    node's own ``budget`` (§2.5).

    An unresolvable body prompt fails the WHOLE node (None), never a truncated
    list: a partial harvest reads downstream as "everything until dry".

    ONE cell for the whole harvest (WF-28), identified by the body with its
    UPSTREAM refs resolved — round 0's bindings, which are the neutral ones
    (nothing harvested yet). The LIVE ``round``/``so_far`` are the loop's own
    state, not its inputs: putting them in the identity would make every round a
    different cell and nothing would ever replay. A DEAD round (something
    genuinely failed) leaves the harvest un-cached, the same way an engine
    fault anywhere else does — but a ``budget`` stop is cached like any other
    author-declared cap (``max_rounds`` already was): it is real, billed work
    that ran to the ceiling the author asked for, ``budget`` is already part of
    this cell's identity (a raised ceiling is simply a different cell), and
    NOT caching it would only punish a resume of the SAME run — re-spending
    the node's own budget against the run's token budget on every resume."""
    body = node.fields.get("body") or {}
    stop_after_k = max(1, int(node.fields.get("stop_after_k_empty", 1)))
    max_rounds = max(1, int(node.fields.get("max_rounds", 3)))
    schema = body.get("schema") if isinstance(body, dict) else None
    template = branch_prompt(body)
    first = strict_prompt(engine, node.id, template, {**context, "round": 0, "so_far": []})
    if first is None:
        return None  # an upstream null: fail the node instead of refining "null"
    # This node's OWN token ceiling (issue #73 follow-up), distinct from the
    # run-level Budget: validation rejects anything but a positive int, so
    # this stays lenient the same way node_timeout/node_retries do — a raw
    # fields dict (a pipeline stage, say) that never saw the validator must
    # not crash the loop.
    budget = node.fields.get("budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        budget = None
    model, effort, provider = _resolve_routing(engine, node)
    chash = engine.cell_hash(
        node.id, "loop_until_dry", first, schema, stop_after_k, max_rounds,
        *((budget,) if budget is not None else ()),
        *_routing_identity(node, model, effort, provider),
    )
    hit, cached = engine.cache_lookup(chash, node.id)
    if hit:
        return cached
    # Resolved ONCE, outside the rounds: the routing belongs to the node, not to a
    # round, so every round of the harvest runs on the same model.
    configure, ok = _rigor_configure(engine, node, model, effort, provider)
    if not ok:
        return None
    leaves: list[str] = []
    intact = True  # every round really ran (nothing died, nothing was cut off)
    collected: list[Any] = []
    empty_streak = 0
    for round_index in range(max_rounds):
        prompt = first if round_index == 0 else strict_prompt(
            engine, node.id, template,
            {**context, "round": round_index, "so_far": collected},
        )
        if prompt is None:
            return None  # an upstream null: fail the node instead of refining "null"
        try:
            sub_id = engine.spawn_leaf(
                prompt, configure=configure,
                causal_context=engine.causal_context(
                    cell_id=chash, role="loop.round", branch_path=(round_index,)
                ),
            )
        except TokenBudgetExhausted:
            # Out of money between rounds. The rounds already harvested are real,
            # billed work, and the run is PAUSED — ``stopped`` breaks the node
            # loop, so no downstream node can read this list as "until dry".
            # Nothing harvested yet is None, never []: an empty list would claim
            # the loop looked and found nothing.
            return collected if collected else None
        leaves.append(sub_id)
        output = engine.collect_with_schema(sub_id, schema)
        dry = False
        if output is None:
            intact = False
            # A dead round says nothing about dryness — counting it as empty would
            # end the loop on an infrastructure failure. Record it and keep going
            # (bounded by max_rounds); the streak is neither bumped nor reset.
            engine.record_fault(f"{node.id}: round {round_index} died (not counted as dry)")
        elif output in ("", [], {}):
            empty_streak += 1
            dry = empty_streak >= stop_after_k
        else:
            empty_streak = 0
            collected.append(output)
        if dry:
            # The loop finished on its OWN terms (K empty rounds in a row) —
            # a real, complete harvest, checked BEFORE the budget below: a
            # round that happens to be both the K-th empty AND over budget is
            # still a harvest that ran dry, not a harvest cut short.
            break
        # The node's OWN ceiling, checked BETWEEN rounds like the run-level
        # gate (never mid-round — a leaf already in flight is work already
        # paid for): every round's leaf is charged here, dead or not, so a
        # round that died still counts against the budget it spent tokens on.
        if budget is not None:
            spent_usage = engine.leaves_cost(leaves)
            spent = spent_usage.input_tokens + spent_usage.output_tokens
            if spent >= budget:
                rounds_run = round_index + 1
                unit = "round" if rounds_run == 1 else "rounds"
                engine.record_advisory_fault(
                    f"{node.id}: loop budget reached after {rounds_run} {unit}: "
                    f"{spent} of {budget} tokens"
                )
                # Cached like any other author-declared cap (see the docstring
                # above) — UNLESS every round so far genuinely died: then there
                # is nothing real to cache, and the return below mirrors the
                # run-level TokenBudgetExhausted path just above (nothing
                # harvested is None, never [], so a downstream reader cannot
                # mistake "every round failed" for "looked and found nothing").
                if not collected and not intact:
                    return None
                break
    if intact:  # a real harvest, dry or not: [] is "looked and found nothing"
        engine.cache_store(
            chash, node.id, collected, engine.leaves_cost(leaves), leaf_count=len(leaves)
        )
    return collected


@dataclass(frozen=True)
class StageCell:
    """The IDENTITY of one pipeline ``(item, stage)`` cell: what will be asked,
    under what schema, and the cache key that pins both."""

    node_id: str  # the synthetic ``<node>#<item>#<stage>``
    prompt: Any
    schema: dict | None
    chash: str


def stage_cell(
    engine: Any,
    owner_node_id: str,
    stage: Any,
    stage_idx: int,
    index: int,
    item: Any,
    prev: Any,
    context: dict[str, Any],
) -> StageCell | None:
    """The key the engine WILL look up for one ``(item, stage)`` cell.

    ONE definition, deliberately: the scheduler below WRITES the cell through it
    and ``cache_preview`` RECOMPUTES it through the same function against a
    stand-in engine. A pipeline's key composition is the only one in the harness
    that no strategy can be replayed to obtain — ``run_pipeline`` spawns before
    it returns — so a second copy of this arithmetic is exactly how a preview
    starts announcing invalidations that are not happening (D6).

    Cell identity = (stage, THIS item, resolved prompt, schema). Without the
    item, a stage whose prompt doesn't interpolate ``${item}`` collapses every
    item onto one cell and a resume replays one answer for all of them. The
    stage's own iteration leash is part of its identity too: a resume after
    raising it must re-run the cell, not replay the answer the short leash
    produced. Only when DECLARED, though (same predicate as ``_stage_configure``):
    a stage that never asked keeps its pre-knob hash, so a run cached before this
    knob existed still resumes.

    None when the prompt resolves to an upstream null: the engine drops that item
    without ever asking the cache, so there is no cell to replay or re-pay."""
    stage_ctx = {**context, "item": item, "stage": {"result": prev}}
    node_id = f"{owner_node_id}#{index}#{stage_idx}"
    prompt = strict_prompt(engine, node_id, branch_prompt(stage), stage_ctx)
    if prompt is None:
        return None
    schema = engine.resolve_schema(stage) if isinstance(stage, dict) else None
    chash = engine.cell_hash(
        owner_node_id, stage_idx, item, prompt, schema,
        *(
            (stage["max_iterations"],)
            if isinstance(stage, dict) and "max_iterations" in stage
            else ()
        ),
    )
    return StageCell(node_id=node_id, prompt=prompt, schema=schema, chash=chash)


@dataclass(frozen=True)
class _Cell:
    """One (item, stage) attempt — everything the done-path needs to settle it."""

    index: int
    stage: int
    schema: dict | None
    chash: str
    node_id: str
    prev_output: Any
    # The node as the SPEC writes it. ``node_id`` is the synthetic
    # ``pl#<item>#<stage>`` a fault needs to say which cell died; this is the one
    # an author could edit, and the only honest identity for a pause payload
    # (#43). Kept as its own field rather than parsed back out of ``node_id``:
    # ``#`` is a legal character in an authored id, so splitting on it would
    # mangle exactly the ids nobody expects to be mangled.
    owner_node_id: str


class _PipelineRun:
    """The NO-barrier scheduler for one ``pipeline`` node (spec §4.3).

    Each item walks the stages independently, chained off the core's non-blocking
    on_done, so a fast item's whole chain can finish before a slow item's first
    stage returns. Every done-path runs on an orch worker: it must never block on
    an orch task (it only reads with wait=False, validates in-process, re-spawns)
    and it must never raise out — the core merely LOGS a raise from on_done, so an
    unguarded crash would strand that item until the barrier expires.

    All shared state is mutated under ``_lock`` (concurrent workers settle items)
    and each item is settled exactly once."""

    def __init__(self, engine: Any, node: Any, items: list, stages: list, context: dict) -> None:
        self._engine = engine
        self._node = node
        self._items = items
        self._stages = stages
        self._context = context
        self._results: list[Any] = [None] * len(items)
        self._retries: dict[tuple[int, int], int] = {}
        self._settled: set[int] = set()
        # The items that really DIED, as opposed to the ones that settled None on
        # a nullable-root schema the author declared (#72, M1). Only this set is
        # published to the engine's hole ledger.
        self._holes: set[int] = set()
        self._remaining = len(items)
        self._expired = False
        self._spawned: list[str] = []  # every leaf this node started (for expiry cancel)
        self._lock = threading.Lock()
        self._done = threading.Event()

    def run(self) -> list[Any]:
        """Dispatch every item, wait on the barrier, return a COPY of the results
        (a straggler must never mutate the list we already reported)."""
        # The WIDTH is news the moment the node starts — the same reason
        # ``parallel`` reports 0/total before it spawns. A fan-out that only
        # speaks up when its first item settles reads as wedged until then.
        self._engine.note_node_items(self._node.id, 0, len(self._items))
        for index in range(len(self._items)):
            try:
                self._advance(index, 0, self._items[index])  # stage 0's "prev" is the item
            except Exception as exc:  # an unexpected raise here would strand the node
                self._engine.record_fault(
                    f"{self._node.id}#{index}: dispatch failed: {type(exc).__name__}: {exc}"
                )
                self._drop(index)
        if not self._done.wait(PIPELINE_TIMEOUT):
            self._expire()
        with self._lock:
            holes = frozenset(self._holes)
            results = list(self._results)
        # Published OUTSIDE the lock (``_finish`` avoids the same nested-lock
        # order), and always — an empty set is the assertion "nothing died here",
        # which is exactly what a downstream nullable item needs said about it.
        self._engine.note_aggregate_holes(self._node.id, holes)
        return results

    def _expire(self) -> None:
        """Close the barrier. The timeout is a FAULT, not just a log line — a
        half-finished pipeline would otherwise report as a clean run."""
        with self._lock:
            self._expired = True
            pending = self._remaining
            spawned = list(self._spawned)
            # An item the barrier stranded never answered: that is a hole too.
            self._holes |= set(range(len(self._items))) - self._settled
        # On the NODE thread, so it may wait: nobody is chained off this.
        zombies, report = self._cancel_running(spawned, quiesce=True)
        # The cancel is cooperative: say whether the leaves we stopped really
        # went quiet, because the next node reads the same working_root (#42-B).
        quiet = f" ({report.clause()})" if report.clause() else ""
        self._engine.record_fault(
            f"{self._node.id}: pipeline timed out after {PIPELINE_TIMEOUT:.0f}s, "
            f"{pending} item(s) unfinished, {zombies} leaf(s) cancelled{quiet}"
        )

    def _cancel_running(
        self, sub_ids: list[str], *, quiesce: bool
    ) -> tuple[int, QuiescenceReport]:
        """Cancel the leaves still in flight once nobody will read them again.

        Leaving them running is the WF-2 zombie at pipeline scale: N leaves each
        holding an orch worker for a node that already reported its results —
        and, since issue #42-B, each of them still holding a write capability on
        the run's shared working root.

        ``quiesce`` is keyword-only and has NO default, because the two callers
        sit on opposite sides of the blocking contract and a third one must not
        inherit a policy it never thought about:

        - the barrier's ``_expire`` runs on the NODE thread and passes True: it
          waits one shared cap (never cap x N) for the leaves to go quiet, and
          what it observed goes into the barrier's own fault;
        - the stranded TOCTOU path in ``_advance`` passes False. That one runs on
          the pool's ``on_done`` workers (a stage chaining into the next), where
          a wait would park a worker the pipeline still needs — exactly what
          ``quiescence.await_quiescence`` says it must never be used for. The
          cancel still lands; only the waiting is skipped, and the empty report
          claims nothing about a leaf nobody looked at.
        """
        cancelled = 0
        interrupted: list[str] = []
        for sub_id in sub_ids:
            try:
                if self._engine.core.collect(sub_id, wait=False).get("status") != "running":
                    continue
                out = self._engine.core.cancel(sub_id)
                cancelled += 1
                if out.get("cancelled") == "running":
                    # Only the cooperative ones are worth waiting for: a queued
                    # leaf never reached a provider and never touched the disk.
                    interrupted.append(sub_id)
                if out.get("cancelled") == "queued":
                    # Its own on_done cannot settle it: we set ``_expired``
                    # before cancelling, and the hook's straggler guard returns
                    # before it ever accounts. Settle it here — ONLY the queued
                    # ones: a leaf still inside a provider call is real cost that
                    # its own done-path must charge.
                    # Whether the slot is refunded is NOT decided here: the core
                    # calls a dropped turn ``cancelled`` only for a sub-session
                    # that never reached a provider AT ALL, and a steered one
                    # that already billed lands as ``interrupted`` instead (#60,
                    # F1). True by construction for this pipeline, which
                    # re-spawns a fresh leaf per attempt and never steers one —
                    # a precondition, not a law of the barrier.
                    self._engine.account_leaf(sub_id)
            except Exception:  # never let cleanup mask the timeout fault
                logger.exception("workflow: failed to cancel stranded leaf %s", sub_id)
        if not quiesce:
            return cancelled, QuiescenceReport()
        return cancelled, await_quiescence(self._engine.core, interrupted)

    @property
    def _is_expired(self) -> bool:
        with self._lock:
            return self._expired

    def _finish(self, index: int, output: Any) -> None:
        """Settle one item exactly once; a straggler that lands after the barrier
        expired is discarded (its slot was already reported)."""
        with self._lock:
            if self._expired or index in self._settled:
                return
            self._settled.add(index)
            self._results[index] = output
            self._remaining -= 1
            settled = len(self._settled)
            if self._remaining == 0:
                self._done.set()
        # Publish intra-node progress (M6) OUTSIDE this lock: the engine takes a
        # lock of its own, and holding two in one order here is how a deadlock
        # gets introduced later. The count was read above, so it is still exact.
        self._engine.note_node_items(self._node.id, settled, len(self._items))

    def _drop(self, index: int) -> None:
        """Settle one item as DEAD. Distinct from ``_finish(index, None)``, which
        also covers an item that legitimately RESOLVED to null (#72, M1): only
        what passes through here is published as a hole."""
        with self._lock:
            self._holes.add(index)
        self._finish(index, None)

    def _advance(self, index: int, stage_idx: int, prev: Any, correction: str | None = None) -> None:
        if self._is_expired:
            return  # barrier closed: no new leaves, no late settlement
        if self._engine.stopped:
            # Cancelled or paused mid-flight: stop chaining stages, but SETTLE the
            # item so the barrier releases now instead of waiting out
            # PIPELINE_TIMEOUT (a quota pause must reach the caller promptly).
            self._drop(index)
            return
        if stage_idx >= len(self._stages):
            self._finish(index, prev)
            return
        engine = self._engine
        stage = self._stages[stage_idx]
        # The cell's identity, computed by the ONE function ``cache_preview``
        # also calls — see ``stage_cell``.
        identity = stage_cell(
            engine, self._node.id, stage, stage_idx, index,
            self._items[index], prev, self._context,
        )
        if identity is None:
            self._drop(index)  # upstream null: drop THIS item (per-item isolation)
            return
        node_id, prompt, schema, chash = (
            identity.node_id, identity.prompt, identity.schema, identity.chash
        )
        # Get-or-spawn per (item, stage) cell — only on the first attempt; a
        # cached cell replays without a spawn (resume, §6.4). Synchronous on hit.
        if correction is None:
            # Shared node id: every (item, stage) of this pipeline stores its
            # cell under the RAW node id, so a miss here cannot claim the
            # identity changed on the strength of a sibling's row (D6).
            hit, cached = engine.cache_lookup(chash, self._node.id, shared_node_id=True)
            if hit:
                if cached is None:
                    self._finish(index, None)
                else:
                    self._advance(index, stage_idx + 1, cached)
                return
        spawn_prompt = prompt if correction is None else f"{prompt}\n\n{correction}"
        cell = _Cell(index, stage_idx, schema, chash, node_id, prev, self._node.id)
        try:
            attempt = self._retries.get((index, stage_idx), 0)
            sub_id = engine.spawn_leaf_with_done(
                with_schema_hint(spawn_prompt, schema),
                self._hook(cell),
                configure=_stage_configure(stage),
                causal_context=engine.causal_context(
                    cell_id=chash, role="pipeline.stage", item_index=index,
                    stage_index=stage_idx, attempt=attempt,
                ),
            )
        except LifetimeExhausted as exc:
            # The run's declared lifetime is gone (N items x M stages can exceed
            # it). The CLAIM is atomic now, made inside the engine's spawn funnel:
            # checking ``lifetime_remaining`` HERE and letting the engine charge
            # after ``core.spawn`` left a window — a DB write and a pool submit
            # wide — that every concurrent on_done worker read the same stale
            # ledger through (#14).
            #
            # Fail CLOSED, exactly like the ``FanoutRejected`` the engine's node
            # handler records for every other node type: a fault naming the cell
            # and a cap trip. A log line plus a null item sealed the run
            # ``complete`` with nothing refused on its record — and a truncated
            # pipeline that reads clean is one the library will certify as a
            # reusable template (M1/M2, §12). We catch it HERE because this raise
            # happens on an on_done worker: it never reaches the node thread.
            engine.record_fault(f"{node_id}: {exc}")
            engine.count_cap_trip()
            self._drop(index)
            return
        except TokenBudgetExhausted:
            # The run is out of tokens (the engine latched the pause). Settle
            # this item HERE: letting the raise escape would reach it either as a
            # bogus "dispatch failed" or "done-path crashed" fault, depending on
            # which thread we are on. Every other item settles on its own via the
            # ``engine.stopped`` check at the top of _advance.
            self._drop(index)
            return
        with self._lock:
            self._spawned.append(sub_id)  # so the barrier can cancel what it strands
            # The barrier can close in the window between the spawn above and this
            # append — its snapshot would then miss this leaf and nobody would
            # ever cancel or read it (the WF-2 zombie again). Append and re-read
            # the flag in the SAME critical section: whichever side wins, the
            # leaf is cancelled exactly once.
            stranded = self._expired
        if stranded:
            # On an ``on_done`` worker (a stage chaining into the next): cancel,
            # never wait — see ``_cancel_running``.
            self._cancel_running([sub_id], quiesce=False)

    def _hook(self, cell: _Cell) -> Any:
        """The leaf's completion callback: guarded, so a crash in the done-path
        settles the item instead of leaving it pending until the barrier."""

        def on_done(sub_id: str) -> None:
            if self._is_expired:
                return  # straggler: never account, cache or settle it
            try:
                self._stage_done(sub_id, cell)
            except Exception as exc:
                self._engine.record_fault(
                    f"{cell.node_id}: done-path crashed: {type(exc).__name__}: {exc}"
                )
                self._drop(cell.index)

        return on_done

    def _stage_done(self, sub_id: str, cell: _Cell) -> None:
        engine = self._engine
        res = engine.core.collect(sub_id, wait=False)  # already terminal; non-blocking
        engine.account_leaf(sub_id)  # fold this cell's cost into the rollup
        if res.get("status") != "complete":
            # The cell id carries the cause; the NODE id is what a pause reports.
            engine.note_leaf_failure(
                cell.node_id, res, owner_node_id=cell.owner_node_id
            )
            self._drop(cell.index)  # dead leaf: no cache row -> a resume re-spawns it
            return
        output = res.get("output")
        if output is None:
            # Nothing came back at all — never the same fact as a stage whose
            # SCHEMA resolved to null further down, which settles via _finish.
            self._drop(cell.index)
            return
        if is_empty_output(output):
            # A stage that answered nothing (WF-7): recoverable, same bounded
            # re-spawn budget as a schema mismatch — never a "" flowing onward.
            self._retry_or_drop(cell, EMPTY_OUTPUT_CORRECTION, empty=True)
            return
        if cell.schema is not None:
            ok, parsed, err = parse_and_validate(output, cell.schema)
            if not ok:
                self._retry_or_drop(cell, correction_prompt(cell.schema, err))
                return
            output = parsed
        engine.cache_store(
            cell.chash, cell.node_id, output, engine.leaf_cost(sub_id), schema=cell.schema
        )
        self._advance(cell.index, cell.stage + 1, output)  # next stage (non-blocking)

    def _stage_retries(self, stage_idx: int) -> int:
        """This stage's re-spawn budget: its own ``retries``, else the default."""
        stage = self._stages[stage_idx]
        return node_retries(stage, MAX_PIPELINE_RETRIES) if isinstance(stage, dict) else MAX_PIPELINE_RETRIES

    def _retry_or_drop(self, cell: _Cell, correction: str, *, empty: bool = False) -> None:
        """Bounded retry via a FRESH leaf carrying a correction (re-spawn, never
        steer — the done-path must not block an orch worker). Schema mismatches
        and empty answers share one budget per (item, stage)."""
        key = (cell.index, cell.stage)
        with self._lock:
            self._retries[key] = self._retries.get(key, 0) + 1
            attempt = self._retries[key]
        if attempt > self._stage_retries(cell.stage):
            if empty:  # a silent drop would hide WHY this item produced nothing
                self._engine.record_fault(f"{cell.node_id}: empty output after retry")
            self._drop(cell.index)  # exhausted -> drop (no cache row; resume retries)
            return
        if not empty:
            self._engine.count_validation_retry()
        # Either way this is a WHOLE new leaf, not a steer inside a living one
        # (see the docstring): count it where every other extra leaf is counted,
        # so ``leaf_respawns`` means the same thing in a pipeline as it does in
        # an ``agent`` node. ``validation_retries`` is untouched — the two
        # counters answer different questions about the same event.
        self._engine.count_leaf_respawn()
        self._advance(cell.index, cell.stage, cell.prev_output, correction=correction)


def _stage_configure(stage: Any) -> Any:
    """A configure hook for one pipeline STAGE. Only ``max_iterations`` for now:
    a stage that needs more tool rounds than the default could not say so, and a
    dropped item is the most expensive kind of silence in a pipeline.

    None when the stage did not ask (byte-identical: no hook, factory cap kept).
    A stage dict is NOT validated at author time — the getter is the only guard,
    so it stays lenient and capped."""
    if not isinstance(stage, dict) or "max_iterations" not in stage:
        return None
    return make_configure(
        max_iterations=node_max_iterations(stage, DEFAULT_LEAF_MAX_ITERATIONS)
    )


def run_pipeline(engine: Any, node: Any, context: dict[str, Any]) -> list[Any] | None:
    """NO-barrier multi-stage (spec §4.3): each item advances through the stages
    independently. A dead stage drops that item to None; a schema-invalid stage
    retries via a FRESH re-spawn (bounded, non-blocking) before dropping.

    The barrier (Event.wait) runs on the SEPARATE WorkflowService pool, so waiting
    here doesn't starve the orch pool. Results gathered in input order."""
    items = refs.resolve_value(node.fields.get("items"), context)
    stages = node.fields.get("stages")
    if not isinstance(stages, list) or not stages:
        engine.record_fault(f"{node.id}: stages is not a non-empty list")
        return None
    if not isinstance(items, list):
        # A ref that didn't resolve to a list is an error, not "nothing to do".
        engine.record_fault(f"{node.id}: items resolved to non-list ({type(items).__name__})")
        return None
    if not items:
        return []
    engine.budget.check_fanout(len(items))
    return _PipelineRun(engine, node, items, stages, context).run()


def run_workflow(engine: Any, node: Any, context: dict[str, Any]) -> Any:
    """Inline-run another named workflow, ONE nesting level (spec §4.4).

    The engine recurses (deterministic — not an agent): load the ref'd template,
    validate, run it on a nested engine that SHARES this run's core/budget/cache
    (so the leaf sandbox and budget can't be escaped), and return its outputs.
    Depth is hard-capped; the nested leaves stay sandboxed by construction."""
    from lohra.workflow.engine import MAX_WORKFLOW_DEPTH
    from lohra.workflow.schema import ValidationError, validate_spec

    if engine.depth >= MAX_WORKFLOW_DEPTH:
        raise RuntimeError(f"workflow nesting exceeds depth {MAX_WORKFLOW_DEPTH}")  # engine fault
    ref = node.fields.get("ref")
    if not isinstance(ref, str):
        return None
    spec_dict = engine.load_workflow(ref)
    if spec_dict is None:
        logger.warning("workflow: nested ref %r not found", ref)
        return None
    parsed = validate_spec(spec_dict, supported_types=frozenset(STRATEGIES))
    if isinstance(parsed, ValidationError):
        logger.warning("workflow: nested ref %r failed validation: %s", ref, parsed.message)
        first_issue = parsed.issues[0]
        # Metadata-safe (#79, H9): the issue's RULE CODE and node/field path —
        # never .message/.example, which quote the author's spec prose back
        # into the fault trail.
        where = ".".join(p for p in (first_issue.node_id, first_issue.field) if p)
        locator = f" ({where})" if where else ""
        engine.record_fault(
            f"{node.id}: nested template '{ref}' rejected: {first_issue.rule}{locator}"
        )
        return None
    authored_args = node.fields.get("args") or {}
    # A hole must not cross into a nested run: there it becomes the child's own
    # ``${args.parts}``, indistinguishable from data the author passed on purpose
    # (#72, M2). Checked BEFORE the child engine is built, so nothing is spawned.
    if refuse_aggregate_hole_deep(engine, node.id, authored_args, context):
        return None
    sub_args = refs.resolve_value(authored_args, context)
    if not isinstance(sub_args, dict):
        sub_args = {}
    nested = engine.nested_engine(node.id).run(parsed, sub_args)
    engine.fold_nested(nested, ref)  # keep nested failures visible in the rollup
    return nested.outputs


def _refute_prompt(finding: Any, lens: str) -> str:
    return (
        f"You are a skeptic reviewing through the lens of: {lens}. Try hard to REFUTE "
        f"the following finding. Default to refuted=true if you find any real problem.\n\n"
        f"FINDING:\n{as_text(finding)}\n\n"
        'Respond with ONLY JSON: {"refuted": <true|false>, "reason": "<why>"}.'
    )


def _score_prompt(attempt: Any) -> str:
    return (
        "Score this attempt from 0 to 10 on quality and correctness.\n\n"
        f"ATTEMPT:\n{as_text(attempt)}\n\n"
        'Respond with ONLY JSON: {"score": <0-10>, "rationale": "<why>"}.'
    )


STRATEGIES = {
    **GATE_STRATEGIES,  # gate / completeness_check / checkpoint (M7)
    "agent": run_agent,
    "parallel": run_parallel,
    "pipeline": run_pipeline,
    "verify": run_verify,
    "judge_panel": run_judge_panel,
    "loop_until_dry": run_loop_until_dry,
    "workflow": run_workflow,
}
