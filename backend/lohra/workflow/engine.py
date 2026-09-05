"""WorkflowEngine — the tree-walking interpreter over a validated node DAG.

It does NOT execute code: it pattern-matches on ``node.type`` and dispatches to a
strategy (strategies.py). Deterministic control flow is entirely here;
intelligence is only at the leaves. Each node runs under an engine-fault
try/except so one node's internal failure is recorded and nulled — the run
continues — distinct from a leaf returning ``None`` (spec §7.5).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import json
import logging
import threading
from typing import Any
from uuid import uuid4

from lohra.agent.types import Usage, combine_usage
from lohra.orchestration.core import CANCELLED
from lohra.providers.errors import AUTH_FAILED, QUOTA_EXHAUSTED, TIMEOUT
from lohra.providers.timeouts import ENV_VAR as READ_TIMEOUT_ENV_VAR
from lohra.providers.timeouts import effective_read_timeout_seconds
from lohra.workflow.audit import (
    CHANNEL_ROUTE_ENVELOPE,
    causal_audit_event,
    rerouted_event,
)
from lohra.workflow.budget import (
    TOKEN_BUDGET_EXHAUSTED,
    TOKEN_BUDGET_OVERRUN,
    Budget,
    FanoutRejected,
    LifetimeExhausted,
    TokenBudgetExhausted,
)
from lohra.workflow import artifact as artifacts
from lohra.workflow import artifact_paths
from lohra.workflow.cache import (
    MISS_ARTIFACT_CHANGED,
    MISS_IDENTITY_CHANGED,
    MISS_IDENTITY_CHANGED_OR_SIBLING,
    MISS_NEVER_COMPLETED,
    content_hash,
    spec_identity,
)
from lohra.workflow.causality import CausalContext
from lohra.workflow.cell_stamp import (
    CellStamp,
    advisory_message,
    divergence as stamp_divergence,
)
from lohra.workflow.events import FAULT, ITEMS, NODE
from lohra.workflow.accounting import (
    UNKNOWN_AT_SEAL,
    UNSETTLED_AT_SEAL,
    NodeCost,
    RunResult,
    derive_status,
    leaf_settled,
    leaf_unknown,
    leaf_usage,
)
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.graph import topological_order
from lohra.workflow.leaf_retry import is_retryable_failure
from lohra.workflow.namespacing import sub_fault, sub_node_id
from lohra.workflow.nodes import (
    AGGREGATION_ELEMENT,
    Node,
    WorkflowSpec,
    node_timeout,
    resolve_schema,
)
from lohra.workflow.progress import COMPLETE, NULL, RUNNING, SKIPPED, ProgressTracker
from lohra.workflow.quiescence import await_quiescence
from lohra.workflow.required import (
    completeness_fault,
    completeness_gaps,
    nested_required_fault,
    required_fault,
    skip_faults,
)
from lohra.workflow.routes import (
    EMPTY_ENVELOPE,
    COSTLIER,
    EXHAUSTED,
    GATED,
    INELIGIBLE,
    NESTED,
    NO_ENVELOPE,
    REROUTED,
    RUN_STOPPED,
    UNPRICED,
    RouteEnvelope,
    cheaper_or_equal,
    next_route,
    rerouted_fault,
    route_key,
    route_override,
)
from lohra.workflow.route_fault import (
    MAX_FAULT_CAUSE_CHARS,
    ROUTE_FAULT,
    route_change,
    route_fault_payload,
    route_fault_summary,
    should_pause_on_route_fault,
)
from lohra.workflow.steering import SteeringLimits
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

# ``MAX_FAULT_CAUSE_CHARS`` (the bound on a quoted cause, so one huge stack trace
# can't drown the rollup the agent polls) now lives in ``route_fault.py`` and is
# imported above: the pause payload carries the same quoted prose into the
# durable line, and two copies of that ceiling is how a bound drifts.

# What a leaf cut off mid-stream says in its fault (issue #42, épico E3). ONE
# constant for the TWO sites that can report such a leaf — the engine's own
# timeout (``_timed_out``) and any other cancel that lands on a live leaf
# (``note_leaf_failure``) — so the phrase an author greps for cannot drift
# between them. It names the accounting consequence, not just the mechanism:
# the leaf's tokens are a floor, and no estimate replaces the missing bill.
USAGE_UNCERTAIN_CAUSE = "stream aborted on cancel; provider usage unknown"

# How a leaf ends when something STOPPED it rather than when it failed: dropped
# from the queue before it ever ran ("cancelled"), or interrupted mid-turn
# ("interrupted"). Both are what a pause's ``_cancel_inflight`` produces.
_ADMINISTRATIVE_STATUSES = frozenset({CANCELLED, "interrupted"})

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

    def renamespace(self, updates: dict) -> None:
        """Rewrite the latched payload with a NEW dict carrying ``updates``.

        The one thing a latch may change after the fact, and only about IDENTITY:
        a nested run latches with the identity it knows (its own node id), and
        only the parent can say which `workflow` node that was. Never the reason,
        never the fact — and never in place, so a snapshot taken before this
        (the nested ``RunResult``) still describes what the nested run saw."""
        with self._lock:
            if self._hit and isinstance(self.payload, dict):
                self.payload = {**self.payload, **updates}



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
        steering_limits: SteeringLimits | None = None,
        checkpoint_answers: dict[str, Any] | None = None,
        on_event: Any | None = None,
        on_audit: Any | None = None,
        run_id: str | None = None,
        segment_id: str | None = None,
        node_scope: tuple[str, ...] = (),
        artifact_scope: Any | None = None,
        routes: RouteEnvelope | None = None,
        route_fallback_try: Any | None = None,
        nested_ref: str | None = None,
        nested_node: str | None = None,
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
        # Where the harness may stat/hash a declared artifact (#45 E4). None =
        # nothing is verifiable, which is what every caller that never heard of
        # manifests gets: cells store `unverifiable` and replay as they always
        # did. NEVER the leaf sandbox's own root — see ArtifactScope.
        self._artifact_scope = artifact_scope
        self._loader = loader  # resolve a workflow ref -> spec dict (for nesting)
        self._depth = depth
        self._client_pool = client_pool  # cross-provider leaf clients (may be None)
        self._tiers = tiers  # operator model-tier map (WF-5); None = nothing mapped
        # The operator's ROUTE ENVELOPE (#63) and the durable brake that bounds
        # it. Both come from the service, and both are absent by default: an
        # engine nobody configured re-routes nothing and pauses exactly as it did
        # before this existed. Deliberately NOT passed to ``nested_engine`` — a
        # node inside a `workflow` template is not in the spec this run persists,
        # so "the resume sees the new route" would be false for it (the same
        # reason the command channel refuses a nested route answer).
        self._routes = routes if routes is not None else EMPTY_ENVELOPE
        self._route_fallback_try = route_fallback_try
        # Per-node re-route bookkeeping, all read and written on the run thread
        # (only an ``agent`` node can reach them, and its strategy is the loop):
        #   _reroute_pending  node_id -> the route override its next attempt runs
        #   _reroute_tried    node_id -> every route key it has been on
        #   _reroute_faults   node_id -> the faults a WINNING re-route discounts
        self._reroute_pending: dict[str, dict[str, str]] = {}
        self._reroute_tried: dict[str, list[str]] = {}
        self._reroute_faults: dict[str, list[str]] = {}
        # Steering budget for this run's internal corrections (schema-retry
        # fixes): one default per engine, shared with nested engines so a
        # sub-workflow's leaves draw from the same per-leaf ceilings.
        self._steering = (
            steering_limits if steering_limits is not None else SteeringLimits()
        )
        # Where this engine sits when it is a nested `workflow` node: the
        # template it is running (``nested_ref``, what the pause payload names)
        # and the PARENT NODE that called it (``nested_node``, what namespaces a
        # nested checkpoint's answer key — #78). Deliberately different: two
        # nodes may run one template with different args, which is two questions
        # a human answers separately, so the CALL is the identity an answer
        # belongs to. Both None at the top.
        self._nested_ref = nested_ref
        self._nested_node = nested_node
        # Answers a human gave to this run's checkpoints, keyed by the id a
        # nested gate is ASKED under — bare at the top, `sub[ref]:id` one level
        # down (WF-10, #78). The mapping is copied per engine, so the child's
        # copy carrying the parent's spelling was exactly the collision.
        self._checkpoint_answers = dict(checkpoint_answers or {})
        self._schemas: dict[str, Any] = {}
        # Which of THIS spec's nodes aggregate (id → type). The fail-closed
        # guard needs the node TYPE behind a ref root, and the run context
        # carries only outputs (issue #72).
        self._aggregate_types: dict[str, str] = {}
        # ...and what an aggregation RECORDED about its own deaths (id → the
        # indices that really died). A ``pipeline`` item may settle ``None`` on
        # a nullable-root schema, so the value alone cannot say (#72, M1).
        self._aggregate_holes: dict[str, frozenset[int]] = {}
        self._spec_id: tuple[Any, Any] = ("", 0)
        self._result = RunResult()
        self._accounted: set[str] = set()  # leaf sub_ids already folded into the rollup
        # ...and the ones read BEFORE they settled (issue #42). A leaf still
        # inside a provider call has no bill to fold yet, so it is remembered
        # here instead of being written down as zero, and accounted for real by
        # whichever second chance reaches it first (its late ``on_done`` hook,
        # another caller, or the seal). Bounded by the run's leaf lifetime, like
        # ``_costs``/``_leaf_node``/``_timed_out_leaves`` beside it.
        self._pending_account: set[str] = set()
        # The rollup is CLOSED once the run is sealed: a hook that fires one
        # instant later must not add usage the persisted rollup no longer
        # contains, nor contradict the fault already written about that leaf.
        self._sealed = False
        self._costs: dict[str, Usage] = {}  # sub_id -> everything that leaf spent
        self._leaf_node: dict[str, str] = {}  # sub_id -> the node that spawned it
        # Leaves the engine itself cut off at their deadline. Remembered so
        # ``leaf_retryable`` can refuse them even if the provider's own error
        # lands in the same breath as the cancel: the leaf-level timeout is
        # not a failure a re-spawn may buy again (``leaf_retry.py``).
        self._timed_out_leaves: set[str] = set()
        # The NUMBERED fault each attempt of a same-route series left behind,
        # keyed by the leaf that wrote it (Q2, #43). Only faults that carry an
        # "(attempt i/N)" land here — that is exactly the set a later attempt of
        # the SAME cell can retire — so the dict stays as bounded as the series
        # itself and every other fault path is untouched. Popped by
        # ``mark_recovered`` when the series finds a winner; left to count when
        # it does not.
        self._attempt_faults: dict[str, list[str]] = {}
        # (node_id, body) -> how many cells replayed divergently this stretch
        # (#75). The TEXT is written once, at the seal, with the count in it.
        self._replay_divergences: dict[tuple[str, str], int] = {}
        # ...and the same shape for a replay a SIBLING's write explains (#65):
        # path -> how many cells were kept instead of re-spawned, written once at
        # the seal. A wide fan-out on one shared path is one fact about the spec.
        self._shared_path_replays: dict[str, int] = {}
        # Which cells of this run declared which artifact paths (#65). Loaded
        # LAZILY and once — the run's own rows, so a resume sees the cells an
        # earlier stretch stored — behind its own lock, because the first caller
        # may be a pipeline pool worker.
        self._run_paths: Any | None = None
        self._run_paths_lock = threading.Lock()
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
    def run_id(self) -> str:
        """The run this engine speaks for — stable across its nested engines and
        across the stretches of one resume, which is what makes it the key every
        durable ledger (cache, spend, audit, route fallbacks) is written under."""
        return self._run_id

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

    def replay_totals(self) -> tuple[int, int]:
        """``(cells replayed, tokens those cells saved)`` for THIS stretch (#61).

        The live read, like ``budget.snapshot()`` beside it: there is no
        RunResult mid-run, and mid-run is when the agent is deciding whether the
        resume it just accepted is doing what the preview promised."""
        with self._result_lock:
            return (self._result.cells_replayed, self._result.tokens_saved)

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
                # A skipped node never ran, so "completed" would be a lie in the
                # ledger; the audit event types are a closed set (spec §11), so
                # it rides on ``node.failed`` with its real state in the data.
                "node.failed" if state in (NULL, SKIPPED) else "node.completed"
            )
        )
        self._audit_control(
            audit_type, node_id=node_id, role="node.lifecycle", data={"state": state}
        )
        if self._on_event is None:
            return
        snapshot = self._progress.snapshot()
        payload: dict[str, Any] = {
            "node_id": node_id,
            "state": state,
            "done": snapshot["done"],
            "total": snapshot["total"],
            "running": snapshot["running"],
            "pending": snapshot["pending"],
            "tokens": self._budget.tokens_spent,
        }
        # Replayed nodes are the ones whose cost DIDN'T move the counter beside
        # them (#61) — without this, a resume that replayed the whole DAG reads
        # exactly like one that re-paid for it, only faster.
        entry = next((n for n in snapshot["nodes"] if n["id"] == node_id), None)
        if entry is not None and entry.get("replayed"):
            payload["replayed"] = True
        self._emit(NODE, payload)

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

    def note_route_fault(
        self,
        node_id: str,
        result: dict,
        detail: str,
        *,
        node: Any = None,
        attempts_declared: bool = False,
        exhausted: bool = False,
    ) -> bool:
        """A ROUTE died in a way no retry repairs — pause instead of degrading (#43).

        Returns whether this call actually stopped the run, because the caller
        owns the fallback: False means the verdict is still unrecorded and the
        caller must write it as an ordinary fault, exactly as it did before this
        pause existed. Two ways to get False, and neither may swallow a cause:
        the death does not meet the narrow trigger (``route_fault.py``), or
        something else had already latched a pause and keeps the reason that
        really stopped the run.

        Cancels the leaves still in flight, the one real parity with a quota
        pause: the siblings sharing this route are about to be refused the same
        way, and letting them run only spends the budget the pause is trying to
        save. The cost is honest and named — a sibling on a DIFFERENT, healthy
        route dies too — and it is bounded: those leaves come back
        ``cancelled``/``interrupted``, are recorded as pause-CAUSED faults
        (discounted from the "an earlier stretch really failed" verdict), and a
        leaf the pool never dispatched refunds its lifetime slot.

        Called from the node thread AND from pipeline on_done workers, so like
        every other pause reporter it must never block or raise.
        """
        if not should_pause_on_route_fault(
            node, result.get("status"), result.get("error_kind"),
            attempts_declared, exhausted,
        ):
            return False
        payload = route_fault_payload(
            node_id=node_id,
            # What the leaf REALLY ran on, the way ``account_leaf`` reads it: a
            # node that named no model still has a route, and it is the one the
            # reader has to change.
            provider=result.get("provider"),
            model=result.get("model"),
            error_kind=result.get("error_kind"),
            cause=detail,
            # ...and what the provider actually said. On the exhaustion branch
            # the verdict is a tautology and ``error_kind`` is None precisely
            # when the classifier could not name the death — which is when the
            # prose is the only evidence there is.
            last_error=result.get("output"),
        )
        candidate, outcome = self._offer_reroute(node_id, payload, node)
        if candidate is not None:
            # The OPERATOR already answered this pause, in writing, before the
            # run (#63). No latch, no cancel of the leaves in flight, no human:
            # the node's next attempt runs on the next route the operator listed
            # and the run keeps going. True all the same — the verdict is OWNED
            # here, so the caller must not write it a second time as an ordinary
            # fault.
            self._latch_reroute(node_id, payload, candidate, detail)
            return True
        payload["envelope"] = outcome
        if not self._pause.record(node_id, ROUTE_FAULT, payload=payload):
            return False
        with self._result_lock:
            # A pause at a node the envelope already MOVED owns everything that
            # node spent chasing a route (#63 x Q2). The faults held for the
            # re-route's own discount are deaths on a route that is now doubly
            # gone, and the run stopped precisely because of them: leaving them
            # to count would seal ``prior_degraded`` on a run whose only problem
            # is a route, so a human who then answers with a working one could
            # never get the shape certified — the exact harm §7.7 exists to
            # replace. Same grounds, same door, as ``mark_route_fault_caused``.
            self._result.pause_faults.extend(self._reroute_faults.pop(node_id, ()))
        self._record_pause_fault(route_fault_summary(detail, payload))
        self._cancel_inflight()
        return True

    def _offer_reroute(
        self, node_id: str, payload: dict[str, Any], node: Any
    ) -> tuple[str | None, str]:
        """``(candidate route, outcome)`` — what the operator's envelope allows.

        ``candidate is None`` for every refusal, and the outcome word says which
        one, so the pause can tell the reader what was tried (``routes.py`` owns
        the vocabulary; ``route_fault_hint`` turns it into a remedy).

        The checks run in order and NOTHING has a side effect until the last two,
        which are the ones that really commit: building the candidate's client
        (which is also the credential/opt-in gate — an ``openai-codex`` candidate
        still needs ``subscription_active``, and the envelope may not escalate
        into a subscription any more than the agent may) and spending one slot of
        the durable allowance.

        Only an ``agent`` node is ever offered a re-route, and that is a CACHE
        fact before it is a policy one: ``run_agent`` puts the resolved
        provider/model in the cell key unconditionally, so a re-routed node is a
        NEW cell and the dead one stays replayable. A rigor node keys on its
        routing only when it declares any, its strategy owns its own leaf loop,
        and re-routing below a cell whose key did not move would poison the
        cache — so every one of them pauses, exactly as today.

        ``note_route_fault`` promises never to block, and the durable write here
        is the one thing that could. It is reachable only from the run thread:
        the pipeline's on_done workers pass no ``node``, and a pipeline node is
        not an ``agent`` node anyway, so both are refused before the first read.
        """
        if self.stopped:
            # Something already stopped this run — another node's pause, or a
            # cancel. Re-routing would buy a fresh leaf for a run that is not
            # going to schedule it, which is the opposite of what every other
            # stop path does. (Before this existed the second reporter simply
            # lost the latch and wrote an ordinary fault; it still does.)
            return (None, RUN_STOPPED)
        if self._depth:
            # A dead route inside a nested `workflow` template. Re-routing it in
            # memory would work and would then be a LIE on the resume: that node
            # is not in the spec this run persists, so nothing could carry the
            # new route forward. Same refusal, same reason, as the command
            # channel's ``nested_route_refusal``.
            return (None, NESTED)
        if node is None or getattr(node, "type", None) != "agent":
            return (None, INELIGIBLE)
        if getattr(node, "id", None) != node_id:
            # Fail-closed on identity, the guard ``should_pause_on_route_fault``
            # already applies to the same pair: a caller one refactor away from
            # passing the wrong node must not re-route somebody else's.
            return (None, INELIGIBLE)
        if self._routes.empty:
            return (None, NO_ENVELOPE)
        dead = route_key(payload.get("provider"), payload.get("model"))
        if dead is None:
            # A leaf that ran on the run's own default may name only half a
            # route, and half a route matches no entry the operator wrote.
            return (None, NO_ENVELOPE)
        candidate = next_route(self._routes, dead, self._reroute_tried.get(node_id, ()))
        if candidate is None:
            return (None, NO_ENVELOPE)
        verdict = cheaper_or_equal(dead, candidate)
        if verdict is None:
            return (None, UNPRICED)
        if not verdict:
            return (None, COSTLIER)
        if self._client_pool is None:
            # Nothing here can build another provider's client, so nothing can
            # honour the envelope — and a re-route that silently ran on the
            # parent's own client would be the harness ignoring the list.
            return (None, GATED)
        override = route_override(candidate) or {}
        try:
            self._client_pool.get(override.get("provider"))
        except Exception:
            # The credential gate, unchanged and unbypassed: a provider with no
            # key, and ``openai-codex`` without ``subscription_active``, refuse
            # here exactly as they refuse an authored route.
            return (None, GATED)
        if self._route_fallback_try is None:
            # No durable brake wired = no bound on the chain. Refusing is the
            # only fail-closed reading (``routes.py`` doctrine 5).
            return (None, EXHAUSTED)
        try:
            allowed = bool(
                self._route_fallback_try(dead, self._routes.max_fallbacks_per_run)
            )
        except Exception:
            logger.exception("workflow: route fallback ledger unavailable")
            return (None, EXHAUSTED)
        return (candidate, REROUTED) if allowed else (None, EXHAUSTED)

    def _latch_reroute(
        self, node_id: str, payload: dict[str, Any], candidate: str, detail: str
    ) -> None:
        """Commit the re-route: remember it, record it, and say so out loud.

        The record is a fault like every other (fail-closed reporting is
        untouched) and lands in ``rerouted_faults`` as well, so the VERDICT
        discounts it: a run that survived inside the operator's own envelope must
        still be able to seal ``complete``, or the envelope would be a knob that
        guarantees the run it rescued is never certified.

        ``detail`` — the death that triggered this — is recorded here rather than
        by the caller (which returns as if the verdict were already written) and
        is held for the DISCOUNT rather than granted it: it is retired only if
        the new route actually answers. A re-route that dies too leaves the
        lesson where it belongs.
        """
        dead = route_key(payload.get("provider"), payload.get("model")) or ""
        override = route_override(candidate) or {}
        record = rerouted_fault(node_id, dead, candidate)
        self.record_fault(detail)
        self.record_fault(record)
        with self._result_lock:
            self._reroute_pending[node_id] = override
            self._reroute_tried.setdefault(node_id, [dead]).append(candidate)
            self._reroute_faults.setdefault(node_id, []).append(detail)
            self._result.rerouted_faults.append(record)
            self._result.reroutes.append({"node_id": node_id, **override})
        # The typed ledger fact, through #64's shared helpers: ``route_change``
        # derives before/after from the same payload the pause would have
        # carried, and ``audit_reroute`` emits it. Deliberately the SAME pair the
        # command channel uses — two surfaces for one act, and an audit that
        # described them differently would make "was this node re-routed?" a
        # question about which code path ran.
        before, after = route_change(payload, override)
        self.audit_reroute(node_id, before, after, channel=CHANNEL_ROUTE_ENVELOPE)

    def take_reroute(self, node_id: str) -> dict[str, str] | None:
        """The route this node's NEXT attempt must run on, popped (#63).

        Popped, so one offer is spent by one attempt: the strategy loops only
        while the envelope keeps answering, and a node whose re-routed cell dies
        again asks the envelope afresh (and is refused by the durable brake
        unless the operator listed another route for the NEW dead one)."""
        with self._result_lock:
            return self._reroute_pending.pop(node_id, None)

    def mark_reroute_recovered(self, node_id: str) -> None:
        """The re-routed cell ANSWERED: retire what the dead route cost from the
        verdict (#63).

        The mirror of ``mark_recovered`` for a route that moved instead of a
        series that repeated — and it fires only here, on a real output, which is
        what keeps a re-route that failed from laundering its own deaths.
        Retired, never erased: every message stays in ``faults``.

        A no-op for a node nothing re-routed, so the strategy can call it on
        every success without asking."""
        with self._result_lock:
            self._result.recovered_faults.extend(self._reroute_faults.pop(node_id, ()))

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
        along instead of a bare reason.

        ``node_id`` is the node's OWN id and the payload's ``node_id`` is the
        key the answer arrives under — the same one at the top, ``sub[ref]:id``
        one level down (#78). They are split for the reason the nested route
        fault splits them: the latch and the fault text are namespaced on the
        way up by ``fold_nested``, so spelling the prefix here too would print
        it twice; the PAYLOAD cannot wait for the fold, because it is what the
        resume looks the answer up by."""
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
    def steering_limits(self) -> SteeringLimits:
        """This run's steering budget — the default-built one when no
        SteeringLimits was passed in."""
        return self._steering

    @property
    def checkpoint_answers(self) -> dict[str, Any]:
        """What a human already answered for this run's checkpoints (WF-10),
        keyed the way this engine's gates are ASKED — see ``nested_ref``."""
        return self._checkpoint_answers

    @property
    def nested_ref(self) -> str | None:
        """The template this engine is running as a nested `workflow` node, or
        None at the top. It is what a nested pause NAMES (``template``) and how
        ``fold_nested`` namespaces everything it folds up."""
        return self._nested_ref

    @property
    def nested_node(self) -> str | None:
        """The parent's `workflow` node that called this engine, or None at the
        top. It namespaces the key a human answers a checkpoint under (#78) —
        ``lohra.workflow.namespacing.checkpoint_key`` — because two nodes may
        run one template with different args, and those are two questions."""
        return self._nested_node

    def load_workflow(self, ref: str) -> dict | None:
        """Resolve a `workflow` node's ref (a template name) to its spec dict."""
        return self._loader(ref) if self._loader is not None else None

    def nested_engine(
        self, node_id: str | None = None, ref: str | None = None
    ) -> "WorkflowEngine":
        """A child engine for a `workflow` node: shares core/budget/cache/loader
        (so the leaf sandbox + budget can't be escaped), one level deeper.

        ``ref`` (the template) and ``node_id`` (this call) both travel down for
        one reason: the child has to know how its own checkpoints are asked
        BEFORE it asks them (#78) — the key by the CALL, the payload's
        ``template`` by the ref. Everything else nested is namespaced on the way
        UP, by ``fold_nested``; an answer cannot wait for the fold."""
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
            # ...and one steering budget: a nested leaf's corrections land on
            # the same per-leaf ceilings as the parent's own leaves.
            steering_limits=self._steering,
            # A checkpoint inside a nested template shares the pause; its answer
            # has to reach it too, or the resume could never satisfy it.
            checkpoint_answers=self._checkpoint_answers,
            # ...under the namespace this child asks them in: the CALL (#78).
            nested_ref=ref,
            nested_node=node_id,
            on_audit=self._on_audit,
            run_id=self._run_id,
            segment_id=self._segment_id,
            node_scope=self._node_scope + ((node_id,) if node_id else ()),
            # A nested template's leaves write into the same run tree as the
            # parent's, so they are verifiable under the same scope. Sharing it
            # (like core/budget/cache) also means a nested engine cannot widen
            # what the harness may read.
            artifact_scope=self._artifact_scope,
            # ``routes``/``route_fallback_try`` are deliberately NOT passed: a
            # node inside a template is not in the spec this run persists, so a
            # re-route down here could never be carried forward by a resume
            # (``_offer_reroute`` refuses on ``depth`` too — belt and braces).
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
        # The nested run's unknown bills are the parent's unknown bills: a
        # sub-workflow whose leaves were cut mid-stream would otherwise fold in
        # as exact tokens, and the parent's rollup would claim a precision it
        # does not have (issue #42).
        self._result.usage_uncertain_leaves += nested.usage_uncertain_leaves
        # The nested DAG's nodes, namespaced like its faults: the parent's
        # per-node money still sums to the parent's total, and a reader can tell
        # a sub-workflow's node from one of its own.
        for node_id, cost in nested.node_costs.items():
            self._result.node_costs[sub_node_id(ref, node_id)] = cost
        self._result.forcing_fallbacks += nested.forcing_fallbacks
        # The nested engine keeps a progress tracker of its own (the parent
        # reports the `workflow` node as ONE node), but the metric folds up: a
        # parent reporting 0 would say a fully cached sub-workflow cost it a
        # whole run (#61).
        self._result.cells_replayed += nested.cells_replayed
        self._result.tokens_saved += nested.tokens_saved
        if nested.cells_replayed:
            # ...and the parent's OWN progress line for this node, so the `⟲`
            # and the ``replayed`` flag reach a reader who only ever sees the
            # parent's DAG. ``or None`` because a cell is only priced with a
            # non-zero meter: 0 here means nothing was priced, which is not the
            # same claim as "it was free".
            self._progress.mark_replayed(
                self._current_node,
                nested.tokens_saved or None,
                cells=nested.cells_replayed,
            )
        self._result.faults.extend(sub_fault(ref, f) for f in nested.faults)
        # Namespaced the same way, or the parent's verdict could not match them
        # back — a nested run shares the parent's pause object, so its leaves are
        # stopped by the very same pause.
        self._result.pause_faults.extend(
            sub_fault(ref, f) for f in nested.pause_faults
        )
        # ...and the nested series that RECOVERED, namespaced identically (Q2):
        # ``fold_nested`` prefixes the nested faults, so the parent's discount
        # can only match them back if their recovered twins carry the same
        # prefix — otherwise a nested leaf that died and recovered would seal
        # the PARENT degraded, which is the exact bug this slice removes.
        self._result.recovered_faults.extend(
            sub_fault(ref, f) for f in nested.recovered_faults
        )
        # ...and the nested ADVISORIES, namespaced for the same reason (#45): the
        # parent discounts by matching the text it folded up, so an unprefixed
        # advisory would seal the PARENT degraded over a nested leaf that merely
        # miscounted a hash.
        self._result.advisory_faults.extend(
            sub_fault(ref, f) for f in nested.advisory_faults
        )
        # ...and how many of those advisories were DIVERGENT REPLAYS (#75). The
        # count travels beside the list because the certified template derives
        # ``artifact_divergences`` by subtracting it: a nested template whose
        # cells replayed under a new policy would otherwise publish the parent as
        # having miscounted N artifact claims it never touched.
        self._result.replay_divergences += nested.replay_divergences
        # ...and the OTHER advisory source, counted the same way and for the same
        # reason: the parent's certified template stamps the two apart.
        self._result.artifact_advisories += nested.artifact_advisories
        self._result.leaf_respawns += nested.leaf_respawns
        # ...and the fault the nested PAUSE itself wrote. It travels in a field of
        # its own down there (``pause_fault``, singular) and there is no singular
        # slot up here to receive it, so it folds in beside the faults the pause
        # caused — which is what ``carried_faults`` discounts. Without this line a
        # nested pause escapes as an ordinary fault and seals the PARENT
        # ``prior_degraded``: the run that paused resumably would never be
        # certifiable again, however cleanly it came back.
        if nested.pause_fault is not None:
            self._result.pause_faults.append(sub_fault(ref, nested.pause_fault))
        if nested.route_fault is not None:
            # A dead route one level down. The node that died is namespaced like
            # every other nested identity, and the TEMPLATE is named outright:
            # the remedy is not in the spec a resume would send, and a payload
            # that says "node `i`" sends the author looking for it there.
            self._pause.renamespace(
                {
                    "node_id": sub_node_id(ref, nested.route_fault.get("node_id")),
                    "template": ref,
                }
            )
        # A `required` node that failed one level down must not be silently
        # unenforceable: the parent's node loop reads this and aborts at the
        # `workflow` node, and the identity stays namespaced so the rollup can
        # match it back to the sub-workflow that raised it (issue #15).
        if nested.required_failure is not None and self._result.required_failure is None:
            self._result.required_failure = sub_node_id(ref, nested.required_failure)

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

    def audit_reroute(
        self, node_id: str, before: Any, after: Any, *, channel: str
    ) -> None:
        """Record that ONE node's route was MOVED, and through what (#64).

        Emitted by whoever APPLIES the change — the service for the command
        channel of #43, the envelope for #63 — and always inside the stretch
        that will run on the new route, so a reader sees the move and then the
        leaves it produced. Never raises: an audit event is evidence about a
        run, never a condition of it.
        """
        if self._on_audit is None:
            return
        try:
            self._on_audit(
                rerouted_event(
                    self.causal_context(
                        cell_id=self._segment_id, role="run.reroute", node_id=node_id
                    ),
                    node_id=node_id,
                    before=before,
                    after=after,
                    channel=channel,
                )
            )
        except Exception:
            logger.exception("workflow reroute audit failed")

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

    def _miss_reason(self, node_id: str, shared_node_id: bool) -> str | None:
        """WHY this lookup missed — the one moment the answer exists (#44).

        Derived here because it cannot be recovered later: the audit's cell_id is
        the STRUCTURAL identity, so a post-hoc reader sees the same id for a miss
        and a replay. Telemetry only: a store that cannot answer leaves the field
        out rather than changing a single thing about the run."""
        if self._on_audit is None or self._cache is None:
            return None
        try:
            seen = self._cache.hashes_for_node(node_id, include_fanout=shared_node_id)
        except Exception:
            logger.exception("workflow: cache miss reason unavailable for %s", node_id)
            return None
        if not seen:
            return MISS_NEVER_COMPLETED
        # A shared node id (pipeline cells, a nested template's nodes) cannot
        # support the stronger claim: the row may be a sibling cell (D6).
        if shared_node_id or self._depth > 0:
            return MISS_IDENTITY_CHANGED_OR_SIBLING
        return MISS_IDENTITY_CHANGED

    def _cell_saving(self, chash: str) -> int | None:
        """What replaying this cell saved, when its price was recorded (M5).

        None — never 0 — for an unpriced cell: a zero would read as "this replay
        was free", which is the opposite of "nobody wrote the price". Read
        unconditionally now (#61): the audit is no longer the only reader, since
        the progress and the rollup report the saving too."""
        if self._cache is None:
            return None
        try:
            return self._cache.cell_tokens(chash)
        except Exception:
            logger.exception("workflow: cache replay saving unavailable for %s", chash)
            return None

    def _note_replay(self, node_id: str, tokens_saved: int | None) -> None:
        """Record that this node served a cell out of the cache (#61).

        Per CELL: a pipeline lands here once per (item, stage) that hits, from
        its concurrent done-path workers — hence the result lock, the same one
        every off-thread write to the rollup takes. A hit after the books close
        is not counted, exactly like a late leaf's usage: the rollup that says
        so has already been persisted."""
        self._progress.mark_replayed(node_id, tokens_saved)
        with self._result_lock:
            if self._sealed:
                return
            self._result.cells_replayed += 1
            self._result.tokens_saved += tokens_saved or 0

    def _paths_of_run(self) -> Any | None:
        """The run's ``path -> cells`` index (#65), loaded once per stretch.

        None without a cache: nothing declared anything, so no path can be
        shared and every recheck answers exactly as it did before."""
        if self._cache is None:
            return None
        with self._run_paths_lock:
            if self._run_paths is None:
                self._run_paths = artifact_paths.RunPaths.load(self._cache)
            return self._run_paths

    def _artifact_recheck(self, artifact: dict[str, Any] | None) -> artifacts.Recheck | None:
        """Does this cell's stored manifest still describe the filesystem (#45 E4)?

        None when there is nothing to ask: no manifest, or one the harness could
        not measure in the first place (a path outside its scope — the v4 case of
        a leaf writing into the user's project through an operator-enabled
        shell). Those replay as they always did; "we may not look" is not
        evidence of change, and inventing a miss there would re-pay for every
        such cell on every resume.

        A recheck that RAISES also replays: the failure is logged and the cell
        stays unverified. Fail-open is the honest side here — an exception means
        the harness does not know, and spending a leaf on not knowing is the same
        lie in the other direction, with a bill attached."""
        if artifact is None or artifact.get("verification") != artifacts.VERIFIED:
            return None
        try:
            verdict = artifacts.recheck(
                artifact.get("entries"), self._artifact_scope, self._paths_of_run()
            )
        except Exception:
            logger.exception("workflow: artifact recheck failed; replaying unverified")
            return None
        for path in verdict.shared:
            # Counted per CELL, written per PATH at the seal — the same shape as
            # the stamp advisories, for the same reason (#75): the count is not
            # known until the last cell of a fan-out has replayed.
            with self._result_lock:
                self._shared_path_replays[path] = (
                    self._shared_path_replays.get(path, 0) + 1
                )
        return verdict

    def cache_lookup(
        self, chash: str, node_id: str, *, shared_node_id: bool = False
    ) -> tuple[bool, Any]:
        """(hit, output) — only successful completions are ever cached.

        ``shared_node_id`` says this node stores MANY cells under one node id (a
        pipeline's per-(item, stage) cells), so a miss cannot claim the identity
        changed just because a row exists.

        A hit whose ARTIFACT moved on is refused here and becomes a miss with
        ``reason: artifact_changed`` (#45 E4): the key is byte-identical and the
        row is right there, but replaying it would re-assert a description of a
        file that no longer holds. The node re-spawns — which is the point: this
        is the one miss the cache key can never see by itself."""
        if self._cache is None:
            return (False, None)
        try:
            hit, output, artifact, stamp_row = self._cache.get_with_stamp(chash)
        except Exception:
            self._audit_cache(
                "cache.unavailable", chash, node_id, provenance="unavailable",
                data={"reason": "lookup_failed"},
            )
            raise
        if hit:
            verdict = self._artifact_recheck(artifact)
            if verdict is not None and verdict.stale:
                # Deliberately NOT ``_miss_reason``: it would answer
                # ``identity_changed``, which is false — nothing about the
                # identity moved. Status only, never the path (audit stays
                # metadata-only, §11.2).
                self._audit_cache(
                    "cache.missed", chash, node_id, provenance="observed",
                    data={"reason": MISS_ARTIFACT_CHANGED, "artifact": verdict.status},
                )
                return (False, None)
            saved = self._cell_saving(chash)
            self._note_replay(node_id, saved)
            data = None if saved is None else {"tokens_saved": saved}
            if artifact is not None:
                status = verdict.status if verdict is not None else artifact.get("verification")
                data = {**(data or {}), "artifact": status}
            moved = self._stamp_divergence(node_id, stamp_row)
            if moved is not None:
                data = {**(data or {}), "reason": moved}
            self._audit_cache(
                "cache.replayed", chash, node_id, provenance="replayed", data=data,
            )
        else:
            reason = self._miss_reason(node_id, shared_node_id)
            self._audit_cache(
                "cache.missed", chash, node_id, provenance="observed",
                data={"reason": reason} if reason is not None else None,
            )
        return (hit, output)

    def _stamp_divergence(self, node_id: str, stamp_row: Any) -> str | None:
        """The ``reason`` this replay deserves, or None (#75).

        MARK, never invalidate (the owner's decision): the cell is work the run
        already paid for and it replays either way — what a divergence buys is
        that the fact stops being invisible. The fault is ADVISORY on the #45
        grounds: the node CONCLUDED, so nothing about the spec's SHAPE is what
        went wrong, and a run whose only blemish is a narrower policy must still
        be certifiable.

        Counted per CELL here, WRITTEN per (node, reason) at the seal: the ledger
        keeps every cell's own ``reason`` on its own ``cache.replayed`` event,
        and the fault list gets one line per node saying how many. A 500-item
        pipeline resumed under a narrower policy is one fact, not 500.

        None whenever either side is UNKNOWN: a cell stored before the stamp
        existed, or a cache that holds no policy to compare against. Never
        invent a divergence where there is no record."""
        if self._cache is None or self._cache.stamp is None:
            return None
        moved = stamp_divergence(CellStamp.stored(stamp_row), self._cache.stamp)
        if moved is None:
            return None
        reason, body = moved
        # The node id is carried in the KEY (and later in the text) for the same
        # reason ``cache_store`` puts it there: a pipeline replays from a pool
        # worker, where ``_current_node`` is whatever the run thread is on.
        with self._result_lock:
            self._replay_divergences[(node_id, body)] = (
                self._replay_divergences.get((node_id, body), 0) + 1
            )
            self._result.replay_divergences += 1
        return reason

    def _flush_replay_advisories(self) -> None:
        """Write the stretch's replay advisories — one per (node, reason).

        At the SEAL because the count belongs in the text and the count is not
        known until the last cell has replayed. Before the verdict, so the fault
        list ``derive_status``/``unrecovered`` read is complete; drained, so a
        second seal cannot write them twice."""
        with self._result_lock:
            pending = list(self._replay_divergences.items())
            self._replay_divergences = {}
        for (node_id, body), cells in pending:
            self.record_advisory_fault(advisory_message(node_id, body, cells))
        self._flush_shared_path_advisories()

    def _flush_shared_path_advisories(self) -> None:
        """...and the replays a SIBLING's write explains — one per PATH (#65).

        Drained at the same seal and for the same reason as the stamp
        advisories: the cell count belongs in the text, and it is not known
        until the last cell of the fan-out has replayed."""
        with self._result_lock:
            pending = list(self._shared_path_replays.items())
            self._shared_path_replays = {}
        for message in artifact_paths.replay_messages(self._run_paths, pending):
            self.record_advisory_fault(message)

    def _measure_artifacts(
        self, node_id: str, output: Any, schema: Any
    ) -> tuple[tuple[str, str] | None, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """``((verification, manifest_json), divergences, notes, collisions)``
        for a manifest cell.

        Measured BEFORE the cell is written, so the verdict rides in the cell's
        own guarded transaction instead of a second write a fence refusal could
        drop. The leaf's ``sha256``/``bytes`` are a CLAIM: a divergence from what
        the harness measured is an ADVISORY fault (#45), never a dead node and
        never a degraded run — the file was written, and the cell stores what the
        harness measured. ``notes`` are ordinary faults: they report a
        verification the harness could not finish, which is a hole rather than a
        corrected claim.

        ``collisions`` are the paths ANOTHER cell of this run already declared
        (#65). Decided here, against the run's index — two declarations compared
        to each other, the disk never consulted — so the warning does not depend
        on which writer won a race, which is what made #62's detection a
        lottery."""
        if not artifacts.is_manifest_schema(schema):
            return None, (), (), ()
        try:
            record = artifacts.verify_output(output, self._artifact_scope)
        except Exception:
            logger.exception("workflow: artifact measurement failed for %s", node_id)
            return None, (), (), ()
        if record is None:
            return None, (), (), ()
        return (
            (record.verification, json.dumps(record.as_entry_list(), ensure_ascii=False)),
            record.divergences,
            record.notes,
            artifact_paths.collision_messages(
                self._paths_of_run(), node_id, record.entries
            ),
        )

    def cache_store(
        self, chash: str, node_id: str, output: Any, cost: Usage | None = None,
        *, schema: Any | None = None, leaf_count: int = 1,
    ) -> None:
        """Cache only successful completions; a None (dead/invalid) leaves no row
        so a resume re-spawns it. An EMPTY answer is the same kind of
        non-completion (WF-7) — caching it would freeze the silence into every
        later resume of this run.

        ``cost`` is what the cell's winning leaf spent, stored alongside it so a
        resume can account for work it replays instead of re-spawning. Earlier
        failed attempts on the same cell are NOT included (v1): the crash-only
        fallback that sums these rows therefore under-reports a retried cell.

        ``leaf_count`` is how many leaves ``cost`` covers — 1 unless the caller
        also passed ``leaves_cost`` over a LIST, which is the whole set of nodes
        that cache one cell for many leaves (#71). The two travel together on
        purpose: a price summed over N leaves and stored as one is exactly what
        made a resume's measured average N times too high.

        ``schema`` is the node's RESOLVED output schema, passed by the strategies
        that have one. When it is an artifact manifest (#45 E4) the harness
        measures the declared paths and stores the measurement in sidecar
        columns — never in ``output_json``, which is what a downstream
        ``${ref}`` reads."""
        if self._cache is None or output is None or is_empty_output(output):
            return
        artifact, divergences, notes, collisions = self._measure_artifacts(
            node_id, output, schema
        )
        try:
            self._cache.put_complete(
                chash, node_id, output, cost, leaf_count=leaf_count, artifact=artifact
            )
        except Exception:
            self._audit_cache(
                "cache.unavailable", chash, node_id, provenance="unavailable",
                data={"reason": "store_failed"},
            )
            raise
        self._audit_cache(
            "cache.stored", chash, node_id, provenance="observed",
            data={"artifact": artifact[0]} if artifact is not None else None,
        )
        for message in divergences:
            # The node id goes in the TEXT: a pipeline records this from an
            # on_done worker, where ``_current_node`` is whatever the run thread
            # happens to be on.
            self.record_artifact_advisory(f"{node_id}: {message}")
        for message in collisions:
            # NOT ``record_artifact_advisory``: that counter (#45) is "manifest
            # CLAIMS the harness corrected", and nobody's claim was corrected
            # here — the harness compared two declarations and found them naming
            # one file. Same discount by ``derive_status``, honest counter.
            self.record_advisory_fault(message)
        for message in notes:
            self.record_fault(f"{node_id}: {message}")

    def cache_answer(self, chash: str, node_id: str, answer: Any) -> None:
        """Cache an answer a HUMAN gave (WF-10) — whatever it is.

        Deliberately not ``cache_store``: the None/empty gate there speaks about
        a LEAF that came back saying nothing, which is a non-completion worth
        re-spawning. A person who answered ``""`` (or an explicit ``null``, what
        a declared ``default: null`` sends) has answered, and re-opening that
        question on the next resume is exactly what a checkpoint's cache exists
        to prevent. Costs nothing — a checkpoint spawns no leaf.

        And it is stored UNSTAMPED (#75): no sandbox policy ever governed a
        person typing an answer, so a later policy change has nothing to say
        about this cell. Stamping it would manufacture an advisory about the one
        kind of cell an operator knob could never have changed."""
        if self._cache is None:
            return
        # no leaf, no cost, and no policy over it
        self._cache.put_complete(chash, node_id, answer, stamped=False)
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

    @property
    def aggregate_types(self) -> dict[str, str]:
        """This run's aggregation nodes, id → type (a COPY — the guard only reads
        it, and nothing outside may re-shape the run's view of its own graph)."""
        return dict(self._aggregate_types)

    @property
    def aggregate_holes(self) -> dict[str, frozenset[int]]:
        """Which elements of each aggregation actually died (a COPY, same reason).
        Written once per node, by the strategy that watched them die."""
        return dict(self._aggregate_holes)

    def note_aggregate_holes(self, node_id: str, indices: frozenset[int]) -> None:
        """Record which top-level elements of ``node_id`` died — a NEW dict, never
        a mutation, so a snapshot already handed to a guard cannot shift under it."""
        self._aggregate_holes = {**self._aggregate_holes, node_id: frozenset(indices)}

    def resolve_schema(self, fields: dict) -> dict | None:
        """This spec's named schemas + a node's fields → the schema that goes into
        the cell hash. The rule itself is shared (nodes.py): a recomputation of a
        key has to resolve it identically or it would miss every schema'd cell."""
        return resolve_schema(self._schemas, fields)

    def _node_schema(self, node: Node) -> dict | None:
        return self.resolve_schema(node.fields)

    def _gate_tokens(self) -> None:
        """The SOFT token gate (§7.1), read before every spawn — the ONE funnel
        every leaf goes through, so scalar, fan-out, rigor, pipeline and nested
        runs are all covered and none of them can buy its way past it.

        Soft means "checked here and only here": a leaf already running is never
        interrupted for going over. Latch the pause first, then raise, so the
        caller only has to settle the cell it was about to start.

        TWO questions, in order (issue #71). "Is the budget spent?" is the old
        one and keeps its own words. "Can what is LEFT pay for one more leaf?"
        is the stop line: asking only the first let a run with 500 tokens left
        spawn a leaf its own measurements price at 2500, so the ceiling was
        never held, only crossed. Asked ONLY once this run has measured a leaf
        of its own — before that the estimate is a static constant that knows
        nothing about these leaves, and refusing on it would stop a fresh run
        under a small ceiling before it ever bought its first measurement.
        """
        budget = self._budget
        if budget.tokens_exhausted:
            self.note_budget_exhausted(self._current_node)
            raise TokenBudgetExhausted(
                f"token budget exhausted: spent {budget.tokens_spent} "
                f"of {budget.token_budget} tokens"
            )
        # ``affordable_leaves`` is the same question ``gate_fanout`` asks, for a
        # width of one — and it is None for a run with no ceiling, which is the
        # only reading that keeps ``tokens_remaining``'s "0 means unlimited"
        # from being mistaken for "nothing left".
        affordable = budget.affordable_leaves()
        if not budget.has_measurement or affordable is None or affordable >= 1:
            return
        estimate = budget.est_leaf_cost
        remaining = budget.tokens_remaining
        detail = (
            f"{self._current_node}: next leaf estimated at {estimate} tokens "
            f"(measured average), only {remaining} left of "
            f"{budget.token_budget} — token budget exhausted"
        )
        self.note_budget_exhausted(self._current_node, detail)
        raise TokenBudgetExhausted(detail)

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
        self._announce_fault(message)

    def _announce_fault(self, message: str) -> None:
        """Log it, emit it, audit it — ALWAYS outside ``_result_lock`` (the
        strategies.py rule): the sink takes a lock of its own, and nesting two of
        them in one order is how a deadlock lands later."""
        logger.warning("workflow: %s", message)
        self._emit(FAULT, {"text": message})
        self._audit_control(
            "workflow.fault",
            node_id=self._current_node,
            role="workflow.fault",
            data={"cause": {"state": "redacted", "characters": len(message)}},
        )

    def record_advisory_fault(self, message: str) -> None:
        """A fault that ADVISES about a node that concluded (#45) — recorded like
        any other and remembered as advisory, so the verdict discounts it here
        and across stretches. Same shape as the pause siblings below: one door
        into ``faults``, one extra list, no second reporting path."""
        self._record_advisory(message)

    def record_artifact_advisory(self, message: str) -> None:
        """...and the advisory whose source is an artifact CLAIM (#45).

        Counted at the door it comes through, never inferred later: the advisory
        list has more than one source now (#75), and a certified template that
        told them apart by their PROSE would be doing exactly what the verdict
        rules forbid."""
        self._record_advisory(message, artifact=True)

    def _record_advisory(self, message: str, *, artifact: bool = False) -> None:
        """The three writes an advisory makes, under ONE acquisition: a reader
        must never find the fault list and the advisory list disagreeing about a
        message that is already in one of them."""
        with self._result_lock:
            self._result.faults.append(message)
            self._result.advisory_faults.append(message)
            if artifact:
                self._result.artifact_advisories += 1
        self._announce_fault(message)

    def _record_pause_fault(self, message: str) -> None:
        """The fault a PAUSE wrote — recorded like any other, and remembered as
        the pause's own so a later stretch can tell it from a real failure."""
        self.record_fault(message)
        with self._result_lock:
            self._result.pause_fault = message

    def _record_pause_caused_fault(self, message: str) -> None:
        """A fault the pause CAUSED (a leaf it stopped) — recorded like any other
        and remembered as administrative, so the verdict across stretches can
        discount it alongside the pause's own."""
        self.record_fault(message)
        with self._result_lock:
            self._result.pause_faults.append(message)

    def note_leaf_failure(
        self,
        node_id: str,
        result: dict,
        *,
        attempt: tuple[int, int] | None = None,
        sub_id: str | None = None,
        owner_node_id: str | None = None,
        node: Any = None,
    ) -> None:
        """Surface WHY a leaf died. The core stores the error string in the sub's
        ``output`` when it ends non-complete; dropping it left the spec author
        with a bare null and no way to tell a crash from an empty answer.

        ``attempt`` is ``(i, n)`` when the caller may re-spawn this cell on the
        same route (``leaf_retry.py``): three identical faults in a row read as
        three broken nodes unless each says which attempt it was. It is stamped
        ONLY on a failure that is actually part of such a series — a timeout or
        an administrative stop buys no re-spawn, so numbering it "1/2" would
        promise a second attempt that is never coming. Absent, and for ``n == 1``
        where there is no series, the message is exactly what it always was.

        ``owner_node_id`` is the node an AUTHOR could edit, when that differs
        from the id this fault is written under: a pipeline names each cell
        ``pl#3#0`` so the fault says which item and stage died, but a pause
        payload built from that id would point at a node no spec contains. The
        fault text is unchanged; only the identity the pause reports moves.

        ``node`` is the NODE itself, threaded down the one collect hop that has
        it (``collect_validated``): the operator's route envelope (#63) may only
        move an ``agent``, and judging that off an id would be judging it off the
        wrong thing. Absent everywhere else — a pipeline's on_done worker has no
        node to give — which is exactly the fail-closed reading.

        ``sub_id`` is the leaf that died, and it is passed only by the collect
        path a re-spawn can follow. A numbered fault is remembered under it so
        that ``mark_recovered`` can retire this exact message BY IDENTITY if a
        later attempt of the same cell succeeds (Q2, #43) — never by re-reading
        the text, which is the provider's prose and not a fact."""
        if result.get("error_kind") == QUOTA_EXHAUSTED:
            # Not this leaf's own failure — the whole run is out of quota.
            self.note_quota_exhausted(node_id, result.get("retry_after"))
            return
        status = result.get("status") or "unknown"
        if result.get("error_kind") == TIMEOUT:
            # A raw ``str(exc)`` here ("Request timed out.") tells the spec
            # author nothing actionable: there are TWO timeouts in play (the
            # HTTP read timeout and the leaf's own ``timeout:``/LEAF_TIMEOUT),
            # and neither is obvious from an opaque SDK message. Name both.
            read_seconds = effective_read_timeout_seconds()
            cause = (
                f"provider read timeout after ~{read_seconds:.0f}s (silence, not "
                f"size — leaves stream); {READ_TIMEOUT_ENV_VAR} raises the HTTP "
                f"limit; the node `timeout:` field controls the leaf-level limit "
                f"(default {LEAF_TIMEOUT:.0f}s)"
            )
        elif result.get("error_kind") == AUTH_FAILED:
            # Quoting the SDK's "Error code: 401" alone sends the author looking
            # for a prompt to fix. Name the route's credential and say the remedy
            # is not theirs: no retry, no wait, no spec edit repairs a key.
            detail = str(result.get("output") or "no detail")[:MAX_FAULT_CAUSE_CHARS]
            cause = (
                f"provider refused this route's credential or its permissions "
                f"({detail}); no retry or wait repairs this — the operator owns "
                f"the key, the scope and whether this provider is enabled"
            )
        elif result.get("usage_uncertain"):
            # A leaf whose stream a cancel closed mid-flight (issue #42, épico
            # E3). Its ``output`` is empty by construction — nothing was
            # assembled — so the generic branch below would report "no detail"
            # for the one failure whose cause we know exactly, and would say
            # nothing about the bill nobody can read.
            cause = USAGE_UNCERTAIN_CAUSE
        else:
            cause = str(result.get("output") or "no detail")[:MAX_FAULT_CAUSE_CHARS]
        message = f"{node_id}: leaf {status}: {cause}"
        if (
            isinstance(attempt, tuple)  # never trust a caller's ``attempt`` shape:
            # this used to be reachable with an int, and the TypeError buried the
            # very cause this method exists to report.
            and attempt[1] > 1
            and is_retryable_failure(status, result.get("error_kind"))
        ):
            message = f"{message} (attempt {attempt[0]}/{attempt[1]})"
            if sub_id is not None:
                with self._result_lock:
                    self._attempt_faults.setdefault(sub_id, []).append(message)
        if status in _ADMINISTRATIVE_STATUSES and self.paused and not self.cancelled:
            # THIS pause stopped this leaf — deliberately, ``_cancel_inflight``
            # kills what is in flight because it would all 429 too. That is not
            # the shape failing: the remedy is the wait the run is about to do,
            # and the run comes back. Reported like any other fault, but
            # discounted from the "an earlier stretch really failed" verdict, or
            # a pause with a backlog would keep a perfectly clean resume from
            # ever being certified as a template (§12).
            #
            # A USER cancel is deliberately NOT this case: it leaves ``cancelled``
            # set, the run seals ``cancelled``, and ``record_outcome`` is skipped
            # outright — unchanged.
            self._record_pause_caused_fault(message)
            return
        owner = owner_node_id or node_id
        if self.note_route_fault(owner, result, message, node=node):
            # A dead ROUTE, not a dead call (#43): the pause owns the verdict and
            # recorded it once, discounted like every other pause's own fault.
            # Degrading here instead would keep scheduling nodes onto a
            # credential the provider has already refused for this run.
            return
        if self.route_fault_owner(owner):
            # A SIBLING of the leaf that latched it, dying of the same dead route
            # a moment later — a fan-out dispatches its whole width before the
            # first refusal can land, so this is the ordinary shape, not a rare
            # race. Their deaths are the pause's evidence, not a second verdict:
            # counting them would seal ``prior_degraded`` on a run whose only
            # problem was one route, and no adapted resume could ever clear it.
            self._record_pause_caused_fault(message)
            return
        self.record_fault(message)

    def count_cap_trip(self) -> None:
        """Record a budget refusal the NODE handler will never see (WF/sol #3).

        ``_run_node`` counts a ``FanoutRejected`` that escapes a strategy, but the
        pipeline catches its own on an on_done worker — the raise never reaches
        the node thread, so the trip has to be counted here or a truncated run
        seals as if nothing had been refused. Locked, for the same reason
        ``count_validation_retry`` is."""
        with self._result_lock:
            self._result.cap_trips += 1

    def count_validation_retry(self) -> None:
        """Record a schema-validation retry in the rollup (scalar + pipeline).
        Locked: pipeline retries run on concurrent on_done workers."""
        with self._result_lock:
            self._result.validation_retries += 1

    def count_leaf_respawn(self) -> None:
        """Record ONE extra leaf bought for a cell the author wrote once (Q2).

        Distinct from ``count_validation_retry``: that one is a STEER inside a
        living sub-session, this one is a whole new leaf on the same route.
        Locked for the same reason — the pipeline re-spawns off its on_done
        workers, not off the run thread."""
        with self._result_lock:
            self._result.leaf_respawns += 1

    def mark_recovered(self, sub_ids: Iterable[str]) -> None:
        """The series ended with a winner: retire the faults its dead attempts
        wrote from the VERDICT (Q2, #43).

        Retired, never erased — every message stays in ``faults`` exactly where
        it landed, and lands in ``recovered_faults`` as well so a reader of a
        ``complete`` run can reconcile it against the faults it still lists.
        Popping is what makes this idempotent and keeps the ledger bounded: a
        leaf's fault can be recovered once, by the one series that owns it."""
        with self._result_lock:
            for sub_id in sub_ids:
                self._result.recovered_faults.extend(self._attempt_faults.pop(sub_id, ()))

    def route_fault_owner(self, node_id: str) -> bool:
        """Is a ``route_fault`` pause latched on exactly THIS node?

        The one question that decides whether another death at this node is the
        pause's own evidence or an independent lesson about the spec. Reason AND
        node: a route fault raised somewhere else says nothing about this one."""
        return self._pause.reason == ROUTE_FAULT and self._pause.node == node_id

    def mark_route_fault_caused(self, node_id: str, sub_ids: Iterable[str]) -> None:
        """A series that ended in a ``route_fault`` pause: retire its numbered
        faults into that pause (#43 x Q2).

        The second door to Q2's discount, opened on the pause's grounds rather
        than on a recovery's. Q2 discounts a series that found a WINNER, because
        the node produced its output and the DAG carried on. This one never did —
        and yet the same argument holds for the same reason: the exhaustion is
        precisely what the run PAUSED on, the pause's own verdict already says
        so, and the remedy is a route, not a spec edit. Leaving the attempts to
        count would make ``retries`` self-defeating from the other side: the
        author who bought resilience would be guaranteed an uncertifiable run
        every time a route dies, and a resume that adapts the route and finishes
        clean would still seal ``prior_degraded``.

        Fail-closed on the REASON **and on the NODE**, checked here rather than by
        the caller: only a route fault opens this door, and only for the node the
        pause actually latched on. A quota pause, a cancel or a stop mid-series
        leaves the numbered faults counting exactly as before — and so does a
        route fault raised somewhere ELSE while this series was in flight, whose
        deaths this node's attempts are no evidence about. Err toward "don't
        certify this", the safe direction for a decision that publishes.

        Retired, never erased: the messages stay in ``faults`` verbatim and the
        leaves stay counted in ``leaf_respawns``, so the price of the dead route
        survives the discount. Popping keeps it idempotent, like
        ``mark_recovered`` — a fault is retired once, by whoever owns it."""
        with self._result_lock:
            if not self.route_fault_owner(node_id):
                if node_id not in self._reroute_faults:
                    return
                # A RE-ROUTE, not a pause, is what ended this series (#63): the
                # operator's envelope owns the verdict, so the numbered faults
                # are HELD for its discount — granted only if the new route goes
                # on to answer (``mark_reroute_recovered``), never on the pause's
                # administrative grounds, because nothing here is waiting.
                #
                # Checked SECOND on purpose. A node the envelope already moved
                # can die again on the new route and pause for real, and then the
                # pause owns these attempts: leaving them in the re-route's bucket
                # would let a run that ends `paused` seal `prior_degraded` on
                # faults the pause itself caused — exactly the discount Q2 exists
                # to give, lost through the door this slice opened.
                for sub_id in sub_ids:
                    self._reroute_faults[node_id].extend(
                        self._attempt_faults.pop(sub_id, ())
                    )
                return
            for sub_id in sub_ids:
                self._result.pause_faults.extend(self._attempt_faults.pop(sub_id, ()))

    def account_leaf(self, sub_id: str) -> None:
        """Fold a TERMINAL leaf's cost/rigor into the rollup, once per sub_id
        (deduped — tokens accumulate across a sub's turns, so account at the end).

        ONE-SHOT by ``_accounted``, over a sub-session that can run MORE than one
        turn: whoever one day accounts a leaf and then steers it again loses the
        next turn's usage silently, because the second fold is deduped away.
        Latent today — every engine path either collects blocking before it
        accounts (the schema correction) or re-spawns a fresh leaf (the
        pipeline's retry), so nothing accounts BETWEEN two turns of the same
        sub-session. A future path that steers an accounted leaf owes this dedup
        a per-turn key, not a per-sub_id one (#60).
        Locked: pipeline leaves complete on concurrent on_done workers, so the
        ``+=`` on the shared counters would lose updates unguarded.

        The same cost is charged to the BUDGET, not just the rollup: the rollup
        of a nested run only folds into its parent when the nested run ends, so a
        gate reading it would be blind to a sub-workflow still in flight. The
        budget is shared by reference, so charging there is visible immediately.

        TERMINAL is the whole precondition (issue #42). A leaf read while it is
        still inside a provider call has no bill yet: writing its zero down —
        and spending its one trip through the dedup — froze "this leaf was free,
        and its usage is certain" into the rollup forever. Such a read now
        accounts NOTHING and defers (``_defer_account``). Nor is anything folded
        after the seal: the rollup that was persisted is the one that stands."""
        r = self._core.collect(sub_id, wait=False)  # read; no shared-state mutation
        if not leaf_settled(r):
            self._defer_account(sub_id)
            return
        usage = leaf_usage(r)
        # Hoisted out of the lock: the overrun advisory below names the leaf,
        # and it is recorded AFTER the lock releases (record_advisory_fault
        # takes it again, and the sink beyond it takes one of its own). The read
        # is safe unguarded even though ``spawn_leaf`` writes the entry under
        # ``_result_lock``: this leaf is TERMINAL, so the spawn that wrote its
        # entry happens-before the completion that brought us here — there is no
        # interleaving in which the key is missing but the leaf has settled.
        node_id = self._leaf_node.get(sub_id, self._current_node)
        with self._result_lock:
            if sub_id in self._accounted or self._sealed:
                return
            self._accounted.add(sub_id)
            self._pending_account.discard(sub_id)
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
            previous = self._result.node_costs.get(node_id, NodeCost())
            self._result.node_costs[node_id] = previous.merge(
                usage, r.get("provider"), r.get("model")
            )
            if r.get("forced_fallback"):
                self._result.forcing_fallbacks += 1
            if r.get("usage_uncertain"):
                # The leaf's stream was closed mid-flight by a cancel (issue
                # #42): ``usage`` above is what the provider MANAGED to report,
                # which for an abort on the first round-trip is zero. The tokens
                # are still charged as read — inventing a number would be worse
                # — but the count of leaves whose bill is unknown travels beside
                # them, so a rollup reading "0 tokens" is never mistaken for
                # "this leaf was free".
                self._result.usage_uncertain_leaves += 1
        # The BUDGET is deliberately still two axes (Fatia C): ``input_tokens``
        # is now uniformly the uncached prompt, and cache is a REPORT column,
        # never a spending limit.
        crossed = self._budget.charge_tokens(usage.input_tokens, usage.output_tokens)
        if crossed:
            # The ceiling was crossed by a leaf ALREADY IN FLIGHT (issue #71).
            # The gate is soft on purpose, so this charge is right and the run
            # is not degraded by it — but a `complete` whose spend is a multiple
            # of its ceiling, with nothing anywhere saying so, is the silence
            # this marker ends. ADVISORY (#45): visible in ``faults``, and
            # discounted by the verdict exactly like the artifact divergences.
            # Once per CROSSING, never once per leaf that lands afterwards —
            # ``charge_tokens`` returns True to a single caller.
            budget = self._budget
            self.record_advisory_fault(
                f"{TOKEN_BUDGET_OVERRUN}: spent {budget.tokens_spent} "
                f"of {budget.token_budget} (leaf {node_id})"
            )
        if refund:
            # OUTSIDE ``_result_lock``: the budget takes a lock of its own, and
            # nesting two of them in one order here is how a deadlock lands later
            # (the strategies.py rule, applied to the budget).
            self._budget.refund(1)

    def _defer_account(self, sub_id: str) -> None:
        """A leaf that has NOT settled: write nothing, remember it, arm a second
        chance (issue #42).

        The one caller that really lands here is the scalar timeout path — a
        leaf that ignored the cancel and outlived the quiescence wait is still
        ``running`` when ``account_leaf`` reads it. Its second chance is a late
        ``on_done`` hook on the core: the very worker that finishes the turn
        accounts it, on the spot, for the real number. Non-blocking on both
        sides, as this whole path must be.

        Nothing is armed when the core refuses (already terminal, gone, or
        already hooked — the pipeline's own ``on_done`` is that second chance
        and must not be stolen). Terminal-in-the-window is charged right here,
        so a leaf that landed a microsecond ago is not deferred to the seal for
        no reason; anything else is left to the hook that exists or, failing
        every one of them, to ``_settle_pending``."""
        with self._result_lock:
            if sub_id in self._accounted or self._sealed:
                return
            self._pending_account.add(sub_id)
        try:
            armed = self._core.watch_done(sub_id, self.account_leaf)
        except Exception:  # a second chance must never be what kills a run
            logger.exception("workflow: could not arm late accounting for %s", sub_id)
            armed = False
        if not armed and leaf_settled(self._core.collect(sub_id, wait=False)):
            self.account_leaf(sub_id)

    def _settle_pending(self) -> None:
        """Close the books on every leaf that was read before it settled.

        Second chance first: a leaf that landed between the timeout and the seal
        is accounted for REAL here (its own late hook may have done it already —
        ``account_leaf`` is idempotent). What is left gets the only honest entry
        available: no tokens invented, one more leaf whose bill is unknown, and a
        fault naming the node that spawned it AND WHICH of the two causes it is
        — still inside a provider call, or gone from the registry. The cause is
        read off the core one last time, deliberately OUTSIDE the lock (the
        engine's result lock never nests inside the core's), because a fault that
        says "still running" about a leaf that finished long ago and was evicted
        is a fault with a false cause.

        The count and the seal happen in ONE critical section: setting
        ``_sealed`` in a later one would let a hook firing in the gap add the
        leaf's usage AND leave the "usage unknown" fault standing about the same
        leaf. The faults are written outside it — ``record_fault`` takes the same
        lock (the strategies.py rule).

        **Latent trap, named:** these are ORDINARY faults, not administrative
        ones (they never reach ``pause_faults``). Today only the scalar timeout
        path populates ``_pending_account``, and a timeout fault is already an
        ordinary fault, so there is no delta — but a pause that one day sealed
        with pending leaves would have its stragglers counted against the
        verdict as real failures. Whoever adds the second producer decides that,
        with the pause's own ``_record_pause_caused_fault`` right there."""
        with self._result_lock:
            pending = list(self._pending_account)
        for sub_id in pending:
            self.account_leaf(sub_id)
        with self._result_lock:
            candidates = [s for s in self._pending_account if s not in self._accounted]
        causes = {s: self._core.collect(s, wait=False) for s in candidates}
        with self._result_lock:
            # The LIVE set, not the snapshot above: a leaf deferred between the
            # two (``_sealed`` is still False, so ``_defer_account`` admits it)
            # would be counted by nobody and refused by everybody afterwards.
            # Whatever settled in the loop above ``account_leaf`` discarded from
            # this set, so what is left is exactly the residue.
            stragglers = [s for s in self._pending_account if s not in self._accounted]
            self._sealed = True
            self._result.usage_uncertain_leaves += len(stragglers)
        for sub_id in stragglers:
            node_id = self._leaf_node.get(sub_id, self._current_node)
            # A straggler with no read of its own was deferred in the gap above,
            # microseconds ago and by definition as NOT-terminal: "still
            # running" is the truthful default for it.
            read = causes.get(sub_id)
            cause = (
                UNKNOWN_AT_SEAL
                if read is not None and leaf_unknown(read)
                else UNSETTLED_AT_SEAL
            )
            self.record_fault(f"{node_id}: {cause}")

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
        self, sub_id: str, schema: dict | None, *, timeout: float | None = None,
        attempt: tuple[int, int] | None = None, node: Any = None,
    ) -> Any:
        """Collect + validate a leaf, then account its cost once.

        ``attempt`` only ever travels down to the fault text (see
        ``note_leaf_failure``); nothing here branches on it, and every caller
        that does not re-spawn leaves it None."""
        output = self._collect_validate(
            sub_id, schema, timeout=timeout, attempt=attempt, node=node
        )
        self.account_leaf(sub_id)
        return output

    def _timed_out(self, sub_id: str, result: dict, limit: float) -> bool:
        """A leaf still ``running`` after a blocking collect blew its deadline.

        Walking away leaves a ZOMBIE: the turn keeps holding one of the core's
        few workers with nobody left to read its answer. Cancel it (cooperative
        interrupt) and record the timeout as its own fault — a bare "leaf
        running" told the author nothing about what actually happened.

        The cancel is cooperative, so it is not the end of the story: we wait a
        short bounded moment for the leaf to really go quiet (issue #42-B), and
        the fault SAYS which way it went. The successor node shares the run's
        filesystem scope with the leaf being cancelled — its ``working_root``,
        any ``fs_allow`` root, and (if the operator opted the leaf into a
        shell) anything that shell can reach beyond either allowlist —
        "still running" is the difference between a clean hand-off and a
        successor reading state somebody else is still writing.
        """
        if result.get("status") != "running":
            return False
        self._core.cancel(sub_id)
        with self._result_lock:
            self._timed_out_leaves.add(sub_id)
        # ONE leaf, so one cap. The node loop is sequential, so a node whose
        # leaves all blow their deadline pays this cap once per leaf — the price
        # of collecting them one at a time (see ``quiescence``); the alternative
        # would be killing the whole fan-out on the first straggler.
        report = await_quiescence(self._core, [sub_id])
        suffix = report.suffix()
        # The cancel that just settled this leaf may have closed a stream in
        # flight (issue #42, épico E3) — the very reason it settled so fast.
        # Read off the core, the same source ``account_leaf`` uses, so the fault
        # and the rollup counter can never tell two different stories.
        if self._core.collect(sub_id, wait=False).get("usage_uncertain"):
            suffix = f"{suffix}; {USAGE_UNCERTAIN_CAUSE}"
        self.record_fault(
            f"{self._current_node}: leaf timeout after {limit:.0f}s ({suffix})"
        )
        return True

    def _collect_validate(
        self, sub_id: str, schema: dict | None, *, timeout: float | None = None,
        attempt: tuple[int, int] | None = None, node: Any = None,
    ) -> Any:
        """Collect a leaf; if ``schema`` given, validate + steer-retry on the same
        sub-session. Returns the validated object, raw text (no schema), or None
        (dead leaf / timeout / exhausted retries). The leaf keeps its full toolset."""
        limit = LEAF_TIMEOUT if timeout is None else timeout
        result = self._core.collect(sub_id, wait=True, timeout=limit)
        if self._timed_out(sub_id, result, limit):
            return None
        if result.get("status") != "complete":
            self.note_leaf_failure(
                self._current_node, result, attempt=attempt, sub_id=sub_id, node=node
            )
            return None
        output = result.get("output")
        if schema is None or output is None:
            return output
        # NOT ``attempt`` — that is this method's PARAMETER, the ``(i, n)`` of the
        # same-route re-spawn series (``leaf_retry.py``). Naming the correction
        # round the same thing shadowed it, and the second ``note_leaf_failure``
        # below then handed an int to code that indexes a tuple: the real leaf
        # cause vanished behind a TypeError raised as an engine fault. The two
        # counters are genuinely different things, so they get different names.
        for validation_round in range(MAX_VALIDATION_RETRIES + 1):
            ok, parsed, error = parse_and_validate(output, schema)
            if ok:
                return parsed
            if validation_round == MAX_VALIDATION_RETRIES:
                logger.warning("workflow: schema not satisfied after retries: %s", error)
                return None
            # The fix is an INTERNAL steer — it draws from the run's steering
            # budget first, and a refused reservation is fail-closed: no
            # correction is issued and the node settles on its invalid output.
            reservation = self._steering.reserve_internal(sub_id)
            if not reservation.accepted:
                self.record_fault(
                    f"{self._current_node}: steering correction limit exhausted "
                    f"({reservation.reason})"
                )
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
                self.note_leaf_failure(
                    self._current_node, retry, attempt=attempt, sub_id=sub_id, node=node
                )
                return None
            result = retry
            output = retry.get("output")
            if output is None:
                return None
        return None

    def collect_validated(
        self, node: Node, sub_id: str, *, attempt: tuple[int, int] | None = None
    ) -> Any:
        """Collect a node's leaf under the NODE's own deadline (falling back to
        the global one), validating against the node's own schema."""
        return self.collect_with_schema(
            sub_id, self._node_schema(node),
            timeout=node_timeout(node.fields, LEAF_TIMEOUT), attempt=attempt,
            # The NODE, not just its id (#63): a refused credential is judged
            # against the node that authored the route, and the operator's
            # envelope may only move an ``agent``. Threaded down this one hop
            # rather than stashed on the engine — a stash is exactly the "one
            # refactor away from the wrong node" shape the pause predicate
            # already refuses to trust.
            node=node,
        )

    def leaf_retryable(self, sub_id: str) -> bool:
        """Did this leaf die a failure a SAME-ROUTE re-spawn could plausibly fix?

        Read off the core — the same read ``account_leaf`` already does, so no
        second source of truth about how a leaf ended. The decision itself lives
        in ``leaf_retry.is_retryable_failure`` next to the reasons for it.

        A leaf THIS engine cut off at its deadline is refused here regardless of
        what the core ends up reporting: the cancel is cooperative, so a provider
        error can still land in the same breath, and a timeout is not a failure a
        re-spawn may buy again."""
        if sub_id in self._timed_out_leaves:
            return False
        result = self._core.collect(sub_id, wait=False)
        return is_retryable_failure(result.get("status"), result.get("error_kind"))

    def leaf_result(self, sub_id: str) -> dict:
        """How a leaf ended, as the core knows it — status, kind and the ROUTE it
        really ran on. The same non-blocking read ``leaf_retryable`` does, exposed
        because a caller that has to NAME the dead route (#43) needs more than a
        yes/no about re-spawning it."""
        return dict(self._core.collect(sub_id, wait=False))

    def run(self, spec: WorkflowSpec, args: dict[str, Any] | None = None) -> RunResult:
        result = RunResult()
        self._result = result
        self._accounted = set()
        self._pending_account = set()
        self._sealed = False
        self._costs = {}
        self._leaf_node = {}
        self._timed_out_leaves = set()
        self._attempt_faults = {}
        self._replay_divergences = {}
        self._shared_path_replays = {}
        self._spawned = []
        self._schemas = spec.schemas
        self._aggregate_types = {
            node.id: node.type for node in spec.nodes if node.type in AGGREGATION_ELEMENT
        }
        self._aggregate_holes = {}
        self._spec_id = spec_identity(spec)
        base_context: dict[str, Any] = {"args": args or {}}
        ordered = topological_order(spec)
        result.nodes_total = len(ordered)
        self._progress.reset([node.id for node in ordered])
        for position, node in enumerate(ordered):
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
            # A node the author declared indispensable came back with nothing:
            # the run stops HERE (spec §7.4/§7.5). Never on a stop that is not a
            # failure -- a quota/budget/checkpoint pause nulls the node too, and
            # calling that "required failed" would tell the author to fix a spec
            # that is fine and bury the resume that was already scheduled.
            if not self.stopped and self._required_abort(node, output, result):
                self._skip_remaining(spec, node, ordered[position + 1:], result)
                break
        self._seal(result)
        return result

    def _required_abort(self, node: Node, output: Any, result: RunResult) -> bool:
        """Did this node just end the run? Records the fault that says why.

        Two ways a ``required`` node ends it. The old one is emptiness (the node
        produced nothing). The second (issue #74) is a ``completeness_check``
        that produced a real answer whose content is "this is not done": the
        output is kept — the gap list is the most useful thing the run has — and
        only the VERDICT changes."""
        if output is None and node.required:
            result.required_failure = node.id
            self.record_fault(required_fault(node.id))
            return True
        if node.required:
            gaps = completeness_gaps(node, output)
            if gaps is not None:
                result.required_failure = node.id
                self.record_fault(completeness_fault(node.id, gaps))
                return True
        if result.required_failure is not None:
            # Set by ``fold_nested``: the abort happened inside this node's
            # nested workflow, and it aborts the parent at this node.
            self.record_fault(nested_required_fault(node.id, result.required_failure))
            return True
        return False

    def _skip_remaining(
        self, spec: WorkflowSpec, failed: Node, remaining: list[Node], result: RunResult
    ) -> None:
        """Say, per node, that it will never run -- and why.

        Deliberately NOT nulled: nulling what never ran would poison
        ``null_rate`` and read downstream as "the leaves died", the same
        reasoning the cancel and pause paths already follow."""
        for skipped, fault in zip(remaining, skip_faults(spec, failed.id, remaining)):
            self.record_fault(fault)
            self._progress.skip(skipped.id)
            self._emit_node(skipped.id, SKIPPED)

    def _seal(self, result: RunResult) -> None:
        """Stamp the run's verdict, on the run thread only. An explicit cancel
        outranks a pause: the user stopped this run, so nothing should resume it.

        The books close BEFORE the verdict: a leaf still in flight is one more
        unknown bill and one more fault, and both belong to the rollup this
        stretch persists (issue #42) — and closing them may never COST the run
        the rollup: ``_seal`` runs outside the per-node try/except, so an
        exception here would reach ``service`` and mark a finished run
        ``failed``, throwing away a complete result over a bookkeeping tail."""
        try:
            self._settle_pending()
        except Exception:
            logger.exception("workflow: failed to settle pending leaf accounting")
            # Close the books anyway: the rollup below is persisted either way,
            # and a books pass that broke is the LAST moment to let a late hook
            # start adding usage to a result nobody will save again.
            with self._result_lock:
                self._sealed = True
        # The stretch's replay advisories, aggregated (#75) — BEFORE the verdict
        # reads the fault list, and before the early returns below: a cancelled
        # or paused stretch replayed those cells too.
        self._flush_replay_advisories()
        if self.cancelled:
            result.status = "cancelled"
            return
        if self.paused:
            result.status = "paused"
            result.pause_reason = self._pause.reason
            result.retry_after = self._pause.retry_after
            # ONE payload slot on the latch, two readers with two remedies, so
            # the REASON decides who gets it: a route payload arriving in the
            # ``checkpoint`` field would read as a human gate nobody authored.
            result.checkpoint = self._pause.payload if self._pause.reason == CHECKPOINT else None
            result.route_fault = (
                self._pause.payload if self._pause.reason == ROUTE_FAULT else None
            )
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
