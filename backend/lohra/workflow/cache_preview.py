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
knowable (a miss, or a node this module does not cover) every node downstream of
it becomes ``unknown`` — an honest refusal beats a confident wrong hash.

NOT covered in v1, reported as ``unknown``:
- ``pipeline`` — one cell per (item, stage), and stage N's prompt interpolates
  stage N-1's OUTPUT. Chaining that through the cached per-cell outputs is
  feasible and is v2; getting it half right would report invalidations that are
  really sibling cells (D6).
- ``workflow`` — a nested node owns no cell of its own; its children are
  namespaced by the SUB-template's identity, which needs the template loader and
  a second recomputation pass.
"""

from __future__ import annotations

import logging
from typing import Any

from lohra.workflow import artifact as artifacts
from lohra.workflow.cache import (
    MISS_ARTIFACT_CHANGED,
    MISS_IDENTITY_CHANGED,
    MISS_IDENTITY_CHANGED_OR_SIBLING,
    NodeCache,
    content_hash,
    spec_identity,
)
from lohra.workflow.graph import ref_roots, topological_order
from lohra.workflow.nodes import Node, WorkflowSpec, resolve_schema
from lohra.workflow.strategies import STRATEGIES

logger = logging.getLogger(__name__)

# The node types whose keys this version does not recompute (see the docstring).
_OUT_OF_SCOPE = {"pipeline": "pipeline_fanout", "workflow": "nested_workflow"}
# ...and the two ways a covered node still ends up unknowable.
UPSTREAM_UNKNOWN = "upstream_unknown"
NOT_RECOMPUTABLE = "not_recomputable"


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


def _unknown(node_id: str, why: str) -> dict[str, str]:
    return {"node_id": node_id, "why": why}


def _artifact_stale(artifact: dict[str, Any] | None, scope: Any | None) -> bool:
    """True when a hit's declared artifact no longer matches what is on disk.

    Same rule the engine applies at the lookup (#45 E4) and, like it, read-only:
    a manifest the harness could not measure in the first place is not evidence
    of change, and a recheck that raises degrades to "replays" rather than
    announcing an invalidation the run may not actually make."""
    if artifact is None or artifact.get("verification") != artifacts.VERIFIED:
        return False
    try:
        return artifacts.recheck(artifact.get("entries"), scope).stale
    except Exception:
        logger.debug("cache preview: artifact recheck failed", exc_info=True)
        return False


def preview_resume(
    db: Any,
    run_id: str,
    spec: WorkflowSpec,
    args: dict[str, Any] | None = None,
    *,
    tiers: Any | None = None,
    checkpoint_answers: dict[str, Any] | None = None,
    artifact_scope: Any | None = None,
) -> dict[str, Any]:
    """``{replay, invalidate, never_completed, tokens_to_repay, invalidated}``.

    ``tokens_to_repay`` is what this run ALREADY paid for cells that will not
    replay — all five meters summed, the axis the #44 investigation reports.
    A cell with no price row contributes 0 and its node is named in
    ``cost_unknown``, so an under-count is never silent.

    Adds ``unknown`` (nodes whose key this version cannot recompute, each with a
    ``why``) and ``cost_unknown`` only when non-empty.

    A cell that HITS but whose artifact manifest no longer matches the
    filesystem is reported as an invalidation with ``reason:
    artifact_changed`` (#45 E4) — the engine will refuse that hit and re-spawn,
    and the whole point of this module is that it says so before the run pays
    for it."""
    cache = NodeCache(db, run_id)
    engine = _PreviewEngine(spec, tiers)
    answers = dict(checkpoint_answers or {})
    # A nested template's node ids share the cache's node_id column with the
    # parent's, so "a row under another hash" cannot be pinned on an identity
    # change while such a collision is possible (the pipeline half of D6, one
    # level up).
    nested = any(node.type == "workflow" for node in spec.nodes)
    changed_reason = MISS_IDENTITY_CHANGED_OR_SIBLING if nested else MISS_IDENTITY_CHANGED

    context: dict[str, Any] = {"args": args or {}}
    unknown_roots: set[str] = set()
    unknown: list[dict[str, str]] = []
    invalidated: list[dict[str, str]] = []
    cost_unknown: list[str] = []
    replay = never_completed = tokens_to_repay = 0

    def give_up(node_id: str, why: str) -> None:
        unknown_roots.add(node_id)
        unknown.append(_unknown(node_id, why))

    def _charge(cache: NodeCache, hashes: list[str], node_id: str) -> None:
        """What this run already paid for cells that will NOT replay. A cell with
        no price row contributes 0 and names its node in ``cost_unknown``, so an
        under-count is never silent."""
        nonlocal tokens_to_repay
        priced = [cache.cell_tokens(old) for old in hashes]
        tokens_to_repay += sum(cost for cost in priced if cost is not None)
        if any(cost is None for cost in priced):
            cost_unknown.append(node_id)

    for node in topological_order(spec):
        out_of_scope = _OUT_OF_SCOPE.get(node.type)
        if out_of_scope is not None:
            give_up(node.id, out_of_scope)
            continue
        if ref_roots(node.fields) & unknown_roots:
            # An ancestor's output is not knowable, so this node's prompt cannot
            # be resolved the way the engine will resolve it.
            give_up(node.id, UPSTREAM_UNKNOWN)
            continue
        try:
            chash = _cell_hash_of(engine, node, context)
        except Exception:
            # A strategy step the stand-in cannot answer. Never a guess: the
            # node is named as unknown and everything downstream of it too.
            logger.debug("cache preview: %r is not recomputable", node.id, exc_info=True)
            give_up(node.id, NOT_RECOMPUTABLE)
            continue
        if chash is None:
            # The engine nulls this node without ever asking the cache; a null
            # output is exactly what downstream will see.
            context[node.id] = None
            continue
        hit, output, artifact = cache.get_with_artifact(chash)
        if hit and not _artifact_stale(artifact, artifact_scope):
            replay += 1
            context[node.id] = output
            continue
        if hit:
            # The key is identical and the row is right there — the FILE the
            # cell declared moved on, so the engine will refuse this hit and
            # re-spawn (#45 E4). Announced here, before anything is paid for.
            invalidated.append({"node_id": node.id, "reason": MISS_ARTIFACT_CHANGED})
            _charge(cache, [chash], node.id)
        else:
            seen = cache.hashes_for_node(node.id)
            if seen:
                invalidated.append({"node_id": node.id, "reason": changed_reason})
                _charge(cache, seen, node.id)
            else:
                never_completed += 1
        if node.type == "checkpoint" and node.id in answers:
            # A human already answered this one: the engine will hand the answer
            # straight back (and cache it) without asking again, so downstream
            # stays computable.
            context[node.id] = answers[node.id]
        else:
            unknown_roots.add(node.id)  # its output is not knowable from here

    preview: dict[str, Any] = {
        "replay": replay,
        "invalidate": len(invalidated),
        "never_completed": never_completed,
        "tokens_to_repay": tokens_to_repay,
        "invalidated": invalidated,
    }
    if unknown:
        preview["unknown"] = unknown
    if cost_unknown:
        preview["cost_unknown"] = cost_unknown
    return preview
