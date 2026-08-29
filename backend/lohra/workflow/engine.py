"""WorkflowEngine — the tree-walking interpreter over a validated node DAG.

It does NOT execute code: it pattern-matches on ``node.type`` and dispatches to a
strategy (strategies.py). Deterministic control flow is entirely here;
intelligence is only at the leaves. Each node runs under an engine-fault
try/except so one node's internal failure is recorded and nulled — the run
continues — distinct from a leaf returning ``None`` (spec §7.5).
"""

from __future__ import annotations

from dataclasses import replace
import logging
import threading
from typing import Any
from uuid import uuid4

from lohra.agent.types import Usage, combine_usage
from lohra.orchestration.core import CANCELLED
from lohra.providers.errors import QUOTA_EXHAUSTED
from lohra.workflow.audit import causal_audit_event
from lohra.workflow.budget import (
    TOKEN_BUDGET_EXHAUSTED,
    Budget,
    FanoutRejected,
    LifetimeExhausted,
    TokenBudgetExhausted,
)
from lohra.workflow.cache import content_hash
from lohra.workflow.causality import CausalContext
from lohra.workflow.events import FAULT, ITEMS, NODE
from lohra.workflow.accounting import NodeCost, RunResult, derive_status, leaf_usage
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.graph import topological_order
from lohra.workflow.nodes import Node, WorkflowSpec, node_timeout
from lohra.workflow.progress import COMPLETE, NULL, RUNNING, ProgressTracker
from lohra.workflow.strategies import LEAF_TIMEOUT, STRATEGIES
from lohra.workflow.validation import (
    MAX_VALIDATION_RETRIES,
    correction_prompt,
    is_empty_output,
    parse_and_validate,
)

logger = logging.getLogger(__name__)

# A `workflow` node runs another workflow inline; recursion is hard-capped here.
MAX_WORKFLOW_DEPTH = 1

# The pause reason for a run an OPERATOR stopped on purpose (M6). A third
# sibling of quota_exhausted / token_budget_exhausted: same resumable stop,
# and again a different remedy — nothing but the operator will resume it.
USER_PAUSE = "user_requested"

# A dead leaf's cause is quoted into the fault; bound it so one huge stack trace
# can't drown the rollup the agent polls.
MAX_FAULT_CAUSE_CHARS = 200

# Resolve a workflow `ref` (a template name) to its spec dict, or None.



