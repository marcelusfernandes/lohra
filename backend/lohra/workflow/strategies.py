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
from dataclasses import dataclass
from typing import Any

from lohra.agent.client_pool import ProviderError, configure_for
from lohra.agent.overrides import make_configure
from lohra.agent.types import Usage
from lohra.workflow import refs
from lohra.workflow.budget import LifetimeExhausted, TokenBudgetExhausted
from lohra.workflow.gates import GATE_STRATEGIES
from lohra.workflow.nodes import DEFAULT_LEAF_MAX_ITERATIONS, node_max_iterations, node_retries
from lohra.workflow.prompts import as_text, branch_prompt, strict_prompt, with_schema_hint
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

# What a leaf that answered nothing is told on its re-spawn (WF-7).
EMPTY_OUTPUT_CORRECTION = (
    "Your previous answer was empty. Produce the actual answer as text — "
    "if you truly cannot, say explicitly what blocked you."
)

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
    on supporting providers; others fall back to the validate+steer path."""
    prompt = strict_prompt(engine, node.id, node.fields.get("prompt", ""), context)
    if prompt is None:
        return None  # an upstream null: fail this node instead of prompting "null"
    schema = engine.resolve_schema(node.fields)
    model, effort, provider, warning = _leaf_config(engine, node)
    if warning is not None:  # an unmapped tier: say so, then run anyway
        engine.record_fault(warning)
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
    output, cost = _run_leaf_with_retries(
        engine, node, prompt, schema, configure, cell_id=chash
    )
    engine.cache_store(chash, node.id, output, cost)  # only a real completion lands a row
    return output


def _run_leaf_with_retries(
    engine: Any, node: Any, prompt: Any, schema: dict | None, configure: Any,
    *, cell_id: str,
):
    """Spawn the leaf; an EMPTY answer buys a bounded FRESH re-spawn (WF-7).

    A "complete" leaf that said nothing is a recoverable failure, not an answer:
    it is invisible downstream (it passes every schema-less path and counts as no
    null at all). Retry from scratch — never a steer, so the retry can't inherit
    whatever wedged the first attempt — then null it with an explicit fault. A
    dead leaf (None) is NOT retried here: it already carries its own cause.

    Returns (output, cost) — the cost of the leaf that actually answered, for the
    cache row. Every attempt is charged to the budget by ``account_leaf``; only
    the winner's price is what this cell replays as."""
    attempts = node_retries(node.fields) + 1
    for attempt in range(attempts):
        # The retry says WHY it is being asked again; the cache cell identity stays
        # the authored prompt, so a resume still recognises this same cell.
        text = prompt if attempt == 0 else f"{prompt}\n\n{EMPTY_OUTPUT_CORRECTION}"
        sub_id = engine.spawn_leaf(
            with_schema_hint(text, schema), configure=configure,
            causal_context=engine.causal_context(
                cell_id=cell_id, role="agent", attempt=attempt
            ),
        )
        output = engine.collect_validated(node, sub_id)
        if not is_empty_output(output):
            return output, engine.leaf_cost(sub_id)
    engine.record_fault(f"{node.id}: empty output after retry ({attempts} attempt(s))")
    return None, Usage()  # nothing to cache, and no price to carry


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
        engine.cache_store(chash, node.id, outputs, engine.leaves_cost(sub_ids))
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
        engine.cache_store(chash, node.id, verdict, engine.leaves_cost(sub_ids))
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
            engine.cache_store(chash, node.id, winner, engine.leaves_cost(leaves))
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
        engine.cache_store(chash, node.id, output, engine.leaves_cost(leaves))
    return output


