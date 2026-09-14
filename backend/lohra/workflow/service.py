"""WorkflowService — validate a spec, run the engine in the background, track runs.

The Lohra agent authors a spec and calls ``run_workflow``; this validates it
(didactic error BEFORE any spawn), builds a per-run OrchestrationCore whose leaf
factory is SANDBOXED (fs/egress allowlist + taint, spec §8.3), runs the engine on
a background thread, and returns a ``run_id`` immediately. ``status``/``cancel``
read/stop a run. Each run is isolated under ``~/.lohra/runs/<run_id>/``.
"""

from __future__ import annotations

from collections import Counter
import itertools
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from lohra.agent.types import Usage
from lohra.orchestration.core import OrchestrationCore
from lohra.providers.errors import QUOTA_EXHAUSTED
from lohra.state import SessionDB
from lohra.state.runstate import StateWrite
from lohra.workflow import library, rollup
from lohra.workflow.audit import (
    CHANNEL_CHECKPOINT_ANSWERS,
    AuditTrail,
    causal_audit_event,
    resolve_audit_settings,
)
from lohra.workflow.autoresume import AutoResumeScheduler, ResumePlan
from lohra.state.runstate import ResumeToken
from lohra.workflow.budget import Budget
from lohra.workflow.accounting import RunResult
from lohra.workflow.cache import NodeCache, spec_identity
from lohra.workflow.cache_preview import preview_resume
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.failure_taxonomy import SIGNAL_SPEC_SHAPE
from lohra.workflow.events import DONE, ITEMS, NODE, PLAN, EventEmitter, OnEvent, plan_payload
from lohra.workflow.insight_producers import record_lint_warning_candidates
from lohra.workflow.insight_view import project_insights
from lohra.workflow.launch import checkpoint_answers as resolve_checkpoint_answers
from lohra.workflow.launch import launch_args, launch_spec, route_answer
from lohra.workflow.lease_heartbeat import TimerFactory
from lohra.workflow.route_fault import (
    abort_fault,
    apply_reroutes,
    apply_route_answer,
    reroute_fault,
    route_change,
    route_label,
)
from lohra.workflow.lint import lint_warnings, with_warnings
from lohra.workflow.notify import OnRunDone, notify_done
from lohra.workflow.runstate_store import (
    FINISHED_STATUSES as FINISHED_STATUSES,  # compatibility re-export
    RECOVERED_FAULT,
    RUN_LEASE_TTL,
    DurableRun,
    RunStateStore,
    busy_error,
    carried_advisory,
    carried_recovered,
    carried_rerouted,
    carried_substitutions,
    durable_rollup,
    list_entry,
    live_entry,
    progress_fields,
    pause_fields,
    run_leaf_respawns,
    run_artifact_advisories,
    run_overrun,
    run_replay_divergences,
    run_replay,
    run_uncertain,
    view_of,
)
from lohra.workflow.artifact import ArtifactScope
from lohra.workflow.cell_stamp import CellStamp
from lohra.workflow.sandbox import WorkflowPolicy, load_policy, make_sandboxed_leaf_factory
from lohra.workflow.schema import ValidationError, validate_spec
from lohra.workflow.supervision import steer_live_run
from lohra.workflow.submission import LaunchAdmission, Submission
from lohra.workflow.operator_budget import (
    ORIGIN_INHERITED,
    ORIGIN_SPEC,
    AppliedBudget,
    apply_operator_cap,
    normalize_operator_cap,
)
from lohra.workflow.spend import (
    engine_spent,
    engine_split,
    persist_spend,
    refuse_spent_budget,
    seed_charges,
    seed_spend,
    seed_split,
    split_total,
    spent_total,
    validate_token_budget,
)
from lohra.workflow.strategies import STRATEGIES
from lohra.workflow.routes import ROUTES_FILE, RouteEnvelope, load_routes
from lohra.workflow.tiers import TierMap, load_tiers

# Node types the engine can actually execute (rejects valid-but-unbuilt at author
# time so the model never authors into a silently-nulled node).
SUPPORTED_NODE_TYPES = frozenset(STRATEGIES)

logger = logging.getLogger(__name__)

ChildFactory = Callable[[], Any]
DEFAULT_RUN_CONCURRENCY = 4
DEFAULT_MAX_RUNS = 4  # concurrent workflow runs in this process
# The listing rides back inside a tool result the model reads, so it is
# bounded like everything else here — newest first, oldest dropped.
MAX_LISTED_RUNS = 50
# How long a finished run waits for its own `segment.completed` to reach the
# ledger before publishing its terminal line (see `_close_audit_segment`). One
# bounded moment at the very end of a run, in the `AuditTrail.flush` house
# default: long enough for a healthy sink, short enough that a dead one never
# holds a lease hostage.
AUDIT_CLOSE_TIMEOUT = 1.0


def _spec_name(spec_dict: Any) -> str:
    """The spec's own ``meta.name``, so a listing reads as more than hex run ids.
    Best-effort: an unnamed spec lists with an empty name, never a crash."""
    if not isinstance(spec_dict, dict):
        return ""
    meta = spec_dict.get("meta")
    return str(meta.get("name") or "") if isinstance(meta, dict) else ""


def _finished_error(run_id: str, status: str) -> str:
    """Why a cancel was refused: the run already ended with a real verdict, and
    overwriting it would erase the outcome somebody is about to read."""
    return (
        f"workflow run {run_id!r} already finished (status: {status}); "
        f"there is nothing to cancel"
    )


def _run_budget(state: "RunState") -> dict[str, int] | None:
    """The rollup's ``token_budget``: the live engine's snapshot plus the RUN's
    overrun HIGH-WATER MARK (#71).

    Two numbers because they answer two questions. ``overrun`` is "how far over
    the ceiling in force RIGHT NOW", which a renewal legitimately returns to 0.
    ``overrun_max`` is "how far over any ceiling this run has EVER been", which a
    renewal must not erase: the canonical path is overrun -> pause -> the human
    raises the ceiling -> complete, and reporting only the first would print a
    clean 0 beside the overrun advisory still in the run's own faults_total.
    ``total``/``spent``/``remaining`` are already the whole run's (the budget is
    seeded cumulatively on a resume); only the mark needs carrying."""
    if state.engine is None:
        return None
    snapshot = state.engine.budget.snapshot()
    if snapshot is None:
        return None
    return {**snapshot, "overrun_max": run_overrun(state)}


def _is_live(state: "RunState") -> bool:
    """True while a run may still be writing (its thread is alive, or it never
    reached a terminal status). Resuming onto a live run would hand two engines
    the same node cache (the working roots are per-acquisition since issue #12,
    but one cache is quite enough to corrupt).

    A pending Future remains live even after the functional decision. Once it
    completes, a refused persistence operation cannot keep the dead worker live
    forever just because its last durable status is still ``running``. A launch
    not yet handed to a Future uses its initial running state as the guard.
    """
    if state.future is not None:
        return not state.future.done()
    return state.status == "running"


@dataclass
class RunState:
    run_id: str
    # Monotonic launch order. dict insertion order lies on a resume — the entry
    # is REPLACED under the same key and keeps its original slot — so the
    # listing sorts on this instead.
    seq: int = 0
    name: str = ""  # spec meta.name, so a listing reads as more than hex ids
    # The session that launched this run, so a finished run can be announced in
    # that session's steer inbox (M6). None = nobody to tell.
    owner: str | None = None
    status: str = "running"  # running | complete | degraded | failed | cancelled | paused
    revision: int = 0
    state_lock: Any = field(default_factory=threading.Lock, repr=False)
    result: RunResult | None = None
    error: str | None = None
    core: OrchestrationCore | None = None
    # The live engine, so cancel() can stop the NODE LOOP (not just the pool):
    # without it the loop keeps scheduling into a shut-down pool and every
    # remaining node lands as a confusing "cannot schedule new futures" fault.
    engine: WorkflowEngine | None = None
    future: Future | None = None
    # What it takes to re-launch this run without the original caller: the tool
    # caller resends the spec each time, an auto-resume has nobody to ask.
    spec_dict: dict | None = None
    args: dict | None = None
    tainted: bool = False
    # Auto-resume bookkeeping (attempts already fired for THIS run_id).
    attempts: int = 0
    # Faults from the run's EARLIER stretches (WF-26). Each launch builds a
    # fresh RunResult, so a resumed run's rollup used to close reporting only
    # the last segment — the pause that stopped the previous one vanished.
    # Carried across processes too (WF-29): they are written to the run's
    # durable line at every transition, so a resume in a NEW process reports the
    # whole run's faults rather than the segment it happens to have run.
    prior_faults: list[str] = field(default_factory=list)
    # Did an EARLIER stretch fail for a reason that is a lesson about the SPEC?
    # A pause is not one (waiting, or a raised ceiling, is the whole remedy), so
    # the pause's own fault is discounted — and so are the faults it CAUSED (the
    # leaves a quota pause cancels on purpose). Those were the pause, not the
    # shape: counting them meant a pause with a backlog could never be resumed
    # into a certified template, however cleanly the resume ran. Faults folded up
    # from a NESTED run still count — err toward "don't certify this", which is
    # the safe direction for a decision that publishes.
    prior_degraded: bool = False
    # The faults earlier stretches RECOVERED from by re-spawning the same route
    # (Q2, #43) — discounted from the verdict exactly like the pause's own, and
    # carried for exactly the same reason: this stretch's ``RunResult`` is fresh
    # and cannot recognise a series that ran before it existed.
    prior_recovered: list[str] = field(default_factory=list)
    # The nodes this run had re-routed through the command channel (#43), so the
    # template a clean last stretch certifies can be stamped with the emergency
    # route it needed — the ``leaf_respawns`` precedent: "this works, and here
    # is what it took".
    prior_rerouted: list[str] = field(default_factory=list)
    # ...and the {node, from, to} rows of every model an earlier stretch had
    # to SUBSTITUTE because the authored slug does not exist (#85).
    prior_substitutions: list[dict] = field(default_factory=list)
    # ...and what earlier stretches were merely ADVISED about (#45) — discounted
    # from the verdict on the same terms, and carried for the same reason: a
    # fresh ``RunResult`` cannot recognise a divergence a process ago.
    prior_advisory: list[str] = field(default_factory=list)
    # ...and the same advisories counted PER SOURCE (#75), because the certified
    # template stamps them apart and prose is not a discriminator.
    prior_artifact_advisories: int = 0
    prior_replay_divergences: int = 0
    # ...and the extra leaves those stretches paid for, so the counter the
    # rollup and the template report is the WHOLE run's.
    prior_leaf_respawns: int = 0
    # The high-water mark of how far past a ceiling this run ever went (#71).
    prior_overrun: int = 0
    # ...and the leaves those stretches lost mid-stream to a cancel (issue #42).
    # A pause cancels what is in flight, so the pause path is the biggest
    # producer of these — and a resume that reported 0 would be claiming the
    # whole run's usage is exact while spending a cumulative total that already
    # carries their floor.
    prior_uncertain: int = 0
    # ...and how many cells the earlier stretches served out of the node cache,
    # and what those cells had cost the first time (#61). Same carry as
    # ``prior_uncertain``: a resume that reported only its own stretch would say
    # a run replayed nothing every time it was resumed twice.
    prior_cells_replayed: int = 0
    prior_saved: int = 0
    # What EARLIER stretches spent on the cache/reasoning meters (Fatia C).
    # The budget seeds its two axes itself; these are report-only, so the
    # cumulative floor is carried here and re-written on every persist.
    prior_split: Usage = field(default_factory=Usage)
    resume_at: float | None = None
    # quota | token_budget | user_requested | checkpoint | route_fault
    pause_reason: str | None = None
    audit_segment_id: str | None = None
    # What a `checkpoint` pause is waiting for: {node_id, prompt, default?} (WF-10).
    checkpoint: dict | None = None
    # ...and what a `route_fault` pause stopped ON: {node_id, provider, model,
    # error_kind, cause} (#43). Never carried across a resume — a fresh RunState
    # starts empty, so a run that resumed onto a new route cannot keep reporting
    # the dead one.
    route_fault: dict | None = None
    # The ownership fence of the acquisition that launched THIS stretch (issue
    # #12). Bound once, here, rather than looked up per write: a run this same
    # process re-acquires gets a NEW fence, and a straggler from the stretch
    # before must present the old one and be refused, not borrow the new one.
    fence: int | None = None
    # True once this process was fenced OUT of the run (``_abort_fenced_run``):
    # somebody else owns it now, so this state describes a stretch that no longer
    # speaks for the run. Every read path must fall through to the durable line
    # instead — a fenced state that keeps answering masks the new owner, and
    # ``cancel``, which short-circuits on a live state, would report success over
    # a run this process has no claim on. The entry STAYS in ``_runs``: the run
    # thread's own finally still holds this state, and the identity-checked
    # cleanup (and the start-clash guard, which must keep refusing while a
    # straggling thread drains) still have to find it.
    fenced: bool = False


