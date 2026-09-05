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
import logging
from typing import Any, Callable

from lohra.agent.types import Usage

logger = logging.getLogger(__name__)

COMPLETE = "complete"

# WHY a lookup missed, decided at the lookup itself (#44 épico 3).
#
# It cannot be recovered afterwards: the audit's ``cell_id`` is the STRUCTURAL
# identity (run/role/node_path/branch/item/stage), deliberately not the content
# hash, so a miss and a replay of the same node are byte-identical there. Only
# the engine, holding both the recomputed key and the run's stored rows, can
# tell "this node never completed" from "its identity changed".
MISS_NEVER_COMPLETED = "never_completed"
MISS_IDENTITY_CHANGED = "identity_changed"
# ...and the honest weaker claim where a node id is SHARED by many cells — a
# pipeline stores every (item, stage) cell under the raw node id, and a nested
# template's node ids live in the same column as its parent's. A row with
# another hash there may be a sibling, not a changed identity (D6).
MISS_IDENTITY_CHANGED_OR_SIBLING = "identity_changed_or_sibling"
# ...and the one miss whose cause is not the KEY at all (#45 E4): the identity is
# byte-identical and the row is right there, but the file the cell declared has
# changed underneath it. Replaying would re-assert a description of content that
# no longer exists, so the hit is refused and the node re-spawns.
MISS_ARTIFACT_CHANGED = "artifact_changed"


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
        stamp: Any | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        # Under WHAT this process would run a leaf (#75): the operator's
        # effective sandbox policy and the harness version, as a ``CellStamp``.
        # Written on every LEAF cell this cache stores and compared on every hit.
        # None — a read-only NodeCache (``spend``, ``cache_preview``) or a caller
        # from before the stamp existed — writes NULLs and compares nothing:
        # "no record" is not "different", and a reader holding no policy must
        # never invent a divergence.
        self._stamp = stamp
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

    @property
    def stamp(self) -> Any | None:
        """What a cell stored by THIS cache carries (#75), or None when this
        cache stamps nothing."""
        return self._stamp

    def get(self, chash: str) -> tuple[bool, Any]:
        """(hit, output). A miss is (False, None); a cached completion is
        (True, output) — the stored output is always a real completion."""
        hit, output, _ = self.get_with_artifact(chash)
        return (hit, output)

    def get_with_stamp(
        self, chash: str
    ) -> tuple[bool, Any, dict[str, Any] | None, dict[str, Any]]:
        """``get_with_artifact`` PLUS the raw stamp columns (#75), in one read.

        The stamp comes back as the row's own two fields rather than a
        ``CellStamp``: ``cell_stamp`` reads this module's ``content_hash``, so
        the dependency only ever points one way. The caller — the engine, which
        holds the CURRENT stamp — is the one that can compare them."""
        hit, output, artifact, row = self._read(chash)
        stamp = {
            "policy_hash": row.get("policy_hash") if row else None,
            "harness_version": row.get("harness_version") if row else None,
        }
        return (hit, output, artifact, stamp)

    def get_with_artifact(self, chash: str) -> tuple[bool, Any, dict[str, Any] | None]:
        """(hit, output, artifact) — the cell PLUS what the harness measured for
        it (#45 E4), in one read.

        ``artifact`` is ``{"verification": ..., "entries": [...]}`` or None: a
        cell stored without a manifest, and every row written before this
        existed, reads as None and replays exactly as it always did. A row whose
        stored entries are unparseable degrades to no entries rather than
        raising — a corrupt sidecar must never take a run down."""
        hit, output, artifact, _ = self._read(chash)
        return (hit, output, artifact)

    def _read(
        self, chash: str
    ) -> tuple[bool, Any, dict[str, Any] | None, dict[str, Any] | None]:
        """(hit, output, artifact, row) — ONE lookup, every sidecar it carries."""
        row = self._db.cache_get(self._run_id, chash)
        if row is None:
            return (False, None, None, None)
        raw = row.get("output_json")
        output = json.loads(raw) if isinstance(raw, str) else None
        verification = row.get("artifact_verification")
        if not isinstance(verification, str) or not verification:
            return (True, output, None, row)
        entries: Any = []
        stored = row.get("artifact_json")
        if isinstance(stored, str):
            try:
                entries = json.loads(stored)
            except ValueError:
                logger.warning("workflow: unreadable artifact manifest on cell %s", chash)
        return (True, output, {"verification": verification, "entries": entries}, row)

    def put_complete(
        self,
        chash: str,
        node_id: str,
        output: Any,
        cost: Usage | None = None,
        *,
        leaf_count: int = 1,
        artifact: tuple[str, str] | None = None,
        stamped: bool = True,
    ) -> None:
        """Store the completion AND what it cost (spec §7.1).

        The cost is what lets a resume count work already paid for: a cell that
        replays from here spawns nothing, so nothing would otherwise charge the
        run's token budget for it and a resume loop could spend without limit.
        Written to a sidecar row, so a pre-M5 cache row stays readable at 0.

        ``cost`` is a whole ``Usage`` (Fatia C) rather than the two numbers the
        budget charges: the cache meters are what make a replayed cell's price
        honest on screen, and dropping them here would put a zero in the ledger
        forever. None (a human's checkpoint answer) spent no leaf at all.

        ``leaf_count`` is how many leaves that price covers (#71): a fan-out
        caches ONE cell for its whole width, and a resume that divided this
        run's spend by ROWS read a cost per leaf that many times too high.

        ``artifact`` is ``(verification, manifest_json)`` when this cell declared
        an artifact manifest the harness measured (#45 E4) — written in the SAME
        guarded transaction as the cell, never as a follow-up write a fence
        refusal could drop while the cell itself survives, unverified forever.

        ``stamped`` is False for a cell no sandbox ever governed — a human's
        checkpoint answer (#75). Stamping one would make a later policy change
        raise an advisory about a cell the policy never touched: the person
        answered, and no operator knob could have changed that."""
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
            leaf_count=leaf_count,
            fence=self._fence,
            artifact=artifact,
            stamp=(
                self._stamp.columns
                if stamped and self._stamp is not None
                else None
            ),
        )
        if not stored:
            return
        if self._on_write is not None:
            self._on_write()

    def hashes_for_node(self, node_id: str, *, include_fanout: bool = False) -> list[str]:
        """Every cell this run has stored FOR THIS NODE (read-only, #44).

        The discriminator behind a miss reason: no row at all means the node
        never completed; a row under another hash means the identity moved.
        ``include_fanout`` also counts the per-(item, stage) rows a pipeline
        stores under a COMPOSITE node id — where the answer only supports the
        weaker "changed or sibling" claim."""
        return self._db.cache_hashes_for_node(
            self._run_id, node_id, include_fanout=include_fanout
        )

    def artifact_rows(self) -> list[tuple[str, str]]:
        """``(node_id, artifact_json)`` for every measured cell of this run (#65).

        What ``artifact_paths.RunPaths`` indexes: which CELLS declared which
        paths. Read-only and run-wide — the answer has to outlive the stretch
        that wrote the rows, because the cell a sibling's write invalidates is
        the one stored FIRST, in an earlier stretch."""
        return self._db.cache_artifact_rows(self._run_id)

    def cell_tokens(self, chash: str) -> int | None:
        """What one cell cost, ALL FIVE meters summed — or None when nothing
        priced it (a cell cached before M5, or a human's checkpoint answer).

        Five, not the budget's two: the number a reader compares against is what
        the work really cost — the 2.13M headline of the #44 investigation is
        in+out+cache_read+cache_write+reasoning, the same axis ``total_split``
        reports. None is not 0: "unknown price" and "free" are different facts."""
        row = self._db.cache_cost_of(self._run_id, chash)
        return None if row is None else sum(row)

    def total_cost(self) -> tuple[int, int]:
        """(tokens_in, tokens_out) over every cell this run has cached — the two
        axes the token budget charges (deliberately not widened by Fatia C)."""
        return self._db.cache_cost_total(self._run_id)

    def cost_count(self) -> int:
        """How many LEAVES of this run carry a real price — the denominator that
        lets a RESUME keep this run's own measured cost per leaf (issue #71).
        Not a row count: one cached cell can have paid for a whole fan-out."""
        return self._db.cache_cost_count(self._run_id)

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
