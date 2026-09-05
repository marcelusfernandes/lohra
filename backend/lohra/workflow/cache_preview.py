"""What a resume will REPLAY and what it will RE-PAY, before anything spawns.

#44 épico 2. The investigation of the real ``lohra-notion-v4`` run found the
node cache CORRECT (zero incorrect re-executions in six stretches) and one
legitimate invalidation already queued and invisible: a pivot had swapped
``final_certification`` from deepseek to glm, that node HAD a cell, and the next
resume was going to re-pay ~2.13M tokens as a silent ``cache.missed``. Nothing
announced it. This module is the announcement.

Read-only by construction: it opens no leaf, spawns nothing, calls no provider
and writes not one row. It recomputes the cell keys of the spec that is about to
run and crosses them with ``workflow_node_cache`` / ``workflow_node_cost``.

HOW the keys are recomputed — the only method that cannot drift. Rather than
re-deriving each node type's key composition (nine different tuples, several of
them conditional), it runs the REAL strategy from ``STRATEGIES`` against a
stand-in engine that implements exactly what a strategy touches before its cache
lookup — and raises at the lookup itself, with the hash the strategy just
computed. The prompt resolution, the schema coercion, the tier/routing
resolution and every default are therefore the production ones, byte for byte.
If a strategy ever grows a step before its lookup that the stand-in cannot
answer, that node degrades to ``unknown`` (never to a wrong hash), and the
round-trip test that runs a real engine over every covered node type and demands
a full replay fails loudly.

The context a downstream key needs is rebuilt from the cache itself: a cell that
hits IS the upstream output, so a node whose ancestors all replay resolves its
own prompt exactly as the engine will. The moment an ancestor's output is NOT
knowable (a miss, or a node this module cannot recompute) every node downstream
of it becomes ``unknown`` — an honest refusal beats a confident wrong hash.

The two FAN-OUT shapes (#61, v2). Both own more than one cell, and both used to
be reported whole as ``unknown`` — which is where the cost of a real DAG lives:

- ``pipeline`` — one cell per ``(item, stage)``, and stage N's prompt
  interpolates stage N-1's OUTPUT. Its key cannot be obtained by replaying the
  strategy (``run_pipeline`` spawns before it returns), so the identity
  arithmetic lives in ONE shared function, ``strategies.stage_cell``, which the
  scheduler that WRITES the cell calls too. Each item is walked independently:
  a cell that hits IS the ``${stage.result}`` the next stage interpolates, and
  the first cell of an item that does NOT hit makes that item's remaining stages
  ``unknown`` — never a hash guessed off an output nobody has produced (D6).
- ``workflow`` — a nested node owns no cell of its own; its children do, and
  they are namespaced by the SUB-template's ``spec_identity``. The template is
  loaded through the SAME loader the engine's ``load_workflow`` uses and its DAG
  is walked recursively (bounded by ``MAX_WORKFLOW_DEPTH``), with every child
  reported under ``sub[<ref>]:<node id>`` — the namespacing ``fold_nested``
  already uses, so the two readings match.

Counts are per CELL, not per node: a pipeline of 3 items x 2 stages contributes
6 to ``replay``. The LISTS stay per node — a fan-out entry carries ``cells``
(how many) and ``stages`` (which) instead of one line per item, so a 500-item
pipeline reports one readable entry rather than a thousand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lohra.workflow import artifact as artifacts
from lohra.workflow.artifact_paths import RunPaths
from lohra.workflow import refs
from lohra.workflow.cache import (
    MISS_ARTIFACT_CHANGED,
    MISS_IDENTITY_CHANGED,
    MISS_IDENTITY_CHANGED_OR_SIBLING,
    NodeCache,
    content_hash,
    spec_identity,
)
from lohra.workflow.graph import ref_roots, topological_order
from lohra.workflow.namespacing import sub_prefix
from lohra.workflow.nodes import Node, WorkflowSpec, checkpoint_accepts, resolve_schema
from lohra.workflow.strategies import STRATEGIES, stage_cell

logger = logging.getLogger(__name__)

# The ways a node still ends up unknowable.
UPSTREAM_UNKNOWN = "upstream_unknown"
NOT_RECOMPUTABLE = "not_recomputable"
# ...and the two that belong to a nested template: the ref names a template this
# process cannot load, or one that no longer validates. Either way the engine
# would run something this module cannot see the cells of, so the node is NAMED
# rather than counted as a free replay.
NESTED_UNAVAILABLE = "nested_template_unavailable"
NESTED_INVALID = "nested_template_invalid"


class _StopAtLookup(Exception):
    """Raised by the stand-in engine the instant a strategy asks the cache.

    The strategy has, by then, done every bit of identity work: this carries the
    key it computed and unwinds before anything can spawn."""

    def __init__(self, chash: str) -> None:
        super().__init__(chash)
        self.chash = chash


class _PreviewEngine:
    """Exactly what a strategy touches BEFORE its cache lookup — and no more.

    Deliberately NOT a WorkflowEngine: no core, no budget, no cache to write to,
    no audit sink. A strategy that tried to spawn, gate a fan-out or record
    progress here raises AttributeError, and the caller degrades that node to
    ``unknown`` instead of guessing."""

    def __init__(self, spec: WorkflowSpec, tiers: Any | None) -> None:
        self._schemas = spec.schemas
        self._spec_id = spec_identity(spec)
        self._tiers = tiers

    @property
    def tiers(self) -> Any | None:
        return self._tiers

    def record_fault(self, message: str) -> None:
        """A preview diagnoses nothing: the run itself will record the fault, and
        recording it twice would put a fault in the ledger for a run that has not
        started."""

    def resolve_schema(self, fields: dict) -> dict | None:
        return resolve_schema(self._schemas, fields)

    def cell_hash(self, *parts: Any) -> str:
        return content_hash(self._spec_id[0], self._spec_id[1], *parts)

    def cache_lookup(self, chash: str, node_id: str, **_: Any) -> tuple[bool, Any]:
        raise _StopAtLookup(chash)


@dataclass(frozen=True)
class _Ctx:
    """Everything the walk needs that does not change as it recurses."""

    cache: NodeCache
    tiers: Any | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    artifact_scope: Any | None = None
    loader: Any | None = None
    # Which cells of this run declared which artifact paths (#65). The preview
    # has to hold it for the same reason it holds the scope: it announces the
    # invalidations the ENGINE will perform, and an engine that keeps a replay
    # because a sibling explains the change must not be contradicted here.
    run_paths: Any | None = None


@dataclass
class _Tally:
    """The running verdict of one preview — counted per CELL, listed per node."""

    replay: int = 0
    invalidate: int = 0
    never_completed: int = 0
    tokens_to_repay: int = 0
    invalidated: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[dict[str, Any]] = field(default_factory=list)
    cost_unknown: list[str] = field(default_factory=list)

    def invalid(
        self, node_id: str, reason: str, *, cells: int = 1, stages: list[int] | None = None
    ) -> None:
        self.invalidate += cells
        self.invalidated.append(_entry({"node_id": node_id, "reason": reason}, cells, stages))

    def unknowable(
        self, node_id: str, why: str, *, cells: int = 1, stages: list[int] | None = None
    ) -> None:
        self.unknown.append(_entry({"node_id": node_id, "why": why}, cells, stages))

    def charge(self, cache: NodeCache, hashes: list[str], node_id: str) -> None:
        """What this run already paid for cells that will NOT replay. A cell with
        no price row contributes 0 and names its node in ``cost_unknown``, so an
        under-count is never silent."""
        priced = [cache.cell_tokens(old) for old in hashes]
        self.tokens_to_repay += sum(cost for cost in priced if cost is not None)
        if any(cost is None for cost in priced) and node_id not in self.cost_unknown:
            # Named ONCE per node: a fan-out with a hundred unpriced cells is one
            # fact about one node, not a hundred lines of it.
            self.cost_unknown.append(node_id)


def _entry(base: dict[str, Any], cells: int, stages: list[int] | None) -> dict[str, Any]:
    """One list line. ``stages`` is what marks a FAN-OUT entry: a scalar node has
    exactly one cell and stays byte-identical to what v1 reported."""
    if stages is None:
        return base
    return {**base, "cells": cells, "stages": stages}


def _cell_hash_of(engine: _PreviewEngine, node: Node, context: dict[str, Any]) -> str | None:
    """The key the engine WILL compute for this node, or None when the node
    resolves to nothing before ever reaching its cache (an upstream null, a
    container that is not a list): the engine nulls it without a lookup, so
    there is no cell to replay or invalidate."""
    try:
        STRATEGIES[node.type](engine, node, context)
    except _StopAtLookup as stop:
        return stop.chash
    return None


def _artifact_stale(
    artifact: dict[str, Any] | None, scope: Any | None, run_paths: Any | None = None
) -> bool:
    """True when a hit's declared artifact no longer matches what is on disk.

    Same rule the engine applies at the lookup (#45 E4) and, like it, read-only:
    a manifest the harness could not measure in the first place is not evidence
    of change, and a recheck that raises degrades to "replays" rather than
    announcing an invalidation the run may not actually make."""
    if artifact is None or artifact.get("verification") != artifacts.VERIFIED:
        return False
    try:
        return artifacts.recheck(artifact.get("entries"), scope, run_paths).stale
    except Exception:
        logger.debug("cache preview: artifact recheck failed", exc_info=True)
        return False


def _look(ctx: _Ctx, chash: str) -> tuple[bool, bool, Any]:
    """``(replays, stored, output)`` for one key.

    The two booleans are deliberately separate: a row that IS there but whose
    artifact moved on does not replay, and only that difference tells an
    ``artifact_changed`` invalidation from an ``identity_changed`` one."""
    stored, output, artifact = ctx.cache.get_with_artifact(chash)
    if stored and _artifact_stale(artifact, ctx.artifact_scope, ctx.run_paths):
        return (False, True, None)
    return (stored, stored, output)


def _bump(counts: dict[str, tuple[int, set[int]]], reason: str, stage_idx: int) -> None:
    """Aggregate one fan-out cell into its node's line: how many, which stages."""
    cells, stages = counts.get(reason, (0, set()))
    counts[reason] = (cells + 1, stages | {stage_idx})


def _preview_pipeline(
    ctx: _Ctx,
    engine: _PreviewEngine,
    node: Node,
    label: str,
    context: dict[str, Any],
    tally: _Tally,
    changed_reason: str,
) -> tuple[Any, bool]:
    """``(output, knowable)`` for one ``pipeline`` node, cell by cell (#61).

    Each item is walked independently, exactly as the no-barrier scheduler walks
    it: the cell that hits IS the ``${stage.result}`` the next stage's prompt
    interpolates, so a fully cached item's whole chain is recomputed with the
    engine's own keys. The first cell of an item that does not hit ENDS that
    item — its later stages depend on an output nobody has produced, and a hash
    guessed from a stale one would report invalidations that are nothing of the
    sort (D6).

    The node's output is knowable only when EVERY item walked its chain to the
    end: one unknown item leaves a list a downstream ``${ref}`` cannot read."""
    items = refs.resolve_value(node.fields.get("items"), context)
    stages = node.fields.get("stages")
    if not isinstance(stages, list) or not stages or not isinstance(items, list):
        # The engine records a fault and nulls the node without one lookup.
        return None, True
    if not items:
        return [], True
    results: list[Any] = [None] * len(items)
    knowable = True
    invalid: dict[str, tuple[int, set[int]]] = {}
    unknown_cells = 0
    unknown_stages: set[int] = set()
    for index, item in enumerate(items):
        prev: Any = item  # stage 0's "prev" is the item, exactly like ``_advance``
        for stage_idx, stage in enumerate(stages):
            identity = stage_cell(
                engine, node.id, stage, stage_idx, index, item, prev, context
            )
            if identity is None:
                break  # upstream null: the engine drops THIS item, no cell at all
            replays, stored, output = _look(ctx, identity.chash)
            if replays:
                tally.replay += 1
                prev = output
                if stage_idx == len(stages) - 1:
                    results[index] = output
                continue
            # This cell re-executes. Its node id in the cache is the COMPOSITE
            # one — unique to this (item, stage) — so unlike the engine's own
            # lookup, which asks under the SHARED node id and can only make the
            # weaker claim, a row here really is this cell's own (D6).
            if stored:
                _bump(invalid, MISS_ARTIFACT_CHANGED, stage_idx)
                tally.charge(ctx.cache, [identity.chash], label)
            else:
                seen = ctx.cache.hashes_for_node(identity.node_id)
                if seen:
                    _bump(invalid, changed_reason, stage_idx)
                    tally.charge(ctx.cache, seen, label)
                else:
                    tally.never_completed += 1
            # ...and every stage after it, for THIS item, is unknowable.
            unknown_cells += len(stages) - stage_idx - 1
            unknown_stages.update(range(stage_idx + 1, len(stages)))
            knowable = False
            break
    for reason, (cells, hit_stages) in invalid.items():
        tally.invalid(label, reason, cells=cells, stages=sorted(hit_stages))
    if unknown_cells:
        tally.unknowable(
            label, UPSTREAM_UNKNOWN, cells=unknown_cells, stages=sorted(unknown_stages)
        )
    return (results if knowable else None), knowable


def _preview_nested(
    ctx: _Ctx,
    node: Node,
    prefix: str,
    answer_prefix: str,
    context: dict[str, Any],
    tally: _Tally,
    depth: int,
) -> tuple[Any, bool, str | None]:
    """``(outputs, knowable, why)`` for one ``workflow`` node (#61).

    The node owns no cell: its CHILDREN do, under the sub-template's identity.
    Loaded through the same loader ``engine.load_workflow`` uses and walked
    recursively, so the child's ``(name, version)`` namespaces its keys exactly
    as the nested engine will write them.

    ``why`` is set only when nothing else in the report would explain the
    refusal — a template that will not load, or one that no longer validates. A
    child that merely walked to a miss needs no line of its own: its OWN entries,
    namespaced ``sub[<ref>]:<node>``, already say which cell and why.

    TWO prefixes go down, and they are not the same string. ``prefix`` names
    cells for the REPORT and is keyed by the template (``sub[<ref>]:``) — the
    externally documented shape, which #61 published and callers read. The
    ``answer_prefix`` is keyed by this NODE (``sub[<node id>]:``), because that
    is how the engine will look a human's checkpoint answer up (#78): two nodes
    may run one template with different args, and their gates are two different
    questions. Threading them separately is the whole point — collapsing them
    would either rename every reported cell or re-introduce the collision."""
    from lohra.workflow.engine import MAX_WORKFLOW_DEPTH
    from lohra.workflow.schema import ValidationError, validate_spec

    if depth >= MAX_WORKFLOW_DEPTH:
        # The engine raises here and nulls the node: deterministic, and no
        # lookup ever happens — so there is nothing to replay or re-pay.
        return None, True, None
    ref = node.fields.get("ref")
    if not isinstance(ref, str):
        return None, True, None  # the engine returns None without a cell
    spec_dict = ctx.loader(ref) if ctx.loader is not None else None
    if spec_dict is None:
        return None, False, NESTED_UNAVAILABLE
    parsed = validate_spec(spec_dict, supported_types=frozenset(STRATEGIES))
    if isinstance(parsed, ValidationError):
        return None, False, NESTED_INVALID
    sub_args = refs.resolve_value(node.fields.get("args") or {}, context)
    if not isinstance(sub_args, dict):
        sub_args = {}
    outputs = _walk(
        ctx, parsed, sub_args, tally,
        prefix=f"{prefix}{sub_prefix(ref)}",
        answer_prefix=f"{answer_prefix}{sub_prefix(node.id)}",
        depth=depth + 1,
    )
    return outputs, outputs is not None, None


def _settle(
    context: dict[str, Any],
    outputs: dict[str, Any],
    node_id: str,
    output: Any,
    knowable: bool,
    unknown_roots: set[str],
) -> None:
    """Record what downstream will see — or that it cannot be known from here."""
    if not knowable:
        unknown_roots.add(node_id)
        return
    context[node_id] = output
    outputs[node_id] = output


def _walk(
    ctx: _Ctx,
    spec: WorkflowSpec,
    args: dict[str, Any],
    tally: _Tally,
    *,
    prefix: str = "",
    answer_prefix: str = "",
    depth: int = 0,
) -> dict[str, Any] | None:
    """Cross one DAG's cells with the cache, accumulating into ``tally``.

    Returns the node outputs a CALLER can resolve refs against, or None when any
    node of this DAG is unknowable — a nested template with one miss inside it
    cannot tell its parent what ``${sub.x}`` will hold."""
    engine = _PreviewEngine(spec, ctx.tiers)
    # A nested template's node ids share the cache's node_id column with the
    # parent's, so "a row under another hash" cannot be pinned on an identity
    # change while such a collision is possible (the pipeline half of D6, one
    # level up). Same rule the engine's ``_miss_reason`` applies at ``depth > 0``.
    nested = depth > 0 or any(node.type == "workflow" for node in spec.nodes)
    changed_reason = MISS_IDENTITY_CHANGED_OR_SIBLING if nested else MISS_IDENTITY_CHANGED

    context: dict[str, Any] = {"args": args}
    outputs: dict[str, Any] = {}
    unknown_roots: set[str] = set()

    def give_up(node_id: str, label: str, why: str) -> None:
        unknown_roots.add(node_id)
        tally.unknowable(label, why)

    for node in topological_order(spec):
        label = f"{prefix}{node.id}"
        if ref_roots(node.fields) & unknown_roots:
            # An ancestor's output is not knowable, so this node's prompt cannot
            # be resolved the way the engine will resolve it.
            give_up(node.id, label, UPSTREAM_UNKNOWN)
            continue
        try:
            if node.type == "pipeline":
                output, knowable = _preview_pipeline(
                    ctx, engine, node, label, context, tally, changed_reason
                )
                _settle(context, outputs, node.id, output, knowable, unknown_roots)
                continue
            if node.type == "workflow":
                output, knowable, why = _preview_nested(
                    ctx, node, prefix, answer_prefix, context, tally, depth
                )
                if why is not None:
                    give_up(node.id, label, why)
                    continue
                _settle(context, outputs, node.id, output, knowable, unknown_roots)
                continue
            chash = _cell_hash_of(engine, node, context)
        except Exception:
            # A strategy step the stand-in cannot answer. Never a guess: the
            # node is named as unknown and everything downstream of it too.
            logger.debug("cache preview: %r is not recomputable", node.id, exc_info=True)
            give_up(node.id, label, NOT_RECOMPUTABLE)
            continue
        if chash is None:
            # The engine nulls this node without ever asking the cache; a null
            # output is exactly what downstream will see.
            _settle(context, outputs, node.id, None, True, unknown_roots)
            continue
        replays, stored, output = _look(ctx, chash)
        if replays:
            tally.replay += 1
            _settle(context, outputs, node.id, output, True, unknown_roots)
            continue
        if stored:
            # The key is identical and the row is right there — the FILE the
            # cell declared moved on, so the engine will refuse this hit and
            # re-spawn (#45 E4). Announced here, before anything is paid for.
            tally.invalid(label, MISS_ARTIFACT_CHANGED)
            tally.charge(ctx.cache, [chash], label)
        else:
            seen = ctx.cache.hashes_for_node(node.id)
            if seen:
                tally.invalid(label, changed_reason)
                tally.charge(ctx.cache, seen, label)
            else:
                tally.never_completed += 1
        answer_key = f"{answer_prefix}{node.id}"
        if node.type == "checkpoint" and answer_key in ctx.answers:
            # A human already answered this one: the engine will hand the answer
            # straight back (and cache it) without asking again, so downstream
            # stays computable — but only if the answer RELEASES the gate (#74).
            # Looked up by the ANSWER key, which is neither the bare id nor the
            # report label: one level down the engine reads the answer under
            # ``sub[<workflow node id>]:<id>`` (#78) while the label is keyed by
            # the TEMPLATE. Matching the label here would promise a replay for a
            # gate that is about to pause the moment two nodes call one
            # template; matching the bare id would do it always.
            # A rejected answer nulls the node, and promising the dependent will
            # run on it is exactly the claim the preview exists to get right.
            answer = ctx.answers[answer_key]
            if checkpoint_accepts(answer, node.fields.get("accept")):
                _settle(context, outputs, node.id, answer, True, unknown_roots)
            else:
                _settle(context, outputs, node.id, None, True, unknown_roots)
        else:
            unknown_roots.add(node.id)  # its output is not knowable from here
    return None if unknown_roots else outputs


def preview_resume(
    db: Any,
    run_id: str,
    spec: WorkflowSpec,
    args: dict[str, Any] | None = None,
    *,
    tiers: Any | None = None,
    checkpoint_answers: dict[str, Any] | None = None,
    artifact_scope: Any | None = None,
    loader: Any | None = None,
) -> dict[str, Any]:
    """``{replay, invalidate, never_completed, tokens_to_repay, invalidated}``.

    The three counters are CELLS: a ``pipeline`` of N items through M stages
    contributes N*M of them, and a nested ``workflow`` contributes its children's
    (#61). The lists stay per NODE — a fan-out line carries ``cells`` and
    ``stages`` rather than one line per item.

    ``tokens_to_repay`` is what this run ALREADY paid for cells that will not
    replay — all five meters summed, the axis the #44 investigation reports.
    A cell with no price row contributes 0 and its node is named in
    ``cost_unknown``, so an under-count is never silent.

    Adds ``unknown`` (nodes whose key this version cannot recompute, each with a
    ``why``) and ``cost_unknown`` only when non-empty.

    ``loader`` resolves a ``workflow`` node's ref to its template dict — the same
    callable the engine's ``load_workflow`` holds. Without it a nested node
    reports ``unknown`` rather than pretending its children are free.

    A cell that HITS but whose artifact manifest no longer matches the
    filesystem is reported as an invalidation with ``reason:
    artifact_changed`` (#45 E4) — the engine will refuse that hit and re-spawn,
    and the whole point of this module is that it says so before the run pays
    for it."""
    cache = NodeCache(db, run_id)
    ctx = _Ctx(
        cache=cache,
        run_paths=RunPaths.load(cache),
        tiers=tiers,
        answers=dict(checkpoint_answers or {}),
        artifact_scope=artifact_scope,
        loader=loader,
    )
    tally = _Tally()
    _walk(ctx, spec, args or {}, tally)
    preview: dict[str, Any] = {
        "replay": tally.replay,
        "invalidate": tally.invalidate,
        "never_completed": tally.never_completed,
        "tokens_to_repay": tally.tokens_to_repay,
        "invalidated": tally.invalidated,
    }
    if tally.unknown:
        preview["unknown"] = tally.unknown
    if tally.cost_unknown:
        preview["cost_unknown"] = tally.cost_unknown
    return preview