# The structural cause of a model substitution, as the insight store's
# fingerprint reads it (#85 x E1/#50). ``rule:`` is the closed vocabulary E1
# introduced so a candidate fingerprints on its CAUSE rather than on prose —
# without it, every substitution and every rejected spec would collapse into one
# recurrence-counted row (same mechanism, same lone ``SIGNAL_SPEC_SHAPE``).
# A function rather than a constant so callers cannot mutate the tuple they read.
MODEL_NOT_FOUND_RULE = "rule:model_not_found"


def model_substitution_signals() -> tuple[str, ...]:
    """The evidence a model substitution offers the taxonomy.

    ``SIGNAL_SPEC_SHAPE`` because the defect is in the SPEC — the slug names
    nothing, and no provider, key or network condition would have made it run.
    That, with ``mechanism="validation"`` and full confidence, is the branch
    ``failure_taxonomy._resolve`` maps to ``AGENCY``, which is the owner's
    decision on #54 expressed as evidence rather than as an assertion.
    """
    return (SIGNAL_SPEC_SHAPE, MODEL_NOT_FOUND_RULE)


class WorkflowService:
    def __init__(
        self,
        *,
        base_child_factory: ChildFactory,
        db: SessionDB,
        home: Path,
        policy: WorkflowPolicy | None = None,
        run_concurrency: int = DEFAULT_RUN_CONCURRENCY,
        max_runs: int = DEFAULT_MAX_RUNS,
        client_pool: Any | None = None,
        on_run_done: OnRunDone | None = None,
        on_event: OnEvent | None = None,
        tiers: TierMap | None = None,
        clock: Callable[[], float] = time.time,
        lease_ttl: float = RUN_LEASE_TTL,
        lease_timer_factory: TimerFactory | None = None,
        resume_timer_factory: TimerFactory | None = None,
        operator_cap: int | None = None,
        routes: RouteEnvelope | None = None,
        default_route: tuple[str, str] | None = None,
    ) -> None:
        self._base_factory = base_child_factory
        # The operator's pre-authorized token ceiling (#47): resolved by the
        # entrypoint from --token-budget-cap / LOHRA_TOKEN_BUDGET_CAP. It is a
        # ceiling PER RUN, applied to every run this process launches — a spec
        # that asks for more is clamped, a spec that asks for nothing inherits
        # it. It is NOT a process total: N runs still cost up to N×cap (the same
        # gap §7.3 leaves open for concurrency). None = exactly the behaviour
        # before it existed; 0/negative is refused at this boundary, since it
        # would read as "cap everything at nothing" and pause every run.
        self._operator_cap = normalize_operator_cap(operator_cap, where="WorkflowService")
        # ``(provider, model)`` this process was launched on (#85). Read by the
        # engine for ONE question — which tier a node that declared none was
        # working at — and never as a route: absent simply means such a node
        # is not substituted, and its dead slug reaches the §7.7 pause.
        self._default_route = default_route
        self._db = db
        self._home = home
        self._client_pool = client_pool  # cross-provider leaf clients (may be None)
        self._policy = policy if policy is not None else load_policy(home / "workflow_policy.json")
        # Operator model-tier map (WF-5): same rule as the capability policy —
        # it lives on disk, never in a spec, so an authored spec cannot point
        # itself at a model the operator did not sanction.
        self._tiers = tiers if tiers is not None else load_tiers(home / "workflow_tiers.json")
        # ...and the operator's ROUTE ENVELOPE (#63), loaded the same way and for
        # the same reason: what a dead route may fall back to is the operator's
        # standing authorization, written before the run, never something a spec
        # (or the agent that authored it) can grant itself.
        self._routes = routes if routes is not None else load_routes(home / ROUTES_FILE)
        self._run_concurrency = max(1, run_concurrency)
        self._runs: dict[str, RunState] = {}
        self._seq = itertools.count()
        self._on_run_done = on_run_done
        # The live view (WF-30): plan/node/items/fault/done, pushed while the run
        # is still going. Also the pacer for the run's durable progress writes —
        # see _run_event.
        self._events = EventEmitter(on_event, clock=clock)
        # Independent bounded writer: workflow threads never wait on audit I/O.
        # On by default; `LOHRA_AUDIT=off` is the operator's off ramp, and
        # `LOHRA_AUDIT_MAX_EVENTS` raises the per-run cap for a big fan-out whose
        # prefix would otherwise be pruned exactly when it matters most.
        audit_settings = resolve_audit_settings()
        self._audit_enabled = bool(audit_settings["enabled"])
        self._audit = AuditTrail(
            db,
            clock=clock,
            enabled=audit_settings["enabled"],
            max_events_per_run=audit_settings["max_events_per_run"],
            # Resolved lazily: the store is built below, and the sink only ever
            # asks once a run of ours is under way (issue #12).
            fence_of=lambda run_id: self._store.fence_of(run_id),
        )
        self._lock = threading.Lock()
        self._launches = LaunchAdmission()
        self._lifecycle_lock = self._launches.lock
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_runs), thread_name_prefix="wf-run")
        self._autoresume = AutoResumeScheduler(self.resume, clock=clock,
                                              timer_factory=resume_timer_factory)
        # The durable half of a run (WF-29): the line a fresh process resumes
        # from, and the lease that says whether the last owner is still alive.
        self._store = RunStateStore(
            db, clock=clock, ttl=lease_ttl, timer_factory=lease_timer_factory,
            on_lease_lost=self._abort_fenced_run,
            on_acquired=lambda run_id, fence: self._autoresume.acquired(run_id, fence),
        )
        # A quota pause that outlived its process would otherwise wait forever:
        # its timer died with the process that armed it.
        self.rearm_pending_resumes()

    def set_autoresume(self, scheduler: AutoResumeScheduler) -> None:
        """Swap the auto-resume scheduler (tests inject timers and a clock)."""
        self._autoresume.shutdown()
        self._autoresume = scheduler

    def set_on_run_done(self, callback: OnRunDone | None) -> None:
        """Point the completion notification somewhere (equip wires it to the
        owning session's steer inbox). One sink per service."""
        self._on_run_done = callback

    def _prior(self, resume_run_id: str | None) -> DurableRun | None:
        """Resolve replay inputs from durable state once the local stretch stops.

        A live stretch still supplies its freshest view. A stopped local copy
        cannot override cancellation or a newer pause committed by another process.
        """
        if not resume_run_id:
            return None
        state = self._get(resume_run_id)
        durable = self._store.load(resume_run_id)
        if state is None or (not _is_live(state) and durable is not None):
            return durable
        view = view_of(state)
        return replace(
            view, audit_segment_id=durable.audit_segment_id if durable is not None else None
        )

    def start(
        self,
        spec_dict: Any = None,
        args: dict | None = None,
        *,
        tainted: bool = False,
        resume_run_id: str | None = None,
        token_budget: int | None = None,
        owner: str | None = None,
        checkpoint_answers: dict | list | None = None,
        agency_authored: bool = False,
    ) -> dict:
        # Reserve preparation only; SQLite and callbacks run outside admission's
        # mutex. Shutdown waits for acceptance or cleanup of this exact attempt.
        with self._launches.reserve(resume_run_id) as submission:
            if submission.error is not None:
                return {"error": submission.error}
            return self._start_unlocked(
                spec_dict,
                args,
                tainted=tainted,
                resume_run_id=resume_run_id,
                token_budget=token_budget,
                owner=owner,
                checkpoint_answers=checkpoint_answers,
                agency_authored=agency_authored,
                submission=submission,
            )

    def _start_unlocked(
        self,
        spec_dict: Any = None,
        args: dict | None = None,
        *,
        tainted: bool = False,
        resume_run_id: str | None = None,
        token_budget: int | None = None,
        owner: str | None = None,
        checkpoint_answers: dict | list | None = None,
        agency_authored: bool = False,
        pause_prior: DurableRun | None = None,
        resume_token: ResumeToken | None = None,
        submission: Submission,
    ) -> dict:
        """Validate + launch a run. Returns {run_id, status} or {error} (didactic).

        ``resume_run_id`` re-runs reusing that run's node cache (completed cells
        replay) — resume after a crash (spec §6). It also CONTINUES the run's
        token tally: a resume that restarted the count at zero would let a run
        spend its whole ceiling again on every retry.

        ``token_budget`` caps what the whole run may spend (§7.1). Omitted on a
        resume, the run inherits the ceiling it was launched with.

        ``spec_dict`` may be omitted on a resume: the run's own persisted spec is
        replayed (WF-22), and so are its ``args`` when none are sent (WF-24).

        ``checkpoint_answers`` ([{address, answer}], or unambiguous legacy map) satisfies the human gates a
        previous stretch paused on (WF-10); each answer becomes that node's
        output and is cached, so the question is never asked twice.

        ``tainted`` (fed by WorkflowTool from the session's TaintTracker) → leaves
        run with NO fs read and NO web egress (§8.2 control 3). Defense-in-depth on
        top of the always-on DEFAULT-DENY fs + egress sandbox (§8.3).

        ``agency_authored`` — provenance of the AUTHORING surface (SUP-05): only
        the agent's own ``run_workflow`` sets it True. A spec ``validate_spec``
        rejects is then a high-confidence authoring fault, recorded IMMEDIATELY
        (before the didactic return) as a CANDIDATE in ``db.insights``. Operator
        and test callers leave it False: an invalid spec they sent is not the
        agent's to learn from, and the store's gate re-derives agency from the
        evidence anyway, so the flag cannot attribute on its own."""
        explicit_spec = spec_dict is not None
        prior = pause_prior or self._prior(resume_run_id)
        audit_unclosed = bool(prior is not None and prior.audit_segment_id)
        # Decided FIRST, before a spec is resolved, a lease taken or anything is
        # written (#43, decisão 1): an ``abort`` must not fail on "no spec on
        # file", and a refused answer must cost the run nothing at all.
        answered = route_answer(resume_run_id, checkpoint_answers, explicit_spec, prior)
        if answered.error is not None:
            return {"error": answered.error}
        if answered.abort_node is not None:
            return self._abort_route_fault(str(resume_run_id), prior, answered.abort_node)
        checkpoint_answers = answered.answers  # the route key is not a checkpoint's
        spec_dict, missing = launch_spec(spec_dict, resume_run_id, prior)
        run_args = launch_args(args, resume_run_id, prior)
        if missing is not None:
            return {"error": missing}
        # The answered route, applied to the spec the run PERSISTED — one node,
        # routing fields only. Everything after this point is an ordinary resume
        # (budget clamp, cache preview, replay), which is the whole point.
        reroute: str | None = None
        rerouted: dict[str, Any] | None = None
        # The same move, as the typed ledger fact (#64) — held until the stretch
        # that will run on the new route actually exists, so the trail never
        # records a re-route that a later refusal (a spent budget, a lost fence)
        # meant nothing ever ran under.
        route_move: tuple[dict[str, Any], dict[str, Any]] | None = None
        if answered.node_id is not None:
            adapted = apply_route_answer(spec_dict, answered.node_id, answered.route or {})
            if isinstance(adapted, str):
                return {"error": adapted}
            spec_dict = adapted
            dead_route = (prior.route_fault or {}) if prior else {}
            route = answered.route or {}
            reroute = reroute_fault(answered.node_id, dead_route, route)
            # ECHOED in the launch reply, not just written to the faults: the
            # caller who sent two words gets back the route the harness actually
            # applied, so "did it take the model I meant, or the provider I
            # forgot to change?" is answered at the acceptance rather than
            # inferred later out of fault prose (or, worse, out of a second
            # pause). The same three facts the fault carries.
            rerouted = {
                "node_id": answered.node_id,
                "from": route_label(dead_route.get("provider"), dead_route.get("model")),
                "to": route_label(
                    route.get("provider", dead_route.get("provider")),
                    route.get("model", dead_route.get("model")),
                ),
                # Only when the answer moved it: the echo says what the answer
                # DID, and a null effort on every other reroute would read as a
                # knob that was reset rather than one nobody touched.
                **({"effort": route["effort"]} if "effort" in route else {}),
            }
            route_move = route_change(dead_route, route)
        answers, unanswered = resolve_checkpoint_answers(
            resume_run_id, checkpoint_answers, explicit_spec, prior, spec_dict
        )
        if unanswered is not None:
            return {"error": unanswered}
        # Taint is ORed, never overwritten: the tool passes the CURRENT session's
        # taint, so a clean session resuming a run that was launched tainted
        # would otherwise silently hand its leaves back their fs and egress.
        tainted = bool(tainted or (prior.tainted if prior is not None else False))
        parsed = validate_spec(spec_dict, supported_types=SUPPORTED_NODE_TYPES)
        if isinstance(parsed, ValidationError):
            # Só uma spec EXPLÍCITA pode ser autoria da agência atual (SUP-05):
            # num resume sem spec, o validate_spec rodou sobre a spec PERSISTIDA
            # do run — escrevida por outro turno/outro autor — e atribuí-la à
            # agência agora seria atribuição falsa. Fail-closed: sem spec
            # explícita, nada registra, mesmo com agency_authored=True.
            if agency_authored and explicit_spec:
                # High-confidence authoring fault (SUP-05): recorded BEFORE the
                # return, so the candidate exists the instant the author sees the
                # didactic error. Never promoted past 'candidate' here, and a
                # store failure is logged and swallowed — the return the author
                # reads is the didactic error, never this side-channel.
                self._record_spec_candidate(parsed)
            return {"error": parsed.message, "invalid_spec": True}
        invalid = validate_token_budget(token_budget)
        if invalid is not None:
            return {"error": invalid}
        spec_warnings = lint_warnings(parsed)  # #49: warns, never blocks/nests
        if agency_authored and explicit_spec and spec_warnings:
            # E1 part 2 (#50, coordinator decision 2026-09-05): a lint warning
            # carries the SAME structural evidence a rejected spec does
            # (mechanism="validation" + SIGNAL_SPEC_SHAPE + rule:<rule>) —
            # just weaker (the spec ran; see insight_producers.py). Same
            # provenance gate as `_record_spec_candidate` right above: a
            # resume replaying the run's PERSISTED spec (explicit_spec=False)
            # is never this turn's authoring, no matter what the flag says.
            record_lint_warning_candidates(self._db.insights, spec_warnings)

        run_id = resume_run_id or uuid4().hex
        # A `running` line with nobody holding its lease means the process that
        # owned this run died. Recover it — never block on it (the run would be
        # stranded forever) — and say so in the rollup: the cells it had in
        # flight really were lost.
        live_here = self._get(run_id)
        # Whether the lease was ALREADY dead is a fact about the moment BEFORE
        # the acquire: once we hold it, the lease is ours and the question no
        # longer has an answer. Read once here; the recovery verdict that uses
        # it is decided after the acquire (below), off the post-acquire line.
        lease_free = live_here is None and self._store.lease_expiry(run_id) is None
        if pause_prior is not None:
            acquisition = (self._store.acquire_paused(run_id, pause_prior)
                           if resume_token is None else self._store.acquire_paused(
                               run_id, pause_prior, resume_token=resume_token,
                           ))
            if not acquisition.accepted:
                return self._write_refusal(run_id, acquisition)
            leased = True
        else:
            acquisition = self._store.acquire_result(run_id)
            leased = acquisition.accepted
            if not leased and acquisition.kind != "busy":
                return self._write_refusal(run_id, acquisition)
        # What every write of this stretch presents (issue #12); None only on
        # the paths that never took the lease, which write like they always did.
        fence = self._store.fence_of(run_id) if leased else None
        if leased and not isinstance(fence, int):  # pragma: no cover - defensive
            # Unreachable in practice (we acquired one line ago, and only 1024
            # further acquisitions could evict it), but the value goes on to be a
            # SQLite bind param on pool workers: normalise to the fail-CLOSED
            # reading — no launch under a fence we cannot present — rather than
            # to None, which would silently run the whole stretch unfenced.
            # We cannot present the original fence; do not release a lease
            # that may now belong to another acquisition. Its TTL remains.
            return {
                "error": f"workflow run {run_id!r} lost its ownership fence before it "
                "started; nothing ran — launch it again"
            }
        if not leased and not (live_here is not None and _is_live(live_here)):
            # Somebody else is inside this run right now. Two engines on one node
            # cache and one working root is the corruption this lease exists for.
            # A run that is live HERE falls through instead: the registry guard
            # below owns that case, and its message names the status.
            return {
                "error": busy_error(run_id, self._store.lease_expiry(run_id), self._store.now())
            }
        core: OrchestrationCore | None = None
        state: RunState | None = None
        launch_write: StateWrite | None = None
        submission_attempted = False
        try:
            orphaned = False
            # The recovery facts (SUP-05) are re-read UNDER ownership, never from
            # the pre-acquire snapshot: between that read and the lease we now hold,
            # the prior owner's last fenced write may have landed (status, owner,
            # audit marker all moved) or a newer owner may have taken the run over.
            # Deciding "orphaned" and addressing the notice off the stale snapshot
            # would announce a recovery of a run that no longer needs one — or tell
            # the wrong session. The pre-acquire ``prior`` stays only for launch
            # resolution (spec/args/answers/taint), which happened before the fence.
            if resume_run_id:
                prior = self._store.load(resume_run_id)
                orphaned = (
                    prior is not None and live_here is None and lease_free and prior.status == "running"
                )
                audit_unclosed = bool(prior is not None and prior.audit_segment_id)
            # What the run has ALREADY spent — read UNDER ownership, never before it.
            # Read ahead of the acquire, this seeded the new stretch from a tally the
            # previous owner was still finishing, and the run then ran under a
            # ceiling it had in fact already spent.
            spent_in, spent_out = seed_spend(self._db, run_id) if resume_run_id else (0, 0)
            # ...and how many leaves produced that spend, so the resumed run's
            # pre-spawn gate keeps asking "can what is left pay for one more?" with
            # THIS run's measured rate (issue #71), not a static constant.
            spent_charges = seed_charges(self._db, run_id) if resume_run_id else 0
            applied_budget = self._effective_budget(run_id, token_budget, resume_run_id)
            effective_budget = applied_budget.total
            refusal = refuse_spent_budget(
                run_id, effective_budget, spent_in + spent_out, operator_cap=self._operator_cap
            )
            if refusal is not None:
                if leased:
                    # Never sit on a lease for a run we are not going to start: it
                    # would lock every later resume out until the TTL ran down.
                    self._store.release(run_id, fence=fence)
                return refusal
            # One scratch directory per ACQUISITION, not per run (issue #12).
            # The fence protects SQLite; the filesystem has no such guard, and a
            # stale owner's leaves happily kept writing into the shared
            # ``runs/<run_id>/work`` that the recovering owner was reading as its
            # own. Named by the fence, so the new owner is born in a clean
            # directory and the obsolete one writes harmlessly into its own.
            # Cost, deliberate: scratch is NOT carried across stretches — a
            # resume starts with an empty working root. Nothing depends on it
            # today (the path is a sandbox boundary; no prompt, no node and no
            # engine code ever hands a leaf the path), and the alternative —
            # copying a lost stretch's half-written files forward — is exactly
            # the contamination this closes.
            run_tree = self._home / "runs" / run_id
            working_root = run_tree / (
                f"work-{fence}" if fence is not None else "work"
            )
            working_root.mkdir(parents=True, exist_ok=True)
            # What the HARNESS may stat/hash when a cell declares an artifact
            # manifest (#45 E4) — deliberately the RUN's whole tree, not this
            # acquisition's ``work-{fence}``: a cell stored under work-3 has to
            # stay verifiable when the resume owns work-4, and a scope that
            # narrowed with every acquisition would answer ``unverifiable`` for
            # every scratch artifact on the first resume. Plus the operator's
            # ``fs_allow`` roots, ro and rw alike (measuring only ever reads).
            artifact_scope = ArtifactScope.of(run_tree, self._policy)
            leaf_factory = make_sandboxed_leaf_factory(
                base_factory=self._base_factory,
                working_root=working_root,
                policy=self._policy,
                tainted=tainted,
            )
            core = OrchestrationCore(
                self._db,
                leaf_factory,
                max_concurrent=self._run_concurrency,
                # None restores the byte-identical no-audit path: the core
                # returns from _observe before building a safe frame at all.
                event_sink=(
                    (
                        lambda sub_id, context, frame: self._audit.record_gateway(
                            frame, context, sub_id=sub_id
                        )
                    )
                    if self._audit_enabled
                    else None
                ),
            )
            engine = WorkflowEngine(
                core,
                run_id=run_id,
                budget=Budget(
                    token_budget=effective_budget,
                    tokens_in=spent_in,
                    tokens_out=spent_out,
                    charges=spent_charges,
                ),
                # A cached cell also refreshes the run's lease (WF-29) — the cheap
                # top-up on top of the timer heartbeat, which is what keeps a run
                # stuck in ONE long node from lapsing the lease it still holds.
                cache=NodeCache(
                    self._db,
                    run_id,
                    on_write=lambda: self._store.renew(run_id, fence=fence),
                    fence=fence,
                    # Under WHAT this stretch would run a leaf (#75): the
                    # operator's effective policy and the harness version.
                    # Stamped on every leaf cell and compared on every hit, so a
                    # resume under a narrower policy replays the work already
                    # paid for and SAYS SO instead of replaying in silence.
                    stamp=CellStamp.current(self._policy),
                ),
                loader=lambda ref: library.get_template(self._home, ref),  # `workflow` node refs
                client_pool=self._client_pool,  # cross-provider leaves
                tiers=self._tiers,  # portable model choice (WF-5)
                # ...and what this session itself runs on, so a node with no
                # `tier:` has an ANCHOR instead of a guess (#85).
                default_route=self._default_route,
                routes=self._routes,  # pre-authorized route fallbacks (#63)
                # ...and the DURABLE brake that bounds them. Bound to this run
                # here, so the engine spends an allowance it cannot widen and a
                # fresh process reads the same count the last one wrote.
                route_fallback_try=partial(self._db.route_fallback_try, run_id),
                checkpoint_answers=answers,  # human gates already answered (WF-10)
                # Live view + durable progress ride the same event (WF-30).
                on_event=lambda kind, payload: self._run_event(run_id, kind, payload, fence=fence),
                on_audit=self._audit.record if self._audit_enabled else None,
                artifact_scope=artifact_scope,
            )
            state = RunState(
                run_id=run_id,
                seq=next(self._seq),
                name=_spec_name(spec_dict),
                owner=owner,
                core=core,
                engine=engine,
                spec_dict=spec_dict,
                args=run_args,
                tainted=tainted,
                fence=fence,
                revision=prior.revision if prior is not None else 0,
                # What earlier stretches already spent on the report meters, so
                # a resume's ledger continues instead of restarting at zero.
                prior_split=seed_split(self._db, run_id) if resume_run_id else Usage(),
                # Only when the trail is ON: the marker exists to say "the
                # closing `segment.completed` append never landed", and with the
                # audit off no append ever happens — so a persisted marker could
                # never be cleared, and the next resume made after the operator
                # turns auditing back on would be born declaring a gap that never
                # occurred. `off` must leave no trace for a later reader.
                audit_segment_id=engine.segment_id if self._audit_enabled else None,
            )
            with self._lock:
                # Check + register atomically: a resume onto a run that hasn't stopped
                # would clobber its registry entry and share its cache/working_root
                # with a second engine (the first run then writes into a run nobody
                # tracks). Refuse instead.
                existing = self._runs.get(run_id)
                clash = existing if existing is not None and _is_live(existing) else None
                if clash is None:
                    if prior is not None and resume_run_id:
                        # Carry the auto-resume budget across the resume: a fresh
                        # RunState starting at 0 would retry a dead quota forever.
                        state.attempts = prior.attempts + 1
                        # ...and the faults it already collected, for the same
                        # reason the token tally is carried: the run is one run.
                        # Both come from the prior VIEW, so a resume in a fresh
                        # process carries them just as far as one in this process.
                        state.prior_faults = list(prior.prior_faults)
                        state.prior_degraded = prior.prior_degraded
                        state.prior_recovered = list(prior.prior_recovered)
                        state.prior_rerouted = list(prior.prior_rerouted)
                        state.prior_substitutions = list(prior.prior_substitutions)
                        state.prior_advisory = list(prior.prior_advisory)
                        state.prior_artifact_advisories = prior.prior_artifact_advisories
                        state.prior_replay_divergences = prior.prior_replay_divergences
                        state.prior_leaf_respawns = prior.prior_leaf_respawns
                        state.prior_overrun = prior.prior_overrun
                        state.prior_uncertain = prior.prior_uncertain
                        state.prior_cells_replayed = prior.prior_cells_replayed
                        state.prior_saved = prior.prior_saved
                    if orphaned:
                        state.prior_faults = state.prior_faults + [
                            f"{run_id}: {RECOVERED_FAULT} — the process running it stopped "
                            "before it finished; completed cells replayed, work in flight was lost"
                        ]
                    if reroute is not None:
                        # WHO moved this route, and from where to where (#43).
                        # In ``prior_faults`` like the recovery fault above, so
                        # it is reported for the whole run and discounted from
                        # the verdict: the re-route is the remedy, not a lesson
                        # about the spec, and a stretch that runs clean on the
                        # new route must still be able to seal ``complete``.
                        state.prior_faults = state.prior_faults + [reroute]
                        state.prior_rerouted = state.prior_rerouted + [
                            str(answered.node_id)
                        ]
                    self._runs[run_id] = state
            if clash is not None:
                if leased:
                    self._store.release(run_id, fence=fence)  # never sit on a lease we did not use
                core.shutdown()  # nothing was registered — don't leak this core's pool
                return {
                    "error": f"workflow run {run_id!r} has not finished (status: {clash.status}); "
                    "wait for it (workflow_status) or cancel it before resuming"
                }
            # Ownership first: a refused resume must never overwrite the spend
            # ledger of the live run it just lost the race to.
            launch_write = self._save_state(state, mode="launch")
            if launch_write.kind != "written":  # first write of this acquisition
                # Fenced out before the run ever started (issue #12): a newer
                # owner took the run between our acquire and this first write.
                # Nothing may speak for the run now — no recovery notice, no
                # plan, no audit gap, no engine — and nothing we took may leak:
                # the registry entry is dropped (nothing will ever finish it),
                # the core's threads stop, and the lease goes back.
                with self._lock:
                    if self._runs.get(run_id) is state:
                        del self._runs[run_id]
                core.shutdown()
                self._store.release(run_id, fence=fence)
                return self._write_refusal(run_id, launch_write)

            if not self._persist_spend(state):  # the ledger a resume seeds from
                # Fenced out AFTER the line was accepted (issue #12): a newer
                # owner took the run between the line write and this ledger
                # write. Same rule as the refused line above — nothing may
                # speak for the run now (no recovery notice, no plan, no audit
                # gap, no engine) and nothing we took may leak.
                with self._lock:
                    if self._runs.get(run_id) is state:
                        del self._runs[run_id]
                core.shutdown()
                self._store.release(run_id, fence=fence)
                return {
                    "error": f"workflow run {run_id!r} lost its ownership fence before it "
                    "started; nothing ran — launch it again"
                }
            def announce_run() -> None:
                # SUP-05 recovery notice: an orphaned `running` run is now MINE.
                # Fired only after the lease/fence acquisition is validated and the
                # fenced state was persisted (this point is past both), and only on
                # the WINNING path — a refused resume (busy, clash, refusal) returns
                # above and never reaches here. The fact goes to the PRIOR owner,
                # never to the new one; the store's own dedup makes a repeated
                # recovery of the same run one row (the text is a function of the
                # run_id alone), and a store failure is logged and swallowed — the
                # resume itself must never depend on telemetry surviving.
                if orphaned and prior is not None:
                    self._publish_recovery_notice(run_id, prior.owner)
                # The accepted Future is already published. This asynchronous
                # preamble still draws the DAG BEFORE the first leaf spawns;
                # start returning guarantees acceptance, not callback delivery.
                self._events.emit(
                    run_id, PLAN,
                    plan_payload(run_id, parsed, name=state.name,
                                 token_budget=effective_budget, warnings=spec_warnings),
                )
                if orphaned or audit_unclosed:
                    # Neither a dead process nor a lost terminal append can report how
                    # many queued observations died with it: declare the boundary
                    # instead of inventing a count. The REASON discriminates, though —
                    # §11.2 reserves `process_crash` for a process that really died (a
                    # `running` line whose lease nobody holds). An unclosed segment on
                    # its own only proves the closing append never landed, which also
                    # happens to a live process whose sink hiccupped (SQLITE_BUSY at
                    # the audit connection's 50ms timeout, queue overflow). The cause
                    # is not observable there, so the honest label is `unavailable`.
                    self._audit.record_gap(
                        run_id, "process_crash" if orphaned else "unavailable", count=None
                    )
                # WHICH spec this stretch ran under (#44 épico 3): metadata only,
                # never prompt or content. The run stores ONE spec — a pivot
                # overwrites it — so without this stamp nothing can say afterwards
                # that a later stretch ran a DIFFERENT spec from the one that wrote
                # the cells, and an invalidation reads as a bug in the cache.
                stretch_spec_name, stretch_spec_version = spec_identity(parsed)
                engine.audit_segment(
                    "segment.started",
                    {
                        "resume": bool(resume_run_id),
                        # Process liveness only; the gap above carries the segment's.
                        "recovered_process": orphaned,
                        "spec_name": stretch_spec_name,
                        "spec_version": stretch_spec_version,
                    },
                )
                if route_move is not None:
                    # The re-route as a TYPED event (#64), inside the stretch that
                    # runs on the new route and right after the boundary that opens
                    # it. Until this existed the ledger showed the old route in one
                    # ``leaf.started`` and the new one in another, and the sentence
                    # naming the MOVE lived only in ``faults_total`` prose — which
                    # the metadata-only trail redacts by contract (dogfood T10, (e)).
                    # The CHANNEL, never an author: see ``reroute_fault``.
                    engine.audit_reroute(
                        str(answered.node_id), *route_move,
                        channel=CHANNEL_CHECKPOINT_ANSWERS,
                    )
            # What THIS resume will replay and what it will re-pay (#44 épico 2).
            # Read-only, zero LLM, and only on a resume: a fresh run has no cache
            # to diff against, so its acceptance stays byte-identical to before.
            # Computed under ownership (like seed_spend) and BEFORE the submit, so
            # it never races the run thread's own writes — and in its own
            # try/except, because a preview is telemetry: a bug here must never
            # abandon a launch that is otherwise good.
            preview: dict[str, Any] | None = None
            if resume_run_id:
                try:
                    preview = preview_resume(
                        self._db, run_id, parsed, run_args,
                        tiers=self._tiers, checkpoint_answers=answers,
                        artifact_scope=artifact_scope,
                        # The SAME resolution the engine's ``load_workflow``
                        # uses (#61): a nested node's children are only
                        # previewable under the sub-template's own identity, and
                        # the only way to know that identity is to load it.
                        loader=lambda ref: library.get_template(self._home, ref),
                    )
                except Exception:
                    logger.exception("workflow: cache preview failed for run %s", run_id)
            # Pass the raw spec_dict too: it's what record_outcome saves as a template.
            submission_attempted = True
            self._launches.submit(
                submission, self._pool, state, self._run,
                parsed, spec_dict, run_args, engine, state, before_run=announce_run,
            )
            accepted: dict[str, Any] = {"run_id": run_id, "status": "started"}
            if rerouted is not None:
                accepted["rerouted"] = rerouted
            if preview is not None:
                accepted["cache_preview"] = preview
            # Only when a cap is in force: a process without one answers exactly
            # what it answered before this existed (#47).
            ceiling = applied_budget.as_dict()
            if ceiling is not None:
                accepted["token_budget"] = ceiling
            return with_warnings(accepted, spec_warnings)
        except BaseException:
            if submission.accepted:
                raise  # the tracked run owns cleanup once accepted, even if its caller is interrupted
            try:
                self._abandon_launch(
                    run_id, state, core, leased, fence, prior=prior, launch_write=launch_write,
                    cause="submission_refused" if submission_attempted else "preparation_failed",
                )
            except BaseException:
                logger.exception("workflow: launch cleanup failed for %s", run_id)
            raise

    def _record_spec_candidate(self, error: ValidationError) -> None:
        """Record ONE durable candidate from a rejected spec (SUP-05).

        kind='candidate' — never 'insight': the store's learnable gate recomputes
        responsibility from (mechanism, signals, confidence), so a summary alone
        cannot smuggle an authoring claim past it. Summary stays didactic and
        bounded (the store clips again at the schema boundary). Never raises:
        the caller's didactic return must not depend on telemetry surviving.

        Wave 9 / E1 (#50): the insight store now fingerprints STRUCTURE, never
        prose, so ``signals`` must carry the stable cause — ``issue.rule`` is
        a closed vocabulary (schema.py names it as a literal at every call
        site, never interpolates free text into it), unlike ``error.message``
        which bakes in the node id and a rendered example. Without this, every
        rejected spec of every kind would fingerprint identically (same
        mechanism, same lone SIGNAL_SPEC_SHAPE) and collapse into one
        recurrence-counted row regardless of what actually went wrong."""
        try:
            rule_signals = tuple(sorted({f"rule:{issue.rule}" for issue in error.issues}))
            self._db.insights.record(
                kind="candidate",
                status="invalid_spec",
                mechanism="validation",
                signals=(SIGNAL_SPEC_SHAPE, *rule_signals),
                confidence=1.0,
                summary="authored workflow spec rejected by validate_spec: "
                f"{error.message}",
            )
        except Exception:
            logger.warning(
                "workflow: could not record spec-validation candidate", exc_info=True
            )

    def _record_substitution_candidates(self, state: RunState) -> None:
        """Record ONE durable candidate per model this run had to substitute
        (#85, W9-E8 — the owner's decision on #54).

        The owner ruled that a nonexistent model chosen by the AUTHOR is
        ``agency``: the harness has a catalog, so naming a slug that is not in it
        is an authoring mistake and not an accident of the environment. The
        store recomputes that verdict from the evidence — never from this
        caller's word — and ``mechanism="validation"`` + ``SIGNAL_SPEC_SHAPE``
        at full confidence is exactly the branch
        ``failure_taxonomy._resolve`` maps to ``AGENCY``.

        Recorded even when the run is not certifiable, unlike the template: the
        lesson is about the SLUG, and a run that also failed for some other
        reason did not stop the slug from being wrong. ``kind='candidate'`` for
        the reason ``_record_spec_candidate`` gives — a summary alone must never
        smuggle an authoring claim past the gate. Never raises: telemetry is not
        allowed to cost a finished run.
        """
        result = state.result
        substitutions = getattr(result, "model_substitutions", None) or ()
        for entry in substitutions:
            try:
                self._db.insights.record(
                    kind="candidate",
                    status="model_substituted",
                    mechanism="validation",
                    signals=model_substitution_signals(),
                    confidence=1.0,
                    summary=(
                        "authored workflow node named a model the provider does "
                        f"not have: {entry.get('from')!r} -> substituted by "
                        f"{entry.get('to')!r} from the operator's tier map"
                    ),
                )
            except Exception:
                logger.warning(
                    "workflow: could not record model-substitution candidate",
                    exc_info=True,
                )

    def _publish_recovery_notice(self, run_id: str, prior_owner: str | None) -> None:
        """Tell the run's PRIOR owner that its process died mid-run (SUP-05).

        The durable half of the recovery fact ``RECOVERED_FAULT`` already puts
        in the rollup: the process running this run stopped before it finished,
        completed cells were replayed from the cache, and the work it had in
        flight was lost (this stretch is re-doing it). Cross-process, it is the
        prior owner's SESSION that learns this — via ``db.notices``, claimed on
        that session's next turn — because the owner may be a different
        process, or gone when the run is recovered.

        Who is told: ``prior_owner`` ONLY — the session that LOST the run, never
        the one recovering it (the recovering session is the one acting; the
        rollup already tells it everything). Ownerless (None/blank) publishes
        nothing, and the store would refuse it anyway — the guard is here so
        the attempt is never made. The text is a function of the run_id alone
        (no timestamps, no counts), so the store's fingerprint dedup folds a
        repeated recovery of the SAME run into one row — and two runs stay two
        facts. Never raises: a broken notice store (sqlite busy, disk full)
        costs the notice, never the recovery itself."""

        owner = prior_owner.strip() if isinstance(prior_owner, str) else ""
        if not owner:
            return  # ownerless: no session to tell, nothing published
        text = (
            f"workflow run {run_id} recovered: the process running it stopped "
            "before it finished; completed cells were replayed from the cache, "
            "work in flight was lost and is being redone"
        )
        try:
            self._db.notices.publish(owner, text)
        except Exception:
            logger.warning(
                "workflow: could not publish the recovery notice for run %s",
                run_id,
                exc_info=True,
            )

    def _abandon_launch(
        self,
        run_id: str,
        state: RunState | None,
        core: OrchestrationCore | None,
        leased: bool,
        fence: int | None,
        *, prior: DurableRun | None = None, launch_write: StateWrite | None = None,
        cause: str = "preparation_failed",
    ) -> None:
        """Undo only this unaccepted preparation, without any coordination mutex.

        The immutable launch receipt protects against cancellation/succession.
        Its segment was prepared but never emitted: restoring the prior marker
        in this CAS does not fabricate an audit segment.completed event.
        """
        if state is not None:
            try:
                if launch_write is not None and launch_write.kind == "written":
                    snapshot = prior if prior is not None else self._state_snapshot(state)
                    rejected = replace(
                        snapshot,
                        status=snapshot.status if prior and prior.status != "running" else "failed",
                        audit_segment_id=prior.audit_segment_id if prior is not None else None,
                        launch_failure=cause,
                    )
                    receipt = self._store.save_snapshot(
                        rejected, fence=fence, mode="refuse_launch",
                        expected_revision=launch_write.revision,
                    )
                    if receipt.kind != "written":
                        logger.warning("workflow: refused launch metadata for %s was not restored (%s)",
                                       run_id, receipt.kind)
            except BaseException:
                logger.exception("workflow: could not restore refused launch metadata for %s", run_id)
            with self._lock:
                if self._runs.get(run_id) is state:
                    del self._runs[run_id]
        if core is not None:
            try:
                core.shutdown()
            except BaseException:
                logger.exception("workflow: could not shut down refused launch core for %s", run_id)
        if leased:
            self._store.release(run_id, fence=fence)

    def _run(
        self, spec: Any, spec_dict: dict, args: dict, engine: WorkflowEngine, state: RunState,
        *, submission: Submission | None = None, before_run: Callable[[], None] | None = None,
    ) -> None:
        if submission is not None and not self._launches.accepted(submission):
            return  # even the finally has effects; a refused callable must never enter it
        # SQLite seals the functional decision before draining. Effects require
        # that exact decision plus a final accepted snapshot under the same fence.
        record: Callable[[], None] | None = None
        decision: StateWrite | None = None
        try:
            if before_run is not None:
                before_run()
            result = engine.run(spec, args)
            with state.state_lock:
                state.result = result
            decision = self._finish_state(state, result.status, result)
            if decision.kind == "written":
                if result.status == "paused":
                    # Same reasoning as cancelled, different cause: the PROVIDER
                    # stopped this run, so neither certifying nor blaming the spec
                    # is a lesson worth learning. Schedule the retry instead.
                    self._on_paused(state, result)
                else:
                    # Self-improvement feedback (§12): outcome -> insight prior /
                    # template. Skip a cancelled run — the user stopped it; the
                    # shape didn't fail.
                    record = partial(
                        library.record_outcome,
                        self._home,
                        # The spec with THIS stretch's envelope re-routes folded
                        # in (#63). ``spec_dict`` is this method's own parameter,
                        # bound by value before the run: ``_persist_state``
                        # rebinds ``state.spec_dict`` and cannot reach it. A node
                        # that DECLARED the route that then died would otherwise
                        # be certified naming that dead route, on the strength of
                        # a run that only finished because the envelope moved it
                        # — a template published pointing at a route already known
                        # to be gone, where before this slice the run had paused.
                        apply_reroutes(spec_dict, result.reroutes),
                        result,
                        # What the WHOLE run cost, so a resumed run does not
                        # teach the library it was cheap (WF-23).
                        tokens_total=spent_total(self._db, state.run_id, engine_spent(state.engine)),
                        # ...and what the whole run FAULTED on, for the same
                        # reason: a prior that quotes only the last stretch
                        # blames the wrong thing (WF-25/26).
                        faults_total=state.prior_faults + list(result.faults),
                        # A stretch that really failed is not erased by a last
                        # stretch that happened to run clean.
                        prior_degraded=state.prior_degraded,
                        # Where this template was certified (E4, #51) — the run
                        # a reader would find to audit it.
                        run_id=state.run_id,
                        # ...and what surviving the provider actually COST, so a
                        # certified template can say so (Q2, #43).
                        leaf_respawns=run_leaf_respawns(state),
                        # ...and WHICH nodes only got there on a route somebody
                        # supplied mid-run (#43): the certified template is the
                        # ADAPTED spec, so without this it would publish the
                        # emergency route as if the author had chosen it.
                        # The WHOLE run's, this stretch included (#63): the
                        # envelope re-routes without ever touching
                        # ``state.prior_rerouted`` (only a resume's answer does),
                        # so reading that field alone would certify an
                        # envelope-rescued template as one nobody re-routed.
                        rerouted_nodes=carried_rerouted(state.prior_rerouted, result),
                        # ...and how many claims the harness had to correct, so
                        # a certified template says so instead of reading as a
                        # run nobody had to advise (#45). Counted at ITS OWN
                        # door, never derived from the advisory total: that list
                        # has more than one producer (#75), and any arithmetic
                        # over it would go silently wrong the day a third one
                        # lands. The budget overrun (#71) is the third producer
                        # that proves the point — it is advisory and is NOT an
                        # artifact claim, and under this counter it is excluded
                        # BY CONSTRUCTION instead of by matching its prose.
                        artifact_divergences=run_artifact_advisories(state),
                        # ...and how many cells replayed under another operator
                        # policy or another harness version (#75) — stamped
                        # beside it so the next author reads "this works, and N
                        # of its cells were replayed under something else"
                        # rather than inferring a run executed as written.
                        replay_divergences=run_replay_divergences(state),
                        # ...and how far past a ceiling the run ever went — the
                        # HIGH-WATER MARK, not the live arithmetic (#71). The
                        # canonical remedy RAISES the ceiling, so a run that
                        # overspent, paused, was renewed and finished reads 0
                        # live: certifying that number would publish a template
                        # as never having overrun while the advisory fault that
                        # says otherwise sits in the same run's faults_total.
                        budget_overrun=run_overrun(state),
                        # ...and which models the harness had to replace because
                        # the authored slug does not exist anywhere (#85). The
                        # certified spec still CARRIES the substitution (it is
                        # folded through ``apply_reroutes`` above), so without
                        # this stamp the template would read as one the author
                        # got right the first time.
                        # The WHOLE run's, this stretch included (#85), for
                        # the reason ``rerouted_nodes`` reads the carried list:
                        # the stretch that certifies is usually not the stretch
                        # that substituted, and a template stamped from this
                        # stretch alone would publish a spec whose one measured
                        # run reads as one the author got right the first time.
                        model_substitutions=carried_substitutions(
                            state.prior_substitutions, result
                        ),
                    )
        except Exception as exc:  # never let a run thread die silently
            with state.state_lock:
                state.error = f"{type(exc).__name__}: {exc}"
            if decision is None:
                decision = self._finish_state(state, "failed")
        finally:
            # Achado 4 do review SUP-05: ao contrário de RunStore.save (que
            # nunca levanta), o write do ledger pode estourar (OperationalError
            # sob contenção) — e como é a PRIMEIRA sentença deste finally, uma
            # exceção aqui pularia core.shutdown, o fechamento do segmento, a
            # linha terminal E o release da lease (run trancado até o TTL).
            # O ledger é importante; o epílogo é mais.
            try:
                self._persist_spend(state)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "workflow: spend ledger write failed for run %s at settle",
                    state.run_id,
                )
            # The core settles FIRST: a leaf still draining is still emitting
            # frames, and both the segment boundary below and the lease we hand
            # back are lies while one is in flight (cross-process, a resume that
            # took the freed lease would start a second engine over the same node
            # cache).
            if state.core is not None:
                state.core.shutdown()
            self._close_audit_segment(state, engine)
            # The line BEFORE the lease: a process that dies between the two
            # leaves a stale lease over correct state, never the reverse.
            #
            # The final snapshot confirms that this acquisition still owns the
            # line. It is insufficient alone: publishing also requires the exact
            # functional decision above. A refused finish followed by a successful
            # running/cancelled snapshot conveys no success publication right.
            owned = self._persist_state(state)
            self._store.release(state.run_id, fence=state.fence)
            with state.state_lock:
                settled = replace(state)
            if (owned and decision is not None and decision.kind == "written"
                    and settled.status == (decision.row or {}).get("status")):
                self._publish_outcome(settled, record)
            # DELIBERATELY ungated: this is the live view of THIS process's own
            # stretch, on the operator's own terminal, and a run that vanishes
            # with no last line is the black box the live view exists to close.
            # It publishes nothing and steers nobody.
            self._emit_done(settled)

    def _publish_outcome(self, state: RunState, record: Callable[[], None] | None) -> None:
        """Publish this accepted decision only while succession cannot overtake it.

        The dedicated DB/run guard spans the actual file and callback effects,
        unlike a stale precheck. No state mutex or SQLite transaction spans them.
        A delayed entry loses to a successor; an acquisition during publication
        gets busy, including a reentrant one from the callback itself.
        """
        if state.status == "cancelled":
            return
        try:
            with self._db.publication_guard(state.run_id) as access:
                if access != "acquired":
                    logger.warning("workflow: outcome publication refused for %s: %s",
                                   state.run_id, access)
                    return
                row = self._db.run_state_get(state.run_id)
                # The caller already requires THIS functional decision. Benign
                # same-state snapshots may advance revision; only cancellation
                # or a new acquisition can revoke this effect's authority.
                if row is None or (row["fence"], row["status"]) != (state.fence, state.status):
                    return
                self._record_substitution_candidates(state)
                if record is not None:
                    try:
                        record()
                    except Exception:  # feedback never suppresses a valid notice
                        logger.exception("workflow: could not record outcome of %s", state.run_id)
                self._notify_done(state)
        except Exception:  # storage failure cannot authorize publication
            logger.exception(
                "workflow: could not publish the outcome of run %s", state.run_id
            )

    def _close_audit_segment(self, state: RunState, engine: WorkflowEngine) -> None:
        """Close the audit segment BEFORE the terminal line and the lease go out.

        The marker on that line (``audit_segment_id``) is the discriminator for
        "the closing ``segment.completed`` append never landed" (§11.2).
        Publishing the line — and freeing the lease for the next resume — while
        the append is merely QUEUED turns a race into a PERMANENT, false
        ``audit.gap`` on a run in which nothing was ever lost.

        So: emit, wait a bounded moment for the sink to take it, and drop the
        marker only once the ledger really cleared it.  A sink that will not
        take it keeps the marker, which is the honest reading — and a resume
        arriving inside this window finds the lease still held and is told the
        run is busy, never handed a fabricated gap.
        """
        engine.audit_segment("segment.completed", {"status": state.status})
        if not self._audit_enabled:
            return  # off pays neither the flush poll nor the confirming read
        if not self._audit.flush(timeout=AUDIT_CLOSE_TIMEOUT):
            return
        durable = self._store.load(state.run_id)
        if durable is not None and durable.audit_segment_id is None:
            state.audit_segment_id = None

    def _run_event(
        self, run_id: str, kind: str, payload: dict, *, fence: int | None = None
    ) -> None:
        """One live event from a run's engine: to the sink, and to the disk.

        The limiter's verdict decides BOTH — an ``items`` burst too fast to read
        is also too fast to be worth a row rewrite, and the width and the finish
        are never dropped, so the line never ends a fan-out short."""
        state = self._get(run_id)
        if state is None or (fence is not None and state.fence != fence):
            return
        if not self._events.emit(run_id, kind, payload):
            return
        if kind not in (NODE, ITEMS):
            return
        self._persist_state(state)

    def _emit_done(self, state: RunState) -> None:
        """The run stopped. Unlike ``on_run_done`` this fires for a CANCELLED run
        too: that notifier lands in an agent's turn (where telling it what it
        just did is noise), while this is the operator's terminal, where a run
        that vanishes with no last line is the black box we are closing."""
        progress = state.engine.progress_snapshot() if state.engine is not None else None
        self._events.emit(
            state.run_id,
            DONE,
            {
                "name": state.name,
                "status": state.status,
                "done": progress["done"] if progress else 0,
                "total": progress["total"] if progress else 0,
                "tokens": engine_spent(state.engine),
                **({"error": state.error} if state.error else {}),
            },
        )

    def _notify_done(self, state: RunState) -> None:
        """Tell whoever is listening that this run stopped for good (M6)."""
        notify_done(
            self._on_run_done,
            owner=state.owner,
            run_id=state.run_id,
            status=state.status,
            name=state.name,
            spent=engine_spent(state.engine),
        )

    @staticmethod
    def _resume_token(row: Any) -> ResumeToken:
        return ResumeToken(row.run_id, row.fence, row.status, row.pause_reason,
                           row.resume_at, row.attempts)

    def _on_paused(self, state: RunState, result: RunResult) -> None:
        """Retain the accepted pause; completion grants readiness, never authority."""
        with state.state_lock:
            if (state.status != "paused" or state.pause_reason != QUOTA_EXHAUSTED
                    or state.resume_at is None):
                return
            token, future = self._resume_token(state), state.future
        scheduler = self._autoresume
        plan = scheduler.prepare(
            state.run_id, attempts=token.attempts, deadline=token.resume_at,
            token=token, resume=lambda: self._resume_expected(token),
        )
        if plan is not None and future is not None:
            # add_done_callback executes inline for an already completed Future.
            # #138 publishes the accepted Future before this worker can enter.
            future.add_done_callback(lambda _done: self._arm_resume(scheduler, plan))

    def _arm_resume(self, scheduler: AutoResumeScheduler, plan: ResumePlan) -> bool:
        try:
            row = self._store.load(plan.run_id)
            with self._lock:
                state = self._runs.get(plan.run_id)  # even a fenced-out worker must finish
            if (row is None or self._resume_token(row) != plan.token
                    or (state is not None and _is_live(state))):
                scheduler.cancel(plan.run_id, plan=plan)
                return False
            if self._store.lease_expiry(plan.run_id) is not None:
                logger.warning("workflow: auto-resume of run %s not armed: ownership busy; "
                               "a later recovery scan or manual resume is required", plan.run_id)
                scheduler.cancel(plan.run_id, plan=plan)
                return False
            return scheduler.arm(plan)
        except Exception:
            scheduler.cancel(plan.run_id, plan=plan)
            logger.exception("workflow: could not validate auto-resume of run %s", plan.run_id)
            return False

    def _effective_budget(
        self, run_id: str, token_budget: int | None, resume_run_id: str | None
    ) -> AppliedBudget:
        """The ceiling this launch runs under: the one just asked for, else (on a
        resume) the one the run was already launched with — and either way never
        above the operator's cap (#47).

        The clamp is applied LAST, to the inherited value too: a run launched
        unbounded (or with a big ceiling) in a process with no cap must not
        resume unbounded inside a process that has one. The agent asking again
        with a larger number on the resume is clamped exactly the same way — the
        operator sits above the agent, and the agent is the only "human" a
        resume has."""
        asked, origin = token_budget, ORIGIN_SPEC
        if asked is None and resume_run_id:
            row = self._db.run_spend_get(run_id)
            asked = int(row["token_budget"]) if row and row["token_budget"] else None
            # Not this call's decision: the ledger's, which a previous stretch may
            # already have written CLAMPED. Calling it "spec" would credit the
            # agent with a ceiling it never authored on this launch.
            origin = ORIGIN_INHERITED
        return apply_operator_cap(asked, self._operator_cap, origin=origin)

    def _persist_spend(self, state: RunState) -> bool:
        """Write the run-level ledger so a later resume — in this process or a
        fresh one — starts from what the run has already spent.

        Fenced with the STRETCH's fence (issue #12), like ``_persist_state``:
        False means a newer owner has the run and the ledger write was refused
        — the launch caller reads it as "this stretch may no longer speak for
        this run". A run with no engine yet writes nothing and owns the run."""
        if state.engine is None:
            return True
        return persist_spend(
            self._db,
            state.run_id,
            state.engine.budget,
            state.engine.spend_split(),
            state.prior_split,
            fence=state.fence,
        )

    def _state_snapshot(self, state: RunState, **changes) -> DurableRun:
        # Only capture local fields under the lock. Engine snapshots take their
        # own locks and encoding/SQLite must not hold this decision/view lock.
        with state.state_lock:
            captured = replace(state, **changes)
        snapshot = view_of(captured)
        return replace(snapshot, spec=apply_reroutes(
            snapshot.spec, captured.result.reroutes if captured.result is not None else None
        ))

    def _apply_state(self, state: RunState, receipt: StateWrite) -> None:
        if receipt.row is None or receipt.fence != state.fence:
            return
        self._apply_durable(state, DurableRun.from_row(receipt.row))

    def _apply_durable(self, state: RunState, row: DurableRun) -> None:
        with state.state_lock:
            if row.revision < state.revision:
                return
            # Ownerless route-abort may append a carried fault while this
            # engine's late callbacks are still alive. Adopt only the missing
            # occurrences: copying the whole aggregate would count this
            # stretch's own result faults twice on the next snapshot.
            missing = Counter(row.prior_faults) - Counter(
                state.prior_faults + list(state.result.faults if state.result is not None else [])
            )
            extra = []
            for fault in row.prior_faults:
                if missing[fault] > 0:
                    extra.append(fault)
                    missing[fault] -= 1
            state.prior_faults = state.prior_faults + extra
            state.revision = row.revision
            state.status = row.status
            state.pause_reason, state.checkpoint = row.pause_reason, row.checkpoint
            state.route_fault, state.resume_at = row.route_fault, row.resume_at
            state.audit_segment_id = row.audit_segment_id
            state.spec_dict = row.spec

    def _finish_state(
        self, state: RunState, status: str, result: RunResult | None = None
    ) -> StateWrite:
        deadline = None
        if status == "paused" and result is not None and result.pause_reason == QUOTA_EXHAUSTED:
            deadline = self._autoresume.deadline(state.run_id, state.attempts, result.retry_after)
        snapshot = self._state_snapshot(
            state, status=status,
            pause_reason=result.pause_reason if result is not None else None,
            checkpoint=result.checkpoint if result is not None else None,
            route_fault=result.route_fault if result is not None else None,
            resume_at=deadline,
        )
        receipt = self._store.save_snapshot(
            snapshot, fence=state.fence, mode="finish", expected_revision=snapshot.revision
        )
        self._apply_state(state, receipt)
        if not receipt.accepted:
            with state.state_lock:
                state.error = self._write_error(receipt)
        return receipt

    @staticmethod
    def _write_error(receipt: StateWrite) -> str:
        if receipt.kind == "storage_error":
            return "could not persist the workflow result; inspect workflow_status before replay"
        return "workflow state or ownership changed; this acquisition cannot publish its result"

    def _save_state(self, state: RunState, *, mode: str = "snapshot") -> StateWrite:
        """CAS a captured run line; a rejected snapshot confers no publication right."""
        snapshot = self._state_snapshot(state)
        receipt = self._store.save_snapshot(
            snapshot, fence=state.fence, mode=mode, expected_revision=snapshot.revision
        )
        self._apply_state(state, receipt)
        return receipt

    def _persist_state(self, state: RunState) -> bool:
        return self._save_state(state).kind == "written"

    def _write_refusal(self, run_id: str, receipt: StateWrite) -> dict:
        status = (receipt.row or {}).get("status", "unknown")
        if receipt.kind == "missing":
            return {"error": f"no workflow run {run_id!r}"}
        if receipt.kind == "finished":
            return {"error": _finished_error(run_id, status)}
        if receipt.kind == "not_paused":
            return {"error": f"workflow run {run_id!r} is not paused (status: {status})"}
        if receipt.kind == "busy":
            return {"error": busy_error(run_id, self._store.lease_expiry(run_id), self._store.now())}
        if receipt.kind == "publication_busy":
            return {"error": f"workflow run {run_id!r} has publication or transition in progress; "
                    "retry after it finishes (there is no lease-based retry deadline)"}
        if receipt.kind == "conflict":
            return {"error": f"workflow run {run_id!r} changed state or ownership fence; "
                    "inspect workflow_status and retry"}
        return {"error": f"workflow run {run_id!r} could not persist the requested transition; inspect workflow_status and retry"}

    def rearm_pending_resumes(self) -> int:
        """Recover eligible durable intent once; busy ownership needs a later trigger.

        Saved deadlines remain exact. Legacy None uses a local, deduplicated
        backoff; no synthetic timestamp is written into the old pause.
        """
        armed = 0
        scheduler = self._autoresume
        for row in self._store.paused_on(QUOTA_EXHAUSTED, MAX_LISTED_RUNS):
            with self._lock:
                state = self._runs.get(row.run_id)
            if row.spec is None or (state is not None and _is_live(state)):
                continue
            if self._store.lease_expiry(row.run_id) is not None:
                logger.warning("workflow: auto-resume of run %s not armed: ownership busy; "
                               "a later recovery scan or manual resume is required", row.run_id)
                continue
            token = self._resume_token(row)
            plan = scheduler.prepare(
                row.run_id, attempts=row.attempts, deadline=row.resume_at, token=token,
                resume=lambda token=token: self._resume_expected(token),
            )
            if plan is not None:
                armed += self._arm_resume(scheduler, plan)
        return armed

    def _resume_expected(self, token: ResumeToken) -> dict:
        return self.resume(token.run_id, _token=token)

    def resume(self, run_id: str, *, _token: ResumeToken | None = None) -> dict:
        """Resume only the authoritative paused acquisition; explicit start is replay."""
        try:
            prior = self._store.load(run_id)
        except Exception:
            return self._write_refusal(run_id, StateWrite("storage_error"))
        if prior is None:
            return {"error": f"no workflow run {run_id!r}"}
        if _token is not None and self._resume_token(prior) != _token:
            return self._write_refusal(run_id, StateWrite("conflict"))
        state = self._get(run_id)
        if state is not None and state.fence == prior.fence:
            self._apply_durable(state, prior)
        if prior.status != "paused" or prior.spec is None:
            return {"error": f"workflow run {run_id!r} is not paused (status: {prior.status})"}
        with self._launches.reserve(run_id) as submission:
            if submission.error is not None:
                return {"error": submission.error}
            return self._start_unlocked(
                prior.spec, prior.args, tainted=prior.tainted, resume_run_id=run_id,
                owner=prior.owner, pause_prior=prior, resume_token=_token,
                submission=submission,
            )

    def status(self, run_id: str, *, wait: bool = False, timeout: float | None = None) -> dict:
        state = self._get(run_id)
        if state is None:
            # Not ours — but the run may still be perfectly real (WF-29). Read
            # its line rather than denying a run whose cells are on disk.
            row = self._store.load(run_id)
            if row is None:
                return {"error": f"no workflow run {run_id!r}"}
            line = durable_rollup(
                row,
                spent_total=sum(seed_spend(self._db, run_id)),
                stale=self._store.is_stale(row),
                operator_cap=self._operator_cap,
            )
            # Provenance is part of the read, not a field of the run (SUP-02):
            # this line was rebuilt off the persisted durable store — possibly
            # written by ANOTHER PROCESS or before a restart — and the reader
            # is told which primary read path was taken. A fresh copy per call —
            # never shared state.
            line["observation"] = rollup.observation("durable_store")
            return line
        if wait and state.future is not None:
            try:
                state.future.result(timeout=timeout)
            except Exception:
                pass
        # One read, two consumers: the rollup's own number and the pause remedy,
        # which must agree about what this run has spent (#47).
        run_spent_total = spent_total(self._db, state.run_id, engine_spent(state.engine))
        replayed, saved = run_replay(state)
        summary = rollup.summarize(
            run_id,
            state.status,
            state.result,
            state.error,
            pause=pause_fields(
                state.status,
                state.pause_reason,
                state.resume_at,
                state.attempts,
                state.checkpoint,
                route_fault=state.route_fault,
                token_budget=(
                    state.engine.budget.token_budget if state.engine is not None else None
                ),
                operator_cap=self._operator_cap,
                spent=run_spent_total,
            ),
            # Read off the LIVE engine, not the RunResult: the result only exists
            # once the run is terminal, and a run still burning tokens is exactly
            # when the agent needs to see what is left. ``overrun_max`` is the
            # one field the engine cannot answer alone (#71) — the run's mark
            # across every ceiling it ran under — so ``_run_budget`` adds it
            # beside the segment's live ``overrun``, matching the durable
            # surface field for field.
            budget=_run_budget(state),
            # The whole run's cost, next to the segment's (WF-23). Unconditional:
            # the {total, spent, remaining} block only exists when a ceiling was
            # asked for, and a run without one still costs money.
            spent_total=run_spent_total,
            # Everything this run has faulted on, not just this stretch (WF-26).
            faults_total=state.prior_faults + list(state.result.faults if state.result else []),
            # What the run recovered from, and what those recoveries cost —
            # both cumulative across stretches, like ``faults_total`` (Q2, #43).
            recovered_faults=carried_recovered(state.prior_recovered, state.result),
            # ...and what it was only ADVISED about, cumulative too (#45): the
            # list that lets a reader reconcile a `complete` with a fault.
            advisory_faults=carried_advisory(state.prior_advisory, state.result),
            leaf_respawns_total=run_leaf_respawns(state),
            # ...and how many leaves' bills are unknown, across stretches too:
            # this counter sits next to a CUMULATIVE token total, so reporting
            # only the segment's would understate exactly where it matters.
            uncertain_total=run_uncertain(state),
            # Same live read, same reason (M6): mid-run there is no RunResult, and
            # mid-run is when "where is this?" is worth answering.
            progress=progress_fields(state),
            # ...and WHICH node spent what, with the cache made visible and the
            # money where the model has a price (Fatia C). Same live read again.
            nodes=state.engine.node_costs() if state.engine is not None else None,
            spent_split=split_total(self._db, state.run_id, engine_split(state.engine)),
            # ...and how much of this run the node cache served instead of a
            # provider (#61) — cumulative across stretches, like the totals
            # above it, and the number a reader checks the ``cache_preview``
            # against once the resume is actually running.
            cells_replayed=replayed,
            tokens_saved=saved,
        )
        # Provenance on the in-process read too (SUP-02): same block, different
        # primary source — the caller can tell a local-registry read from a
        # durable-store one.
        summary["observation"] = rollup.observation("local_registry")
        return summary

    def list_templates(self) -> list[dict]:
        return library.list_templates(self._home)

    def get_template(self, name: str) -> dict | None:
        return library.get_template(self._home, name)

    def recent_insights(self) -> list[dict[str, Any]]:
        """Causally gated candidates only; legacy ``insights.md`` stays hidden.

        Returns the structured projection (Wave 9 slice E2, issue #51): each
        item is a dict carrying ``summary``, ``mechanism``, ``responsibility``,
        ``confidence``, ``status`` — and ``hits`` when the row has it — instead
        of collapsing to prose, so a consumer can filter by ``responsibility``
        without re-deriving the class from free text. Pure projection: no new
        storage, nothing recomputed (``insight_view.project_insights``).
        """
        return project_insights(self._db.insights.list(limit=20))

    def pause(self, run_id: str) -> dict:
        """Stop a live run at the operator's request — resumably (M6).

        The opposite trade from ``cancel``: nothing in flight is killed and
        nothing is thrown away. Scheduling stops at the next node, the leaves
        already running land and are charged, the finished cells stay in the
        resume cache, and the run reports ``paused`` with reason
        ``user_requested`` — no auto-resume, and no budget to raise on the way
        back in.

        Returns ``pausing``, not ``paused``: the status flips only once the
        engine returns from the node it is inside.
        """
        state = self._get(run_id)
        if state is None:
            # It may be a perfectly real run (workflow_status will show it) —
            # just not one this process holds an engine for, and only the engine
            # can stop scheduling. Say which of the two it is.
            if self._store.load(run_id) is None:
                return {"error": f"no workflow run {run_id!r}"}
            return {
                "error": f"workflow run {run_id!r} is not running in this process; only "
                "the process that launched it can pause it (workflow_cancel stops one "
                "whose process is gone)"
            }
        if state.engine is None or not _is_live(state):
            return {
                "error": f"workflow run {run_id!r} is not running (status: {state.status})"
            }
        state.engine.request_pause()
        return {"ok": True, "run_id": run_id, "status": "pausing"}

    def _steer_audit(
        self,
        event_type: str,
        ctx: Any,
        sub_id: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Record one workflow-owned steering observation; never raise.

        Identity metadata only — the instruction text never enters the
        audit trail.
        """
        if not self._audit_enabled:
            return
        try:
            self._audit.record(
                causal_audit_event(event_type, ctx, sub_id=sub_id, data=data or {})
            )
        except Exception:
            logger.warning("steering audit event %s failed", event_type, exc_info=True)

    def steer(
        self,
        run_id: str,
        sub_id: str,
        text: str,
        *,
        segment_id: str,
        attempt: int,
        turn: int,
    ) -> dict:
        """Inject an instruction into a live run's leaf sub-session.

        Gates that need THIS registry only (local non-fenced state, running,
        core+engine alive); the heavy lift is supervision.steer_live_run.
        """
        state = self._get(run_id)
        if state is None:
            return {
                "error": f"no workflow run {run_id!r} in this process (or this "
                "process was fenced out of it)"
            }
        if state.status != "running":
            return {
                "error": f"workflow run {run_id!r} is not running (status: {state.status})"
            }
        if state.core is None or state.engine is None:
            return {
                "error": f"workflow run {run_id!r} has no live engine/core in "
                "this process to steer"
            }
        # The durable steering-budget store is the SessionDB: the run ceiling
        # outlives a process handoff (WF-29), so cross-process steering hits
        # the same external budget this process's SteeringLimits enforces.
        return steer_live_run(
            state,
            sub_id,
            text,
            segment_id=segment_id,
            attempt=attempt,
            turn=turn,
            audit=self._steer_audit,
            budget_store=self._db,
        )

    def list_runs(self, limit: int = MAX_LISTED_RUNS) -> list[dict]:
        """Every run this service knows — live ones first, then the durable lines
        of runs it never launched (another process's, or its own before a
        restart). Capped; the "what is going on" view a single run_id cannot give.

        Compact on purpose: the whole listing lands inside one tool result, so it
        carries what picks a run out of a crowd (name, status, how far it got,
        what it cost) and nothing that would need the full rollup. A durable row
        reports honest zeros for the per-node counts only a live engine keeps,
        and ``stale`` when it claims to be running with no owner alive."""
        limit = max(0, limit)
        with self._lock:
            # A run this process was fenced out of is NOT one of its live runs:
            # listing it would show this owner's abandoned stretch and, worse,
            # keep the new owner's durable row out of the listing below (it is
            # deduped against what was already listed).
            live = [state for state in self._runs.values() if not state.fenced]
        states = sorted(live, key=lambda state: state.seq, reverse=True)
        entries = [live_entry(state) for state in states[:limit]]
        # ...plus the runs only SQLite knows about (WF-29): another process's, or
        # this one's before a restart. Live first — a live entry is the same run,
        # one write fresher — then the durable lines, newest first.
        known = {state.run_id for state in states}
        for row in self._store.recent(limit):
            if row.run_id in known or len(entries) >= limit:
                continue
            entries.append(
                list_entry(
                    row,
                    spent=sum(seed_spend(self._db, row.run_id)),
                    stale=self._store.is_stale(row),
                )
            )
        return entries

    def own_run_ids(self) -> list[str]:
        """Run ids THIS instance itself launched or resumed — unlike
        ``list_runs``, never the merged view across the whole store. What the
        CLI's per-turn ``--json`` envelope reports on (issue #47), read BEFORE
        ``shutdown()`` cancels whatever this turn leaves running."""
        with self._lock:
            return [run_id for run_id, state in self._runs.items() if not state.fenced]

    def run_owner(self, run_id: str) -> str | None:
        """The session that launched a run, or None when nobody owns it."""
        state = self._get(run_id)
        if state is not None:
            return state.owner
        row = self._store.load(run_id)
        return row.owner if row is not None else None

    def cancel(self, run_id: str) -> dict:
        """Cancel the current functional state, then stop only this local acquisition."""
        state = self._get(run_id)
        receipt = self._store.cancel_state(run_id, fence=state.fence if state is not None else None)
        if state is not None:
            self._apply_state(state, receipt)
        if not receipt.accepted:
            if (state is not None and receipt.kind == "conflict"
                    and receipt.fence != state.fence):
                with state.state_lock:
                    state.fenced = True
                if state.engine is not None:
                    state.engine.request_cancel()
                if state.core is not None:
                    state.core.shutdown(wait=False)
            return self._write_refusal(run_id, receipt)
        self._autoresume.cancel_fence(run_id, receipt.fence)
        if state is not None:
            if state.engine is not None:
                state.engine.request_cancel()
            if state.core is not None:
                state.core.shutdown(wait=False)
        return {"ok": True, "run_id": run_id}

    def _abort_route_fault(self, run_id: str, prior: Any, node_id: str) -> dict:
        """Cancel the authoritative answered pause and append its abort fault.

        The transaction checks its captured revision, functional state and lease.
        Refusal and storage failure publish no acknowledgement or follow-up effect.
        This ownerless operation never acquires or releases a lease.
        """
        fault = abort_fault(node_id, (prior.route_fault or {}) if prior is not None else {})
        receipt = self._store.cancel_state(
            run_id, extra_faults=[fault],
            expected_revision=prior.revision if prior is not None else None,
        )
        if not receipt.accepted:
            return self._write_refusal(run_id, receipt)
        self._autoresume.cancel_fence(run_id, receipt.fence)
        state = self._get(run_id)
        if state is not None:
            self._apply_state(state, receipt)
            with state.state_lock:
                if state.fence == receipt.fence and fault not in state.prior_faults:
                    state.prior_faults = state.prior_faults + [fault]
        return {"run_id": run_id, "status": "cancelled"}

    def _abort_fenced_run(self, run_id: str, fence: int | None = None) -> None:
        """This process lost a run's lease while still inside it — stop working.

        The fencing of issue #12 made an obsolete owner's WRITES fail closed, so
        it can no longer corrupt the new owner's line. It never stopped the
        obsolete owner from EXECUTING: it kept scheduling nodes, kept holding orch
        workers and kept paying a provider for a run somebody else had taken over.
        The heartbeat is the only thing in this process that learns of the
        takeover, so this is where the burning ends.

        IN-MEMORY ONLY, deliberately: a fenced-out process must never route
        control through a write path its own fence now rejects, or the abort
        would be exactly as ineffective as the writes it is reacting to.
        ``request_cancel`` is a flag the run loop reads and ``shutdown(wait=False)``
        is local pool teardown — both still work with no claim on the run at all.

        Nothing is persisted and nothing is marked cancelled: the outcome of this
        run is the NEW owner's to write, and a status this process pushed would be
        refused by the fence anyway (or, worse, believed).

        Runs on a heartbeat timer thread — never block it."""
        state = self._get(run_id)
        if state is None or (fence is not None and state.fence != fence):
            return  # a run we no longer hold in memory has nothing left to stop
        logger.warning(
            "workflow: run %s was taken over by another process; "
            "stopping this owner's engine instead of burning tokens on it",
            run_id,
        )
        # Stop ANSWERING for it too, not just working on it. Aborting the engine
        # while leaving the state readable left this process reporting its own
        # dead stretch over the new owner's line, and ``cancel`` short-circuiting
        # on that state to a false ``{"ok": true}``. Marked (not deleted): the
        # run thread's finally still holds this state, and the identity-checked
        # cleanup still has to find it.
        with state.state_lock:
            state.fenced = True
        self._autoresume.cancel_fence(run_id, state.fence)
        if state.engine is not None:
            state.engine.request_cancel()
        if state.core is not None:
            state.core.shutdown(wait=False)

    def shutdown(self) -> None:
        # Close admission, then wait without its mutex for existing preparations
        # to accept or clean up before dismantling any producer or sink.
        self._launches.close()
        self._autoresume.shutdown()  # no timer outlives the service
        with self._lock:
            states = list(self._runs.values())
        for state in states:
            if state.engine is not None:
                state.engine.request_cancel()
            if state.core is not None:
                state.core.shutdown()
        # A run thread emits its terminal segment and releases its lease after
        # its core settles.  Drain those producers before stores and audit sink.
        self._pool.shutdown(wait=True)
        for state in states:
            self._store.release(state.run_id, fence=state.fence)
        self._store.shutdown()  # no heartbeat outlives this service either
        if not self._audit.shutdown():
            logger.warning("workflow audit sink did not drain before bounded shutdown")

    def _get(self, run_id: str) -> RunState | None:
        """The run's live state — or None once this process was fenced out of it.

        One seam for every consumer (``status``, ``cancel``, ``pause``,
        ``resume`` via ``_prior``, ``run_owner``): a fenced-out owner has nothing
        true left to say about the run, so they all fall through to the durable
        line the NEW owner is writing."""
        with self._lock:
            state = self._runs.get(run_id)
        return None if state is not None and state.fenced else state