class PauseSignal:
    """The run's "stop scheduling, but resumably" latch, recorded exactly once.

    Two causes share it, because the run-level consequence is identical: a 429 is
    not one leaf's bad luck (every sibling and every downstream node would fail
    the same way) and neither is an exhausted token budget. Stopping beats
    nulling the spec node by node — which would also teach ``library`` that the
    SHAPE was at fault.

    The CAUSE still matters, so the latch carries its ``reason`` (and, for a
    ``checkpoint``, the ``payload`` describing what it is waiting for): the first
    reporter wins and the run reports why it really stopped. What differs
    downstream is the remedy, not the mechanism — a quota refills itself with
    time, a budget only when a human raises it.

    Shared by reference with nested engines — one pause stops the whole run, not
    just the sub-workflow that met it. Leaves report from concurrent on_done
    workers, so set + record are one atomic step.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hit = False
        self.node: str = ""
        self.reason: str = ""
        self.retry_after: float | None = None
        # What the pause is waiting for, when saying so takes more than a
        # reason string (a checkpoint's node id + question + default).
        self.payload: dict | None = None

    @property
    def hit(self) -> bool:
        with self._lock:
            return self._hit

    def record(
        self,
        node: str,
        reason: str,
        retry_after: float | None = None,
        payload: dict | None = None,
    ) -> bool:
        """Latch the pause. True only for the FIRST reporter (who logs the
        fault) — a fan-out of N rejected leaves must not write N faults."""
        with self._lock:
            if self._hit:
                return False
            self._hit = True
            self.node = node
            self.reason = reason
            self.retry_after = retry_after
            self.payload = payload
            return True



class WorkflowEngine:
    """Runs one validated workflow over one OrchestrationCore."""

    def __init__(
        self,
        core: Any,
        *,
        budget: Budget,
        run_root: str | None = None,
        cache: Any | None = None,
        loader: Any | None = None,
        depth: int = 0,
        client_pool: Any | None = None,
        cancel_event: threading.Event | None = None,
        pause: PauseSignal | None = None,
        tiers: Any | None = None,
        checkpoint_answers: dict[str, Any] | None = None,
        on_event: Any | None = None,
        on_audit: Any | None = None,
        run_id: str | None = None,
        segment_id: str | None = None,
        node_scope: tuple[str, ...] = (),
    ) -> None:
        self._core = core
        self._budget = budget
        self._run_root = run_root
        # Stable run identity + one fresh execution segment per engine stretch.
        # Nested engines share both and add only a node scope.
        self._run_id = run_id or uuid4().hex
        self._segment_id = segment_id or uuid4().hex
        self._node_scope = tuple(node_scope)
        self._cache = cache
        self._loader = loader  # resolve a workflow ref -> spec dict (for nesting)
        self._depth = depth
        self._client_pool = client_pool  # cross-provider leaf clients (may be None)
        self._tiers = tiers  # operator model-tier map (WF-5); None = nothing mapped
        # Answers a human gave to this run's checkpoints, keyed by node id (WF-10).
        self._checkpoint_answers = dict(checkpoint_answers or {})
        self._schemas: dict[str, Any] = {}
        self._spec_id: tuple[Any, Any] = ("", 0)
        self._result = RunResult()
        self._accounted: set[str] = set()  # leaf sub_ids already folded into the rollup
        self._costs: dict[str, Usage] = {}  # sub_id -> everything that leaf spent
        self._leaf_node: dict[str, str] = {}  # sub_id -> the node that spawned it
        self._result_lock = threading.Lock()  # guards off-thread _result writes (pipeline on_done)
        self._current_node: str = "?"  # attribution for faults raised inside a strategy
        # Live per-node progress (M6), read mid-run by workflow_status off this
        # very engine — the same live read the token budget already relies on.
        self._progress = ProgressTracker()
        # ...and the PUSH half (WF-30): (kind, payload) for whoever is watching.
        # Deliberately NOT passed to nested_engine: an event's scope is one DAG,
        # exactly like the tracker's.
        self._on_event = on_event
        # Durable metadata audit is distinct from the sampled live-view stream.
        # It is passed to nested engines and fail-isolated by AuditTrail.
        self._on_audit = on_audit
        # One stop-flag for the whole run, read by the node loop AND the pipeline
        # scheduler (which runs on other threads) — hence an Event, not a bool.
        self._cancel = cancel_event if cancel_event is not None else threading.Event()
        self._pause = pause if pause is not None else PauseSignal()
        # Every leaf this run started, so a quota pause can cancel the ones still
        # in flight. Appended from concurrent pipeline workers -> under the lock.
        self._spawned: list[str] = []

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def cancelled(self) -> bool:
        """True once someone asked this run to stop scheduling work."""
        return self._cancel.is_set()

    def request_cancel(self) -> None:
        """Stop the run at the next node / pipeline step. Cooperative: it never
        aborts a provider call in flight (that's the core's job, via cancel())."""
        self._cancel.set()

    def request_pause(self) -> bool:
        """Stop this run at the operator's request — resumably (M6).

        Reuses the run's own PauseSignal, so the node loop and the pipeline
        scheduler both observe ``stopped`` and stop scheduling with no new
        machinery. Deliberately does NOT cancel the leaves already in flight:
        exactly the token-budget reasoning — those calls are made and billed, so
        killing them burns the tokens and throws the answers away. They finish,
        they are charged, and only the next node is refused.

        True only for the FIRST latch (a run already paused by quota or budget
        keeps the reason that really stopped it)."""
        if not self._pause.record(self._current_node, USER_PAUSE):
            return False
        self._record_pause_fault("run paused at the operator's request")
        return True

    def progress_snapshot(self) -> dict[str, Any]:
        """Where this run is, right now (M6) — safe to call from any thread."""
        return self._progress.snapshot()

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        """Publish a live event (WF-30). Called from the run thread AND from the
        pipeline's workers, so it must never raise and must never be called while
        holding a lock of ours — the sink takes one of its own."""
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception:  # a broken sink is not a broken run
            logger.exception("workflow: live event failed")

    def _emit_node(self, node_id: str, state: str) -> None:
        """Publish node lifecycle to live view and the metadata audit."""
        audit_type = "node.started" if state == RUNNING else (
            "node.paused" if state == NULL and self.paused else (
                "node.failed" if state == NULL else "node.completed"
            )
        )
        self._audit_control(
            audit_type, node_id=node_id, role="node.lifecycle", data={"state": state}
        )
        if self._on_event is None:
            return
        snapshot = self._progress.snapshot()
        self._emit(
            NODE,
            {
                "node_id": node_id,
                "state": state,
                "done": snapshot["done"],
                "total": snapshot["total"],
                "running": snapshot["running"],
                "pending": snapshot["pending"],
                "tokens": self._budget.tokens_spent,
            },
        )

    def note_node_items(self, node_id: str, done: int, total: int) -> None:
        """Report a fan-out node's settled-item count (the pipeline's workers)."""
        self._progress.note_items(node_id, done, total)
        with self._result_lock:
            landed = self._result.tokens_in + self._result.tokens_out
        self._emit(ITEMS, {"node_id": node_id, "done": done, "total": total, "tokens": landed})

    @property
    def paused(self) -> bool:
        """True once something cut this run off — it stops, but resumably."""
        return self._pause.hit

    @property
    def stopped(self) -> bool:
        """One check for "schedule nothing more", whatever the reason. Read by
        the node loop AND the pipeline scheduler on its own threads."""
        return self.cancelled or self.paused

    def note_quota_exhausted(self, node_id: str, retry_after: float | None) -> None:
        """A leaf died because the provider is out of quota (WF-1).

        Called from the node thread AND from pipeline on_done workers, so it must
        never block or raise: it latches, records one fault, and cancels the
        leaves still in flight (they would all 429 too — no point burning them).
        """
        if not self._pause.record(node_id, QUOTA_EXHAUSTED, retry_after):
            return
        hint = f"{retry_after:.0f}s" if retry_after else "none"
        self._record_pause_fault(f"quota exhausted at {node_id!r} (retry_after={hint})")
        self._cancel_inflight()

    def note_budget_exhausted(self, node_id: str, detail: str | None = None) -> None:
        """The run has spent its whole token budget (§7.1) — pause, never cap.

        Deliberately does NOT cancel the leaves still in flight, and that is the
        one real difference from a quota pause: those calls are already made and
        already billed, so killing them burns the tokens and throws the answers
        away. They finish, they are charged, and only the next SPAWN is refused.

        ``detail`` replaces the default fault text for a refusal the spend alone
        does not explain (a fan-out too wide for what is left — §7.1's cost gate).
        """
        if not self._pause.record(node_id, TOKEN_BUDGET_EXHAUSTED):
            return
        budget = self._budget
        self._record_pause_fault(
            detail
            or (
                f"token budget exhausted: spent {budget.tokens_spent} "
                f"of {budget.token_budget} tokens"
            )
        )

    def note_checkpoint(self, node_id: str, payload: dict) -> None:
        """A human gate was reached (WF-10) — pause, carrying the question.

        Nothing in flight is cancelled and nothing is thrown away, exactly like
        the token-budget pause: the finished cells stay in the resume cache and
        only the next node is refused. Unlike every other pause, waiting changes
        nothing — an ANSWER is the only remedy, which is why the payload rides
        along instead of a bare reason."""
        if not self._pause.record(node_id, CHECKPOINT, payload=payload):
            return
        question = str(payload.get("prompt") or "")[:MAX_FAULT_CAUSE_CHARS]
        self._record_pause_fault(
            f"checkpoint {node_id!r} is waiting for an answer: {question}"
        )

    def _cancel_inflight(self) -> None:
        """Cooperatively cancel every leaf still running. Non-blocking: it only
        reads with wait=False and interrupts — safe from an on_done worker."""
        with self._result_lock:
            sub_ids = list(self._spawned)
        for sub_id in sub_ids:
            try:
                if self._core.collect(sub_id, wait=False).get("status") == "running":
                    self._core.cancel(sub_id)
            except Exception:  # cleanup must never mask the pause itself
                logger.exception("workflow: failed to cancel in-flight leaf %s", sub_id)

    @property
    def client_pool(self) -> Any | None:
        return self._client_pool

    @property
    def tiers(self) -> Any | None:
        """The operator's model-tier map (WF-5), or None when nothing is mapped."""
        return self._tiers

    @property
    def checkpoint_answers(self) -> dict[str, Any]:
        """What a human already answered for this run's checkpoints (WF-10)."""
        return self._checkpoint_answers

    def load_workflow(self, ref: str) -> dict | None:
        """Resolve a `workflow` node's ref (a template name) to its spec dict."""
        return self._loader(ref) if self._loader is not None else None

    def nested_engine(self, node_id: str | None = None) -> "WorkflowEngine":
        """A child engine for a `workflow` node: shares core/budget/cache/loader
        (so the leaf sandbox + budget can't be escaped), one level deeper."""
        return WorkflowEngine(
            self._core,
            budget=self._budget,
            run_root=self._run_root,
            cache=self._cache,
            loader=self._loader,
            depth=self._depth + 1,
            client_pool=self._client_pool,
            cancel_event=self._cancel,  # one cancel stops the nested run too
            pause=self._pause,  # ...and one pause stops parent + nested alike
            tiers=self._tiers,
            # A checkpoint inside a nested template shares the pause; its answer
            # has to reach it too, or the resume could never satisfy it.
            checkpoint_answers=self._checkpoint_answers,
            on_audit=self._on_audit,
            run_id=self._run_id,
            segment_id=self._segment_id,
            node_scope=self._node_scope + ((node_id,) if node_id else ()),
        )

    def fold_nested(self, nested: "RunResult", ref: str) -> None:
        """Fold a nested run's metrics into THIS run's rollup so nested failures
        stay visible — otherwise an all-failed nested run reads as a clean parent
        node and J could certify a broken composite as a template."""
        self._result.null_count += nested.null_count
        self._result.nodes_total += nested.nodes_total
        self._result.cap_trips += nested.cap_trips
        self._result.engine_faults += nested.engine_faults
        self._result.validation_retries += nested.validation_retries
        self._result.tokens_in += nested.tokens_in
        self._result.tokens_out += nested.tokens_out
        self._result.cache_read_tokens += nested.cache_read_tokens
        self._result.cache_write_tokens += nested.cache_write_tokens
        self._result.reasoning_tokens += nested.reasoning_tokens
        # The nested DAG's nodes, namespaced like its faults: the parent's
        # per-node money still sums to the parent's total, and a reader can tell
        # a sub-workflow's node from one of its own.
        for node_id, cost in nested.node_costs.items():
            self._result.node_costs[f"sub[{ref}]:{node_id}"] = cost
        self._result.forcing_fallbacks += nested.forcing_fallbacks
        self._result.faults.extend(f"sub[{ref}]: {f}" for f in nested.faults)

    @property
    def segment_id(self) -> str:
        return self._segment_id

    def cell_hash(self, *parts: Any) -> str:
        """Content hash of a cache cell, namespaced by the spec identity."""
        return content_hash(self._spec_id[0], self._spec_id[1], *parts)

    def _audit_control(
        self, event_type: str, *, node_id: str, role: str,
        data: dict[str, Any] | None = None, provenance: str = "observed",
    ) -> None:
        if self._on_audit is None:
            return
        try:
            self._on_audit(
                causal_audit_event(
                    event_type,
                    self.causal_context(
                        cell_id=self.cell_hash(node_id, role),
                        role=role,
                        node_id=node_id,
                    ),
                    data=data,
                    provenance=provenance,
                )
            )
        except Exception:
            logger.exception("workflow control audit failed")

    def audit_segment(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Record one execution-stretch boundary without exposing spec/args."""
        if self._on_audit is None:
            return
        try:
            self._on_audit(
                causal_audit_event(
                    event_type,
                    self.causal_context(
                        cell_id=self._segment_id, role="run.segment", node_id="$run"
                    ),
                    data=data,
                )
            )
        except Exception:
            logger.exception("workflow segment audit failed")

    def _audit_cache(
        self, event_type: str, chash: str, node_id: str, *, provenance: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        if self._on_audit is None:
            return
        try:
            self._on_audit(
                causal_audit_event(
                    event_type,
                    self.causal_context(cell_id=chash, role="cache", node_id=node_id),
                    data=data,
                    provenance=provenance,
                )
            )
        except Exception:  # audit can disappear; workflow semantics cannot change
            logger.exception("workflow cache audit failed")

    def cache_lookup(self, chash: str, node_id: str) -> tuple[bool, Any]:
        """(hit, output) — only successful completions are ever cached."""
        if self._cache is None:
            return (False, None)
        try:
            hit, output = self._cache.get(chash)
        except Exception:
            self._audit_cache(
                "cache.unavailable", chash, node_id, provenance="unavailable",
                data={"reason": "lookup_failed"},
            )
            raise
        self._audit_cache(
            "cache.replayed" if hit else "cache.missed",
            chash, node_id, provenance="replayed" if hit else "observed",
        )
        return (hit, output)

    def cache_store(
        self, chash: str, node_id: str, output: Any, cost: Usage | None = None
    ) -> None:
        """Cache only successful completions; a None (dead/invalid) leaves no row
        so a resume re-spawns it. An EMPTY answer is the same kind of
        non-completion (WF-7) — caching it would freeze the silence into every
        later resume of this run.

        ``cost`` is what the cell's winning leaf spent, stored alongside it so a
        resume can account for work it replays instead of re-spawning. Earlier
        failed attempts on the same cell are NOT included (v1): the crash-only
        fallback that sums these rows therefore under-reports a retried cell."""
        if self._cache is None or output is None or is_empty_output(output):
            return
        try:
            self._cache.put_complete(chash, node_id, output, cost)
        except Exception:
            self._audit_cache(
                "cache.unavailable", chash, node_id, provenance="unavailable",
                data={"reason": "store_failed"},
            )
            raise
        self._audit_cache(
            "cache.stored", chash, node_id, provenance="observed"
        )

    def cache_answer(self, chash: str, node_id: str, answer: Any) -> None:
        """Cache an answer a HUMAN gave (WF-10) — whatever it is.

        Deliberately not ``cache_store``: the None/empty gate there speaks about
        a LEAF that came back saying nothing, which is a non-completion worth
        re-spawning. A person who answered ``""`` (or an explicit ``null``, what
        a declared ``default: null`` sends) has answered, and re-opening that
        question on the next resume is exactly what a checkpoint's cache exists
        to prevent. Costs nothing — a checkpoint spawns no leaf."""
        if self._cache is None:
            return
        self._cache.put_complete(chash, node_id, answer)  # no leaf, no cost
        self._audit_cache(
            "cache.stored", chash, node_id, provenance="observed",
            data={"source": "human_checkpoint"},
        )

    @property
    def core(self) -> Any:
        return self._core

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def run_root(self) -> str | None:
        return self._run_root

    def resolve_schema(self, fields: dict) -> dict | None:
        """Resolve an output schema from a fields dict: inline ``schema`` (a dict),
        a string ``schema`` that NAMES a schema (tolerate the schema/schema_ref
        mix-up), or ``schema_ref`` (looked up in the spec's named schemas)."""
        inline = fields.get("schema")
        if isinstance(inline, dict):
            return inline
        if isinstance(inline, str):  # schema: "name" — the common mix-up, coerced
            return self._schemas.get(inline)
        ref = fields.get("schema_ref")
        if isinstance(ref, str):
            return self._schemas.get(ref)
        return None

    def _node_schema(self, node: Node) -> dict | None:
        return self.resolve_schema(node.fields)

    def _gate_tokens(self) -> None:
        """The SOFT token gate (§7.1), read before every spawn — the ONE funnel
        every leaf goes through, so scalar, fan-out, rigor, pipeline and nested
        runs are all covered and none of them can buy its way past it.

        Soft means "checked here and only here": a leaf already running is never
        interrupted for going over. Latch the pause first, then raise, so the
        caller only has to settle the cell it was about to start."""
        if not self._budget.tokens_exhausted:
            return
        self.note_budget_exhausted(self._current_node)
        raise TokenBudgetExhausted(
            f"token budget exhausted: spent {self._budget.tokens_spent} "
            f"of {self._budget.token_budget} tokens"
        )

    def gate_fanout(self, width: int) -> None:
        """The gate every BARRIER fan-out goes through: width/lifetime first,
        then affordability (spec §7.1's ``tokens_remaining // EST_TOKENS_PER_LEAF``).

        The per-spawn gate cannot bound a barrier on its own: nothing is charged
        until the batch is collected, so all ``width`` spawns read the same stale
        ledger and the whole fan-out dispatches however little is left. A
        ``pipeline`` needs no width gate — it dispatches item by item, never more
        than the pool holds before their charges land, so the per-spawn gate
        catches up on its own.

        The spec calls an unaffordable fan-out "rejected + logged" (a cap trip);
        this PAUSES instead, on the same reasoning the per-spawn gate already
        uses: nothing about the SHAPE was too wide, the run ran out of money, and
        a cap trip would teach ``library`` to blame the spec for it.
        """
        self._budget.check_fanout(width)
        affordable = self._budget.affordable_leaves()
        if affordable is None or width <= affordable:
            return
        budget = self._budget
        detail = (
            f"{self._current_node}: fan-out of {width} needs about "
            f"{width * budget.est_leaf_cost} tokens; only {budget.tokens_remaining} "
            f"of the {budget.token_budget} token budget are left "
            f"(~{affordable} leaf/leaves) — token budget exhausted"
        )
        self.note_budget_exhausted(self._current_node, detail)
        raise TokenBudgetExhausted(detail)

    def causal_context(
        self, *, cell_id: str = "", role: str, item_index: int | None = None,
        stage_index: int | None = None, branch_path: tuple[int, ...] = (),
        attempt: int = 0, node_id: str | None = None,
    ) -> CausalContext:
        """Build the immutable workflow identity transported by orchestration."""
        current = node_id or self._current_node
        return CausalContext(
            run_id=self._run_id,
            segment_id=self._segment_id,
            node_path=self._node_scope + (current,),
            cell_id=cell_id or self.cell_hash(current, role),
            role=role,
            item_index=item_index,
            stage_index=stage_index,
            branch_path=tuple(branch_path),
            attempt=attempt,
        )

    def spawn_leaf(
        self, prompt: Any, *, configure: Any | None = None,
        causal_context: CausalContext | None = None,
    ) -> str:
        """Spawn one isolated leaf on the core; charge the budget; return its id.
        ``configure(agent)`` tweaks the child (e.g. forced structured output)."""
        self._gate_tokens()
        self._reserve_lifetime()
        try:
            sub_id = self._core.spawn(
                str(prompt), parent_id=self._run_root, configure=configure,
                causal_context=causal_context
                or self.causal_context(
                    cell_id=self.cell_hash(self._current_node, "leaf", str(prompt)),
                    role="leaf",
                ),
            )
        except BaseException:
            self._budget.refund(1)  # nothing was started: the slot is untouched
            raise
        self._track(sub_id)
        return sub_id

    def spawn_leaf_with_done(
        self, prompt: Any, on_done: Any, *, configure: Any | None = None,
        causal_context: CausalContext | None = None,
    ) -> str:
        """Spawn a leaf with a non-blocking completion hook (pipeline §4.3).
        ``configure(agent)`` tweaks the child, exactly as in ``spawn_leaf``."""
        self._gate_tokens()
        self._reserve_lifetime()
        try:
            sub_id = self._core.spawn(
                str(prompt), parent_id=self._run_root, on_done=on_done,
                configure=configure,
                causal_context=causal_context
                or self.causal_context(
                    cell_id=self.cell_hash(self._current_node, "leaf", str(prompt)),
                    role="leaf",
                ),
            )
        except BaseException:
            self._budget.refund(1)  # nothing was started: the slot is untouched
            raise
        self._track(sub_id)
        return sub_id

    def _reserve_lifetime(self) -> None:
        """Claim this leaf's lifetime slot ATOMICALLY, or refuse the spawn.

        Here and only here, so every node type funnels through it — the same
        reasoning ``_gate_tokens`` uses. Reading the remaining lifetime somewhere
        else and charging after ``core.spawn`` left a window wide enough for a DB
        write, and the pipeline's concurrent on_done workers walked straight into
        it (#14). The reservation IS the charge; only a leaf that never ran gives
        it back."""
        if self._budget.reserve(1):
            return
        raise LifetimeExhausted(
            f"{self._current_node}: leaf lifetime exhausted "
            f"({self._budget.lifetime} leaf spawns already claimed)"
        )

    def _track(self, sub_id: str) -> None:
        """Remember a leaf so a quota pause can cancel it if it's still running —
        and WHICH node spawned it, so its cost can be attributed (Fatia C).

        ``_current_node`` is the right attribution even for the pipeline's
        concurrent workers: they all belong to the one node the run loop is
        blocked on, and a nested workflow runs on an engine of its own."""
        with self._result_lock:
            self._spawned.append(sub_id)
            self._leaf_node[sub_id] = self._current_node

    @property
    def spawned(self) -> tuple[str, ...]:
        """Every leaf this run has started (a snapshot, never the live list).

        Introspection, not control flow: the run loop reads ``_spawned`` under
        the lock it already holds, and the strategies keep their own per-node
        lists. This is the locked seam that lets a reader outside the engine —
        a test, a future inspector — turn ``leaf_cost``/``leaves_cost`` into
        something it can actually call, without reaching into a private."""
        with self._result_lock:
            return tuple(self._spawned)

    def record_fault(self, message: str) -> None:
        """Record a fault from a strategy (fail-closed reporting). Locked: the
        pipeline records from its on_done workers, not just the run thread."""
        with self._result_lock:
            self._result.faults.append(message)
        logger.warning("workflow: %s", message)
        # OUTSIDE the lock (the strategies.py rule): the sink takes a lock of its
        # own, and nesting two of them in one order is how a deadlock lands later.
        self._emit(FAULT, {"text": message})
        self._audit_control(
            "workflow.fault",
            node_id=self._current_node,
            role="workflow.fault",
            data={"cause": {"state": "redacted", "characters": len(message)}},
        )

    def _record_pause_fault(self, message: str) -> None:
        """The fault a PAUSE wrote — recorded like any other, and remembered as
        the pause's own so a later stretch can tell it from a real failure."""
        self.record_fault(message)
        with self._result_lock:
            self._result.pause_fault = message

    def note_leaf_failure(self, node_id: str, result: dict) -> None:
        """Surface WHY a leaf died. The core stores the error string in the sub's
        ``output`` when it ends non-complete; dropping it left the spec author
        with a bare null and no way to tell a crash from an empty answer."""
        if result.get("error_kind") == QUOTA_EXHAUSTED:
            # Not this leaf's own failure — the whole run is out of quota.
            self.note_quota_exhausted(node_id, result.get("retry_after"))
            return
        status = result.get("status") or "unknown"
        cause = str(result.get("output") or "no detail")[:MAX_FAULT_CAUSE_CHARS]
        self.record_fault(f"{node_id}: leaf {status}: {cause}")

    def count_validation_retry(self) -> None:
        """Record a schema-validation retry in the rollup (scalar + pipeline).
        Locked: pipeline retries run on concurrent on_done workers."""
        with self._result_lock:
            self._result.validation_retries += 1

    def account_leaf(self, sub_id: str) -> None:
        """Fold a TERMINAL leaf's cost/rigor into the rollup, once per sub_id
        (deduped — tokens accumulate across a sub's turns, so account at the end).
        Locked: pipeline leaves complete on concurrent on_done workers, so the
        ``+=`` on the shared counters would lose updates unguarded.

        The same cost is charged to the BUDGET, not just the rollup: the rollup
        of a nested run only folds into its parent when the nested run ends, so a
        gate reading it would be blind to a sub-workflow still in flight. The
        budget is shared by reference, so charging there is visible immediately."""
        r = self._core.collect(sub_id, wait=False)  # read; no shared-state mutation
        usage = leaf_usage(r)
        with self._result_lock:
            if sub_id in self._accounted:
                return
            self._accounted.add(sub_id)
            # A leaf the pool DROPPED from its queue never reached a provider, so
            # its lifetime slot bought nothing — give it back (#14). Only this
            # status: a leaf that ran and failed stays charged, or an
            # always-failing shape would spawn without end (``Budget.refund``).
            # Deduped by the same ``_accounted`` set that guards the cost above,
            # so the refund is exactly-once no matter how many paths reach a
            # cancelled leaf (its own on_done, _cancel_inflight, the barrier).
            refund = r.get("status") == CANCELLED
            self._costs[sub_id] = usage
            self._result.tokens_in += usage.input_tokens
            self._result.tokens_out += usage.output_tokens
            self._result.cache_read_tokens += usage.cache_read_tokens
            self._result.cache_write_tokens += usage.cache_write_tokens
            self._result.reasoning_tokens += usage.reasoning_tokens
            # ...and against the NODE that spawned it, with the agent that ran
            # it: a rollup that only totals the run cannot say which node is the
            # expensive one, which is the question an author actually asks.
            node_id = self._leaf_node.get(sub_id, self._current_node)
            previous = self._result.node_costs.get(node_id, NodeCost())
            self._result.node_costs[node_id] = previous.merge(
                usage, r.get("provider"), r.get("model")
            )
            if r.get("forced_fallback"):
                self._result.forcing_fallbacks += 1
        # The BUDGET is deliberately still two axes (Fatia C): ``input_tokens``
        # is now uniformly the uncached prompt, and cache is a REPORT column,
        # never a spending limit.
        self._budget.charge_tokens(usage.input_tokens, usage.output_tokens)
        if refund:
            # OUTSIDE ``_result_lock``: the budget takes a lock of its own, and
            # nesting two of them in one order here is how a deadlock lands later
            # (the strategies.py rule, applied to the budget).
            self._budget.refund(1)

    def spend_split(self) -> Usage:
        """Every meter this STRETCH has spent, read live off the running result
        (the report sibling of ``budget.tokens_spent``)."""
        with self._result_lock:
            return Usage(
                input_tokens=self._result.tokens_in,
                output_tokens=self._result.tokens_out,
                cache_read_tokens=self._result.cache_read_tokens,
                cache_write_tokens=self._result.cache_write_tokens,
                reasoning_tokens=self._result.reasoning_tokens,
            )

    def node_costs(self) -> dict[str, NodeCost]:
        """Per-node cost, read LIVE off the running result (a fresh dict, so a
        reader mid-run cannot corrupt the run's own bookkeeping)."""
        with self._result_lock:
            return dict(self._result.node_costs)

    def leaves_cost(self, sub_ids: list[str]) -> Usage:
        """What a WHOLE node's leaves cost, summed (WF-28).

        A fan-out or rigor node caches ONE cell for many leaves, so the row has
        to carry the price of all of them: charging a replay for the winner
        alone would let a resumed run forget it ever paid for the fan-out."""
        with self._result_lock:
            costs = [self._costs.get(sub_id) for sub_id in sub_ids]
        total = Usage()
        for cost in costs:
            total = combine_usage(total, cost) or total
        return total

    def leaf_cost(self, sub_id: str) -> Usage:
        """What an already-accounted leaf cost, so the cell it produced can be
        cached with its price. An empty ``Usage`` for a leaf that never reached
        accounting (or whose provider reported nothing)."""
        with self._result_lock:
            return self._costs.get(sub_id, Usage())

    def collect_with_schema(
        self, sub_id: str, schema: dict | None, *, timeout: float | None = None
    ) -> Any:
        """Collect + validate a leaf, then account its cost once."""
        output = self._collect_validate(sub_id, schema, timeout=timeout)
        self.account_leaf(sub_id)
        return output

    def _timed_out(self, sub_id: str, result: dict, limit: float) -> bool:
        """A leaf still ``running`` after a blocking collect blew its deadline.

        Walking away leaves a ZOMBIE: the turn keeps holding one of the core's
        few workers with nobody left to read its answer. Cancel it (cooperative
        interrupt) and record the timeout as its own fault — a bare "leaf
        running" told the author nothing about what actually happened.
        """
        if result.get("status") != "running":
            return False
        self._core.cancel(sub_id)
        self.record_fault(f"{self._current_node}: leaf timeout after {limit:.0f}s (cancelled)")
        return True

    def _collect_validate(
        self, sub_id: str, schema: dict | None, *, timeout: float | None = None
    ) -> Any:
        """Collect a leaf; if ``schema`` given, validate + steer-retry on the same
        sub-session. Returns the validated object, raw text (no schema), or None
        (dead leaf / timeout / exhausted retries). The leaf keeps its full toolset."""
        limit = LEAF_TIMEOUT if timeout is None else timeout
        result = self._core.collect(sub_id, wait=True, timeout=limit)
        if self._timed_out(sub_id, result, limit):
            return None
        if result.get("status") != "complete":
            self.note_leaf_failure(self._current_node, result)
            return None
        output = result.get("output")
        if schema is None or output is None:
            return output
        for attempt in range(MAX_VALIDATION_RETRIES + 1):
            ok, parsed, error = parse_and_validate(output, schema)
            if ok:
                return parsed
            if attempt == MAX_VALIDATION_RETRIES:
                logger.warning("workflow: schema not satisfied after retries: %s", error)
                return None
            self._result.validation_retries += 1
            steer_kwargs: dict[str, Any] = {}
            snapshot = self._core.causal_snapshot(sub_id)
            causal = snapshot.get("causal_context") if snapshot else None
            if isinstance(causal, CausalContext):
                steer_kwargs["causal_context"] = replace(
                    causal, attempt=causal.attempt + 1, turn=causal.turn + 1
                )
            self._core.steer(
                sub_id, correction_prompt(schema, error), **steer_kwargs
            )
            retry = self._core.collect(sub_id, wait=True, timeout=limit)
            if self._timed_out(sub_id, retry, limit):
                return None
            if retry.get("status") != "complete":
                self.note_leaf_failure(self._current_node, retry)
                return None
            result = retry
            output = retry.get("output")
            if output is None:
                return None
        return None

    def collect_validated(self, node: Node, sub_id: str) -> Any:
        """Collect a node's leaf under the NODE's own deadline (falling back to
        the global one), validating against the node's own schema."""
        return self.collect_with_schema(
            sub_id, self._node_schema(node), timeout=node_timeout(node.fields, LEAF_TIMEOUT)
        )

    def run(self, spec: WorkflowSpec, args: dict[str, Any] | None = None) -> RunResult:
        result = RunResult()
        self._result = result
        self._accounted = set()
        self._costs = {}
        self._leaf_node = {}
        self._spawned = []
        self._schemas = spec.schemas
        self._spec_id = (spec.meta.get("name", ""), spec.meta.get("version", 0))
        base_context: dict[str, Any] = {"args": args or {}}
        ordered = topological_order(spec)
        result.nodes_total = len(ordered)
        self._progress.reset([node.id for node in ordered])
        for node in ordered:
            if self.stopped:
                # Stop scheduling — and do NOT null what never ran. A cascade of
                # nulls would poison null_rate and read downstream as "the leaves
                # died"; the partial outputs plus the status are the truth.
                # A quota pause already recorded its own fault; only an explicit
                # cancel needs saying here (calling a pause "cancelled" would send
                # the author hunting for a user action that never happened).
                if self.cancelled:
                    self.record_fault(f"run cancelled before node {node.id!r}")
                break
            context = {**base_context, **result.outputs}
            output = self._run_node(node, context, result)
            result.outputs[node.id] = output
            # Settle here, not inside _run_node: this also catches the paths that
            # return None from the engine-fault handler.
            self._progress.settle(node.id, output)
            self._emit_node(node.id, NULL if output is None else COMPLETE)
            if output is None:
                result.null_count += 1
        self._seal(result)
        return result

    def _seal(self, result: RunResult) -> None:
        """Stamp the run's verdict, on the run thread only. An explicit cancel
        outranks a pause: the user stopped this run, so nothing should resume it."""
        if self.cancelled:
            result.status = "cancelled"
            return
        if self.paused:
            result.status = "paused"
            result.pause_reason = self._pause.reason
            result.retry_after = self._pause.retry_after
            result.checkpoint = self._pause.payload
            return
        result.status = derive_status(result)

    def _run_node(self, node: Node, context: dict[str, Any], result: RunResult) -> Any:
        self._current_node = node.id
        self._progress.mark_running(node.id)
        self._emit_node(node.id, RUNNING)
        try:
            # Subscript, not .get(): every validatable node type HAS a strategy
            # (pinned by test_every_node_type_has_a_strategy_and_is_executable,
            # and enforced at author time by SUPPORTED_NODE_TYPES). A KeyError
            # here can only be an engine bug, and lands as one below — recorded
            # and nulled like any other, never a dead run thread.
            return STRATEGIES[node.type](self, node, context)
        except TokenBudgetExhausted:
            # The pause is already latched and its single fault already written.
            # NOT a cap_trip: nothing about this SPEC was too wide — the run ran
            # out of money, and blaming the shape is the wrong lesson entirely.
            return None
        except FanoutRejected as exc:
            self.record_fault(f"{node.id}: {exc}")
            result.cap_trips += 1
            return None
        except Exception as exc:  # engine fault — record, null, keep the run going
            self.record_fault(f"{node.id}: engine fault: {type(exc).__name__}: {exc}")
            result.engine_faults += 1
            logger.exception("workflow: engine fault at node %s", node.id)  # + traceback
            return None