def run_loop_until_dry(engine: Any, node: Any, context: dict[str, Any]) -> list[Any] | None:
    """Re-run the body until K consecutive empty rounds or max_rounds (§2.5).

    An unresolvable body prompt fails the WHOLE node (None), never a truncated
    list: a partial harvest reads downstream as "everything until dry".

    ONE cell for the whole harvest (WF-28), identified by the body with its
    UPSTREAM refs resolved — round 0's bindings, which are the neutral ones
    (nothing harvested yet). The LIVE ``round``/``so_far`` are the loop's own
    state, not its inputs: putting them in the identity would make every round a
    different cell and nothing would ever replay. Only a harvest that ran to a
    natural end is cached — a dead round or a budget stop leaves the list short,
    and short is exactly what must not read back as "until dry"."""
    body = node.fields.get("body") or {}
    stop_after_k = max(1, int(node.fields.get("stop_after_k_empty", 1)))
    max_rounds = max(1, int(node.fields.get("max_rounds", 3)))
    schema = body.get("schema") if isinstance(body, dict) else None
    template = branch_prompt(body)
    first = strict_prompt(engine, node.id, template, {**context, "round": 0, "so_far": []})
    if first is None:
        return None  # an upstream null: fail the node instead of refining "null"
    model, effort, provider = _resolve_routing(engine, node)
    chash = engine.cell_hash(
        node.id, "loop_until_dry", first, schema, stop_after_k, max_rounds,
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
        if output is None:
            intact = False
            # A dead round says nothing about dryness — counting it as empty would
            # end the loop on an infrastructure failure. Record it and keep going
            # (bounded by max_rounds); the streak is neither bumped nor reset.
            engine.record_fault(f"{node.id}: round {round_index} died (not counted as dry)")
            continue
        if output in ("", [], {}):
            empty_streak += 1
            if empty_streak >= stop_after_k:
                break
        else:
            empty_streak = 0
            collected.append(output)
    if intact:  # a real harvest, dry or not: [] is "looked and found nothing"
        engine.cache_store(chash, node.id, collected, engine.leaves_cost(leaves))
    return collected


@dataclass(frozen=True)
class _Cell:
    """One (item, stage) attempt — everything the done-path needs to settle it."""

    index: int
    stage: int
    schema: dict | None
    chash: str
    node_id: str
    prev_output: Any


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
                self._finish(index, None)
        if not self._done.wait(PIPELINE_TIMEOUT):
            self._expire()
        with self._lock:
            return list(self._results)

    def _expire(self) -> None:
        """Close the barrier. The timeout is a FAULT, not just a log line — a
        half-finished pipeline would otherwise report as a clean run."""
        with self._lock:
            self._expired = True
            pending = self._remaining
            spawned = list(self._spawned)
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
                    # It never reached a provider, so its lifetime slot bought
                    # nothing — and its own on_done cannot give it back: we set
                    # ``_expired`` before cancelling, and the hook's straggler
                    # guard returns before it ever accounts. Settle it here.
                    # ONLY the queued ones: a leaf still inside a provider call
                    # is real cost that its own done-path must charge.
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

    def _advance(self, index: int, stage_idx: int, prev: Any, correction: str | None = None) -> None:
        if self._is_expired:
            return  # barrier closed: no new leaves, no late settlement
        if self._engine.stopped:
            # Cancelled or paused mid-flight: stop chaining stages, but SETTLE the
            # item so the barrier releases now instead of waiting out
            # PIPELINE_TIMEOUT (a quota pause must reach the caller promptly).
            self._finish(index, None)
            return
        if stage_idx >= len(self._stages):
            self._finish(index, prev)
            return
        engine = self._engine
        stage = self._stages[stage_idx]
        stage_ctx = {**self._context, "item": self._items[index], "stage": {"result": prev}}
        node_id = f"{self._node.id}#{index}#{stage_idx}"
        prompt = strict_prompt(engine, node_id, branch_prompt(stage), stage_ctx)
        if prompt is None:
            self._finish(index, None)  # upstream null: drop THIS item (per-item isolation)
            return
        schema = engine.resolve_schema(stage) if isinstance(stage, dict) else None
        # Cell identity = (stage, THIS item, resolved prompt, schema). Without the
        # item, a stage whose prompt doesn't interpolate ${item} collapses every
        # item onto one cell and a resume replays one answer for all of them.
        # The stage's own iteration leash is part of its identity too: a resume
        # after raising it must re-run the cell, not replay the answer the short
        # leash produced. Only when DECLARED, though (same predicate as
        # ``_stage_configure``): a stage that never asked keeps its pre-knob
        # hash, so a run cached before this knob existed still resumes.
        chash = engine.cell_hash(
            self._node.id, stage_idx, self._items[index], prompt, schema,
            *(
                (stage["max_iterations"],)
                if isinstance(stage, dict) and "max_iterations" in stage
                else ()
            ),
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
        cell = _Cell(index, stage_idx, schema, chash, node_id, prev)
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
            self._finish(index, None)
            return
        except TokenBudgetExhausted:
            # The run is out of tokens (the engine latched the pause). Settle
            # this item HERE: letting the raise escape would reach it either as a
            # bogus "dispatch failed" or "done-path crashed" fault, depending on
            # which thread we are on. Every other item settles on its own via the
            # ``engine.stopped`` check at the top of _advance.
            self._finish(index, None)
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
                self._finish(cell.index, None)

        return on_done

    def _stage_done(self, sub_id: str, cell: _Cell) -> None:
        engine = self._engine
        res = engine.core.collect(sub_id, wait=False)  # already terminal; non-blocking
        engine.account_leaf(sub_id)  # fold this cell's cost into the rollup
        if res.get("status") != "complete":
            engine.note_leaf_failure(cell.node_id, res)  # carry the cause, not a bare null
            self._finish(cell.index, None)  # dead leaf: no cache row -> a resume re-spawns it
            return
        output = res.get("output")
        if output is None:
            self._finish(cell.index, None)
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
        engine.cache_store(cell.chash, cell.node_id, output, engine.leaf_cost(sub_id))
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
            self._finish(cell.index, None)  # exhausted -> drop (no cache row; resume retries)
            return
        if not empty:
            self._engine.count_validation_retry()
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
        return None
    sub_args = refs.resolve_value(node.fields.get("args") or {}, context)
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
