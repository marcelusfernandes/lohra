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

COMPLETE = "complete"


def content_hash(*parts: Any) -> str:
    """Stable sha256 over canonical JSON of the parts (order-sensitive)."""
    canonical = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NodeCache:
    """Run-scoped get/put over the SessionDB workflow_node_cache table."""

    def __init__(
        self, db: Any, run_id: str, on_write: Callable[[], None] | None = None
    ) -> None:
        self._db = db
        self._run_id = run_id
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

    def put_complete(
        self,
        chash: str,
        node_id: str,
        output: Any,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Store the completion AND what it cost (spec §7.1).

        The cost is what lets a resume count work already paid for: a cell that
        replays from here spawns nothing, so nothing would otherwise charge the
        run's token budget for it and a resume loop could spend without limit.
        Written to a sidecar row, so a pre-M5 cache row stays readable at 0."""
        self._db.cache_put(
            self._run_id, chash, node_id, json.dumps(output, ensure_ascii=False, default=str), COMPLETE
        )
        if tokens_in or tokens_out:
            self._db.cache_cost_put(self._run_id, chash, tokens_in, tokens_out)
        if self._on_write is not None:
            self._on_write()

    def total_cost(self) -> tuple[int, int]:
        """(tokens_in, tokens_out) over every cell this run has cached."""
        return self._db.cache_cost_total(self._run_id)
