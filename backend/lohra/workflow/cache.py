"""Content-addressed node cache for resume (spec §6).

The lookup key is the cell's CONTENT hash (canonical spec + resolved inputs), not
a positional ordinal — so reordering/inserting a node doesn't false-miss the
others. Scope is the run (cross-run reuse OFF, §6.3: a hit from a different run
would replay another run's stochastic LLM output — a correctness hazard).

Only SUCCESSFUL completions are cached. A cell that died or produced
schema-invalid output gets no row, so a resume re-spawns it (LLM leaves are
stochastic — a transient failure should be retryable, not permanently poisoned).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from lohra.agent.types import Usage

COMPLETE = "complete"


def content_hash(*parts: Any) -> str:
    """Stable sha256 over canonical JSON of the parts (order-sensitive)."""
    canonical = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def spec_identity(spec: Any) -> tuple[Any, Any]:
    """The ``(name, version)`` every cell of a run is namespaced by (§6.2).

    ONE definition, read by the engine that WRITES the cells and by anything
    that recomputes their keys later (``cache_preview``, ``segment.started``).
    The defaults matter as much as the values: a recomputation that defaulted
    ``version`` to ``None`` where the engine defaults it to ``0`` would miss
    every row of a spec with no version and report a mass invalidation that is
    not happening.
    """
    meta = getattr(spec, "meta", None) or {}
    return (meta.get("name", ""), meta.get("version", 0))


class NodeCache:
    """Run-scoped get/put over the SessionDB workflow_node_cache table."""

    def __init__(
        self,
        db: Any,
        run_id: str,
        on_write: Callable[[], None] | None = None,
        *,
        fence: int | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        # The ownership fence of the stretch this cache belongs to (issue #12).
        # Cells are written from PIPELINE POOL WORKERS, so a stale owner that
        # wakes up finishes its leaf and stores a cell over the new owner's —
        # SQLite refuses it while this fence is behind. None (a read-only
        # NodeCache, or a caller from before the fence) writes as it always did.
        self._fence = fence
        # Every cached cell also refreshes the run's cross-process lease (WF-29).
        # It is the cheap top-up, never the guarantee: a node that outlives the
        # TTL completes nothing, so the lease's real pace is the timer heartbeat
        # (lease_heartbeat.py). Optional, and never load-bearing for a cell.
        self._on_write = on_write

    def get(self, chash: str) -> tuple[bool, Any]:
        """(hit, output). A miss is (False, None); a cached completion is
        (True, output) — the stored output is always a real completion."""
        row = self._db.cache_get(self._run_id, chash)
        if row is None:
            return (False, None)
        raw = row.get("output_json")
        return (True, json.loads(raw) if isinstance(raw, str) else None)

    def put_complete(self, chash: str, node_id: str, output: Any, cost: Usage | None = None) -> None:
        """Store the completion AND what it cost (spec §7.1).

        The cost is what lets a resume count work already paid for: a cell that
        replays from here spawns nothing, so nothing would otherwise charge the
        run's token budget for it and a resume loop could spend without limit.
        Written to a sidecar row, so a pre-M5 cache row stays readable at 0.

        ``cost`` is a whole ``Usage`` (Fatia C) rather than the two numbers the
        budget charges: the cache meters are what make a replayed cell's price
        honest on screen, and dropping them here would put a zero in the ledger
        forever. None (a human's checkpoint answer) spent no leaf at all."""
        priced = cost is not None and any(
            (cost.input_tokens, cost.output_tokens, cost.cache_read_tokens,
             cost.cache_write_tokens, cost.reasoning_tokens)
        )
        # ONE call, one transaction, one guard (issue #12): a cell stored with
        # its price refused is a cell that replays for free on the next resume,
        # so the two are never two writes a new owner can arrive between. A
        # refusal drops both — the cache and the ledger keep telling the same
        # story, and db.py logged it.
        stored = self._db.cache_put_with_cost(
            self._run_id,
            chash,
            node_id,
            json.dumps(output, ensure_ascii=False, default=str),
            COMPLETE,
            cost=(
                (
                    cost.input_tokens,
                    cost.output_tokens,
                    cost.cache_read_tokens,
                    cost.cache_write_tokens,
                    cost.reasoning_tokens,
                )
                if priced
                else None
            ),
            fence=self._fence,
        )
        if not stored:
            return
        if self._on_write is not None:
            self._on_write()

    def total_cost(self) -> tuple[int, int]:
        """(tokens_in, tokens_out) over every cell this run has cached — the two
        axes the token budget charges (deliberately not widened by Fatia C)."""
        return self._db.cache_cost_total(self._run_id)

    def total_split(self) -> Usage:
        """Everything this run's cached cells cost, all four meters — the REPORT
        half of ``total_cost``, so a resumed run can still say what it saved."""
        tokens_in, tokens_out = self._db.cache_cost_total(self._run_id)
        cache_read, cache_write, reasoning = self._db.cache_cost_split(self._run_id)
        return Usage(
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            reasoning_tokens=reasoning,
        )
