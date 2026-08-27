"""WorkflowEngine — the tree-walking interpreter over a validated node DAG.

It does NOT execute code: it pattern-matches on ``node.type`` and dispatches to a
strategy (strategies.py). Deterministic control flow is entirely here;
intelligence is only at the leaves. Each node runs under an engine-fault
try/except so one node's internal failure is recorded and nulled — the run
continues — distinct from a leaf returning ``None`` (spec §7.5).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from lohra.providers.errors import QUOTA_EXHAUSTED
from lohra.workflow import refs
from lohra.workflow.budget import (
    TOKEN_BUDGET_EXHAUSTED,
    Budget,
    FanoutRejected,
    TokenBudgetExhausted,
)
from lohra.workflow.cache import content_hash
from lohra.workflow.events import FAULT, ITEMS, NODE
from lohra.workflow.gates import CHECKPOINT
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


@dataclass
class RunResult:
    outputs: dict[str, Any] = field(default_factory=dict)
    faults: list[str] = field(default_factory=list)
    null_count: int = 0
    validation_retries: int = 0
    cap_trips: int = 0  # fan-out rejections (budget)
    engine_faults: int = 0  # node-level engine faults (distinct from leaf null)
    nodes_total: int = 0
    tokens_in: int = 0  # aggregate leaf token cost (§10)
    tokens_out: int = 0
    forcing_fallbacks: int = 0  # forced tool_choice ignored by provider (§5.3)
    status: str = "complete"  # complete | degraded | failed | cancelled | paused
    pause_reason: str | None = None  # quota | token_budget | user_requested | checkpoint
    # The ONE fault the pause itself wrote (WF-26). A pause is not a lesson
    # about the SPEC — it is what stopped this stretch — so whoever judges the
    # run across its stretches needs to tell that fault from the real ones.
    pause_fault: str | None = None
    retry_after: float | None = None  # provider hint for when to resume, if any
    checkpoint: dict | None = None  # what a checkpoint pause is waiting for (WF-10)

    @property
    def null_rate(self) -> float:
        return self.null_count / self.nodes_total if self.nodes_total else 0.0


def _dependencies(node: Node, node_ids: set[str]) -> set[str]:
    """Node ids this node depends on (explicit depends_on + referenced nodes)."""
    deps: set[str] = set()
    explicit = node.fields.get("depends_on") or []
    if isinstance(explicit, list):
        deps |= {d for d in explicit if isinstance(d, str) and d in node_ids}
    deps |= {root for root in _ref_roots(node.fields) if root in node_ids}
    return deps


def _ref_roots(value: Any) -> set[str]:
    roots: set[str] = set()
    if isinstance(value, str):
        roots |= {inner.split(".")[0] for inner in refs.find_refs(value) if refs.is_valid_ref(inner)}
    elif isinstance(value, list):
        for item in value:
            roots |= _ref_roots(item)
    elif isinstance(value, dict):
        for item in value.values():
            roots |= _ref_roots(item)
    return roots


def _derive_status(result: RunResult) -> str:
    """The run's honest verdict (fail-closed, §7.5).

    A null node is never a clean run — reporting "complete" over nulls is what
    let ``library`` certify a broken spec as a reusable template. Everything
    nulled means the run produced nothing at all: "failed".
    """
    if result.nodes_total and result.null_count >= result.nodes_total:
        return "failed"
    if result.faults or result.null_count:
        return "degraded"
    return "complete"


def topological_order(spec: WorkflowSpec) -> list[Node]:
    """Kahn's algorithm. The spec is already validated acyclic, so this resolves."""
    ids = {n.id for n in spec.nodes}
    by_id = {n.id: n for n in spec.nodes}
    pending = {n.id: _dependencies(n, ids) for n in spec.nodes}
    ordered: list[Node] = []
    while pending:
        ready = [nid for nid, deps in pending.items() if deps <= {o.id for o in ordered}]
        if not ready:  # defensive — validation rejects cycles, so this shouldn't happen
            ordered.extend(by_id[nid] for nid in pending)
            break
        for nid in sorted(ready, key=lambda x: [n.id for n in spec.nodes].index(x)):
            ordered.append(by_id[nid])
            del pending[nid]
    return ordered


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
    ) -> None:
        self._core = core
        self._budget = budget
        self._run_root = run_root
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
        self._costs: dict[str, tuple[int, int]] = {}  # sub_id -> (tokens_in, tokens_out)
        self._result_lock = threading.Lock()  # guards off-thread _result writes (pipeline on_done)
        self._current_node: str = "?"  # attribution for faults raised inside a strategy
        # Live per-node progress (M6), read mid-run by workflow_status off this
        # very engine — the same live read the token budget already relies on.
        self._progress = ProgressTracker()
        # ...and the PUSH half (WF-30): (kind, payload) for whoever is watching.
        # Deliberately NOT passed to nested_engine: an event's scope is one DAG,
        # exactly like the tracker's.
        self._on_event = on_event
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
        """One node moved. Carries the run's counters and spend AT THAT MOMENT,
        so a reader never has to poll to turn the line into progress."""
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

    def nested_engine(self) -> "WorkflowEngine":
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
        self._result.forcing_fallbacks += nested.forcing_fallbacks
        self._result.faults.extend(f"sub[{ref}]: {f}" for f in nested.faults)

    def cell_hash(self, *parts: Any) -> str:
        """Content hash of a cache cell, namespaced by the spec identity."""
        return content_hash(self._spec_id[0], self._spec_id[1], *parts)

    def cache_lookup(self, chash: str) -> tuple[bool, Any]:
        """(hit, output) — only successful completions are ever cached."""
        if self._cache is None:
            return (False, None)
        return self._cache.get(chash)

    def cache_store(
        self, chash: str, node_id: str, output: Any, cost: tuple[int, int] = (0, 0)
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
        self._cache.put_complete(chash, node_id, output, cost[0], cost[1])

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
        self._cache.put_complete(chash, node_id, answer)

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

    def spawn_leaf(self, prompt: Any, *, configure: Any | None = None) -> str:
        """Spawn one isolated leaf on the core; charge the budget; return its id.
        ``configure(agent)`` tweaks the child (e.g. forced structured output)."""
        self._gate_tokens()
        sub_id = self._core.spawn(str(prompt), parent_id=self._run_root, configure=configure)
        self._budget.charge(1)
        self._track(sub_id)
        return sub_id

    def spawn_leaf_with_done(
        self, prompt: Any, on_done: Any, *, configure: Any | None = None
    ) -> str:
        """Spawn a leaf with a non-blocking completion hook (pipeline §4.3).
        ``configure(agent)`` tweaks the child, exactly as in ``spawn_leaf``."""
        self._gate_tokens()
        sub_id = self._core.spawn(
            str(prompt), parent_id=self._run_root, on_done=on_done, configure=configure
        )
        self._budget.charge(1)
        self._track(sub_id)
        return sub_id

    def _track(self, sub_id: str) -> None:
        """Remember a leaf so a quota pause can cancel it if it's still running."""
        with self._result_lock:
            self._spawned.append(sub_id)

    def record_fault(self, message: str) -> None:
        """Record a fault from a strategy (fail-closed reporting). Locked: the
        pipeline records from its on_done workers, not just the run thread."""
        with self._result_lock:
            self._result.faults.append(message)
        logger.warning("workflow: %s", message)
        # OUTSIDE the lock (the strategies.py rule): the sink takes a lock of its
        # own, and nesting two of them in one order is how a deadlock lands later.
        self._emit(FAULT, {"text": message})

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
        with self._result_lock:
            if sub_id in self._accounted:
                return
            self._accounted.add(sub_id)
            tokens_in = r.get("tokens_in", 0)
            tokens_out = r.get("tokens_out", 0)
            self._costs[sub_id] = (tokens_in, tokens_out)
            self._result.tokens_in += tokens_in
            self._result.tokens_out += tokens_out
            if r.get("forced_fallback"):
                self._result.forcing_fallbacks += 1
        self._budget.charge_tokens(tokens_in, tokens_out)

    def leaves_cost(self, sub_ids: list[str]) -> tuple[int, int]:
        """What a WHOLE node's leaves cost, summed (WF-28).

        A fan-out or rigor node caches ONE cell for many leaves, so the row has
        to carry the price of all of them: charging a replay for the winner
        alone would let a resumed run forget it ever paid for the fan-out."""
        with self._result_lock:
            costs = [self._costs.get(sub_id, (0, 0)) for sub_id in sub_ids]
        return (sum(cost[0] for cost in costs), sum(cost[1] for cost in costs))

    def leaf_cost(self, sub_id: str) -> tuple[int, int]:
        """What an already-accounted leaf cost, so the cell it produced can be
        cached with its price. (0, 0) for a leaf that never reached accounting."""
        with self._result_lock:
            return self._costs.get(sub_id, (0, 0))

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
            self._core.steer(sub_id, correction_prompt(schema, error))
            retry = self._core.collect(sub_id, wait=True, timeout=limit)
            if self._timed_out(sub_id, retry, limit):
                return None
            if retry.get("status") != "complete":
                self.note_leaf_failure(self._current_node, retry)
                return None
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
        result.status = _derive_status(result)

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
