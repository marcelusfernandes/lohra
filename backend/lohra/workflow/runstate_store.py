"""Durable run state for workflow runs (WF-29) — the line that outlives a process.

``RunState`` is process-local by construction (it holds an engine, a core and a
Future), so everything a resume needs used to die with the process that launched
the run: the spec, the args, why it paused, what a checkpoint was waiting for,
how many auto-resume attempts it had already spent. Only the token ledger and the
node cache reached SQLite — the cells replayed, but nothing knew there was a run
to replay them for.

This module is the missing half:

- **the line** (``workflow_run_state``): one row per run, written at launch and at
  every status transition. JSON for the compound fields, nothing live;
- **the lease** (``workflow_run_locks``): the ``compression_locks`` contract, for
  runs — PRIMARY KEY single-winner plus a TTL, so two processes resuming the same
  run have one winner and a run whose owner DIED is recognisably ownerless rather
  than permanently locked. The clock is injected, so the whole policy is testable
  without a sleep.

The read-side shaping lives here too — the durable builders
(``durable_rollup``/``list_entry``), their live twins
(``progress_fields``/``live_entry``), the shared ``pause_fields``, and
``view_of``, which describes a live ``RunState`` as a ``DurableRun`` so ONE
resume path serves both. The service keeps only the branch points: a paused run
reads the same way whether its state came from memory or from the row.
"""

from __future__ import annotations

from collections import Counter
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED
from lohra.workflow.operator_budget import (
    OPERATOR_PAUSE_HINT,
    OPERATOR_SPENT_HINT,
    cap_binds,
)
from lohra.workflow.engine import USER_PAUSE
from lohra.workflow.fencing import EVICTED
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.route_fault import ROUTE_FAULT, route_fault_hint
from lohra.workflow.lease_heartbeat import (
    HEARTBEAT_TICKS_PER_TTL,
    LeaseHeartbeat,
    TimerFactory,
)

logger = logging.getLogger(__name__)

# Long enough that an ordinary run never looks abandoned, short enough that a
# crashed process does not strand its run for an afternoon. Renewed on a TIMER
# while the run is alive (``LeaseHeartbeat``), so neither run length nor any one
# node's duration is the ceiling.
RUN_LEASE_TTL = 900.0

# A run that reached one of these will never move again on its own. It lived in
# ``watch`` (which re-exports it) until the cancel guard needed the same list:
# two copies of "what does ended mean" is exactly how a guard drifts.
TERMINAL_STATUSES = ("complete", "degraded", "failed", "cancelled")
# ...and the subset that carries a real VERDICT. Cancelling one of these would
# overwrite the outcome of a run that already finished and answer ``ok``
# (dogfood candidate ii). ``cancelled`` is deliberately OUT: a second cancel says
# exactly what the first one did and erases nothing anybody will miss.
FINISHED_STATUSES = frozenset(TERMINAL_STATUSES) - {"cancelled"}

# What a run recovered from a lost process records, so the rollup never claims a
# clean stretch it did not have. Substring-stable: tests and priors quote it.
RECOVERED_FAULT = "recovered after process loss"

# How many runs' ownership fences one store remembers (issue #12). Kept rather
# than popped on release — see ``release`` — so the ceiling is what keeps a
# long-lived process from growing one entry per run forever. Oldest first: the
# straggler that could still be writing is, by construction, a recent run.
# An evicted fence does NOT read as unfenced: ``fence_of`` falls back to the
# durable fence row and answers ``EVICTED``, so the ceiling costs a refusal,
# never a licence.
_FENCE_MEMORY = 1024

# "Use whatever fence this store holds for the run", as distinct from an
# explicit ``fence=None``, which means "unfenced: write like a pre-#12 caller".
_OWN_FENCE: Any = object()

STALE_HINT = (
    "the process that was running this workflow was lost before it finished; the "
    "cells it completed are kept — run_workflow(resume_run_id=...) continues it"
)
BUSY_HINT = "another process is running this workflow right now"


def _dumps(value: Any) -> str | None:
    """Compact JSON, or None for nothing worth a column."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(raw: Any, fallback: Any) -> Any:
    """Never let one unreadable row take a listing down: a corrupt blob reads as
    the fallback, exactly like a row that was never written."""
    if not isinstance(raw, str) or not raw:
        return fallback
    try:
        return json.loads(raw)
    except ValueError:
        logger.warning("workflow: unreadable durable run-state payload; ignoring it")
        return fallback


def _string_list(raw: Any) -> list[str]:
    """A payload field that must be a list of strings, or nothing at all — the
    same never-trust-the-blob rule ``prior_faults`` reads under."""
    return [str(item) for item in raw] if isinstance(raw, list) else []


@dataclass(frozen=True)
class DurableRun:
    """One run as SQLite knows it — the resume-relevant half of ``RunState``."""

    run_id: str
    name: str = ""
    owner: str | None = None
    status: str = "running"
    pause_reason: str | None = None
    checkpoint: dict | None = None
    # What a ``route_fault`` pause stopped ON (#43): the dead route, named.
    route_fault: dict | None = None
    resume_at: float | None = None
    attempts: int = 0
    prior_faults: list[str] = field(default_factory=list)
    prior_degraded: bool = False
    # The faults earlier stretches RECOVERED from by re-spawning the same route
    # (Q2, #43). Durable for the same reason ``prior_faults`` is: a resume in a
    # fresh process rebuilds an empty ``RunResult``, and without this list the
    # recovered faults it inherits through ``prior_faults`` would read like
    # failures nobody fixed. A subset of ``prior_faults``, always.
    prior_recovered: list[str] = field(default_factory=list)
    # WHICH nodes an earlier stretch re-routed through the command channel after
    # a ``route_fault`` pause (#43). Durable for the same reason the counters
    # beside it are: a template certified by the LAST stretch has to be able to
    # say it only got there on an emergency route somebody supplied mid-run.
    prior_rerouted: list[str] = field(default_factory=list)
    # ...and the ones the CATALOG substituted, as {node, from, to} (#85). A
    # sibling of the list above rather than a subset of it: ``prior_rerouted``
    # is channel-blind by design (the stamp's reader does not care which surface
    # supplied a route), but a slug that never EXISTED is a different fact from
    # an emergency route, and only this list can say which model was replaced by
    # which. Durable for the same reason: the stretch that certifies is not
    # usually the stretch that substituted.
    prior_substitutions: list[dict] = field(default_factory=list)
    # ...and the ADVISORIES earlier stretches collected (#45): a leaf that
    # miscounted a hash for a file it really wrote. Durable for the same reason
    # as the list above — a resume builds a fresh ``RunResult``, and an advisory
    # it inherits through ``prior_faults`` with nothing marking it would seal
    # ``degraded`` a run that never failed at all. A subset of ``prior_faults``,
    # always.
    prior_advisory: list[str] = field(default_factory=list)
    # ...and the same two counts, PER SOURCE (#75): how many of those advisories
    # were artifact claims the harness corrected, and how many were divergent
    # REPLAYS. Carried as counts because the list above cannot be split by its
    # prose — a certified template stamps the two apart, and a run whose first
    # stretch replayed under an old policy has to still say so in the stretch
    # that certifies it. ``prior_replay_divergences`` counts divergent replays,
    # not distinct cells: a cell replayed in two stretches is two of them.
    prior_artifact_advisories: int = 0
    prior_replay_divergences: int = 0
    # ...and how many extra leaves those stretches paid for, so the counter the
    # rollup reports is the WHOLE run's, not the last stretch's (like the
    # cumulative ``faults_total``/``tokens_spent_total`` next to it).
    prior_leaf_respawns: int = 0
    # ...and the HIGH-WATER MARK of how far past a ceiling this run ever went
    # (#71). A max, not a sum, and durable for a reason the counters beside it
    # do not have: an overrun is measured against the ceiling in force AT THE
    # TIME, and the canonical remedy RAISES that ceiling. Recomputing it live
    # after a renewal gives 0 — a run that really did overspend certifying as
    # one that never did, with the advisory fault still sitting in its
    # ``faults_total`` to contradict it.
    prior_overrun: int = 0
    # ...and how many leaves those stretches lost mid-stream to a cancel (issue
    # #42). Carried for a reason the others share and this one sharpens: a pause
    # CANCELS the leaves in flight, so the pause is the biggest producer of
    # aborted streams — and the resume's fresh ``RunResult`` reports 0 next to a
    # cumulative ``tokens_spent_total`` that already includes their floor. Zero
    # is a positive claim here ("every leaf's usage is exact"), so an uncarried
    # 0 is not a missing number: it is a false one.
    prior_uncertain: int = 0
    # ...and how much of this run came out of the node CACHE instead of a
    # provider (#61), cumulative across stretches for the same reason
    # ``tokens_spent_total`` is: the whole point of a resume is what it did not
    # re-pay for, and a segment-only count reports zero for every stretch that
    # is not the one being watched. ``prior_saved`` is a floor — an unpriced
    # cell adds 0 — never an estimate.
    prior_cells_replayed: int = 0
    prior_saved: int = 0
    tainted: bool = False
    spec: dict | None = None
    args: dict = field(default_factory=dict)
    token_budget: int | None = None
    # Where the run got to, as its owner last wrote it (WF-30). The live
    # ProgressTracker dies with its process; this is the half that does not, so
    # a run this process never launched reports the nodes that really ran
    # instead of the honest-but-useless zeros it used to.
    progress: dict | None = None
    audit_segment_id: str | None = None
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "DurableRun":
        payload = _loads(row.get("pause_payload_json"), {})
        payload = payload if isinstance(payload, dict) else {}
        spec = _loads(row.get("spec_json"), None)
        args = _loads(row.get("args_json"), {})
        progress = _loads(row.get("progress_json"), None)
        faults = payload.get("prior_faults")
        return cls(
            run_id=str(row["run_id"]),
            name=str(row.get("name") or ""),
            owner=row.get("owner"),
            status=str(row.get("status") or "running"),
            pause_reason=row.get("pause_reason"),
            checkpoint=payload.get("checkpoint")
            if isinstance(payload.get("checkpoint"), dict)
            else None,
            route_fault=payload.get("route_fault")
            if isinstance(payload.get("route_fault"), dict)
            else None,
            resume_at=payload.get("resume_at"),
            attempts=int(payload.get("attempts") or 0),
            prior_faults=[str(fault) for fault in faults] if isinstance(faults, list) else [],
            prior_degraded=bool(payload.get("prior_degraded")),
            prior_recovered=_string_list(payload.get("prior_recovered")),
            prior_rerouted=_string_list(payload.get("prior_rerouted")),
            prior_substitutions=_substitution_list(payload.get("prior_substitutions")),
            prior_advisory=_string_list(payload.get("prior_advisory")),
            prior_artifact_advisories=int(payload.get("prior_artifact_advisories") or 0),
            prior_replay_divergences=int(payload.get("prior_replay_divergences") or 0),
            prior_leaf_respawns=int(payload.get("prior_leaf_respawns") or 0),
            prior_overrun=int(payload.get("prior_overrun") or 0),
            prior_uncertain=int(payload.get("prior_uncertain") or 0),
            prior_cells_replayed=int(payload.get("prior_cells_replayed") or 0),
            prior_saved=int(payload.get("prior_saved") or 0),
            tainted=bool(row.get("tainted")),
            spec=spec if isinstance(spec, dict) else None,
            args=args if isinstance(args, dict) else {},
            token_budget=int(row["token_budget"]) if row.get("token_budget") else None,
            progress=progress if isinstance(progress, dict) else None,
            audit_segment_id=(
                str(row["audit_segment_id"]) if row.get("audit_segment_id") else None
            ),
            updated_at=float(row.get("updated_at") or 0.0),
        )


class RunStateStore:
    """Read/write the durable line and hold the run's cross-process lease.

    One store per service instance: ``holder`` identifies THIS process's service,
    the way a compaction holder identifies the compressor that owns a session.
    """

    def __init__(
        self,
        db: Any,
        *,
        holder: str | None = None,
        clock: Callable[[], float] = time.time,
        ttl: float = RUN_LEASE_TTL,
        timer_factory: TimerFactory | None = None,
        on_lease_lost: Callable[[str], None] | None = None,
    ) -> None:
        self._db = db
        self._holder = holder or f"{os.getpid()}:{uuid4().hex[:8]}"
        self._clock = clock
        self._ttl = max(1.0, float(ttl))
        self._lock = threading.Lock()
        self._renewed: dict[str, float] = {}
        # run_id -> the fence of the acquisition this store won (issue #12).
        # Every write made under that ownership presents it, and SQLite refuses
        # it once a newer owner has taken the run.
        self._fences: dict[str, int] = {}
        # The lease is renewed by TIME, not by the run's output: a node that
        # takes longer than the TTL must not be able to lapse the lease of the
        # run that is still inside it (see lease_heartbeat.py).
        self._heartbeat = LeaseHeartbeat(
            self._beat,
            interval=self._ttl / HEARTBEAT_TICKS_PER_TTL,
            timer_factory=timer_factory,
            # Losing the lease is not just bookkeeping: the run this process is
            # still executing now belongs to somebody else, and only the owner
            # above can stop it (issue #8's half of the fencing story).
            on_lease_lost=on_lease_lost,
        )

    @property
    def holder(self) -> str:
        return self._holder

    # --- the line -------------------------------------------------------

    def save(
        self,
        *,
        run_id: str,
        name: str = "",
        owner: str | None = None,
        status: str = "running",
        pause_reason: str | None = None,
        checkpoint: dict | None = None,
        route_fault: dict | None = None,
        resume_at: float | None = None,
        attempts: int = 0,
        prior_faults: list[str] | None = None,
        prior_degraded: bool = False,
        prior_recovered: list[str] | None = None,
        prior_rerouted: list[str] | None = None,
        prior_substitutions: list[dict] | None = None,
        prior_advisory: list[str] | None = None,
        prior_artifact_advisories: int = 0,
        prior_replay_divergences: int = 0,
        prior_leaf_respawns: int = 0,
        prior_overrun: int = 0,
        prior_uncertain: int = 0,
        prior_cells_replayed: int = 0,
        prior_saved: int = 0,
        tainted: bool = False,
        spec: dict | None = None,
        args: dict | None = None,
        token_budget: int | None = None,
        progress: dict | None = None,
        audit_segment_id: str | None = None,
        fence: Any = _OWN_FENCE,
        require_unleased: bool = False,
    ) -> bool:
        """Write the run's line. Never raises: a bookkeeping write must not be
        able to take down the run thread it is called from.

        Fenced by default with whatever fence this store holds for the run
        (issue #12), so a stale owner's transition cannot replace the line of
        the process that took the run over. Callers that own a STRETCH pass
        theirs explicitly; the ownerless paths (``mark_cancelled`` on a run
        nobody holds) present None and write like they always did. False = the
        write was refused because the run has a newer owner.

        ``require_unleased`` adds the ownerless condition to the SAME statement:
        the write lands only while nobody holds a live lease on the run. It is
        what the administrative paths (cancelling a run this process only knows
        from its line) need, and the only thing that makes their "nobody is
        inside this run" true at the moment of the write rather than a moment
        before it."""
        payload = {
            "checkpoint": checkpoint,
            "route_fault": route_fault,
            "resume_at": resume_at,
            "attempts": int(attempts),
            "prior_faults": list(prior_faults or []),
            "prior_degraded": bool(prior_degraded),
            "prior_recovered": list(prior_recovered or []),
            "prior_rerouted": list(prior_rerouted or []),
            "prior_substitutions": _substitution_list(prior_substitutions),
            "prior_advisory": list(prior_advisory or []),
            "prior_artifact_advisories": int(prior_artifact_advisories),
            "prior_replay_divergences": int(prior_replay_divergences),
            "prior_leaf_respawns": int(prior_leaf_respawns),
            "prior_overrun": int(prior_overrun),
            "prior_uncertain": int(prior_uncertain),
            "prior_cells_replayed": int(prior_cells_replayed),
            "prior_saved": int(prior_saved),
        }
        guard = self.fence_of(run_id) if fence is _OWN_FENCE else fence
        if guard is EVICTED:
            # This store cannot present the run's fence, so it has no way to
            # prove the line is still its to move. Refusing is the same verdict
            # SQLite would give a stale fence — reached here because there is no
            # fence to send at all.
            logger.warning(
                "workflow: refused a run line write for run %s — this process "
                "cannot present the run's ownership fence",
                run_id,
            )
            return False
        try:
            return bool(
                self._db.run_state_put(
                    run_id,
                    {
                        "name": name,
                        "owner": owner,
                        "status": status,
                        "pause_reason": pause_reason,
                        "pause_payload_json": _dumps(payload),
                        "spec_json": _dumps(spec),
                        "args_json": _dumps(args or {}),
                        "token_budget": token_budget,
                        "tainted": tainted,
                        "progress_json": _dumps(progress),
                        "audit_segment_id": audit_segment_id,
                    },
                    self._clock(),
                    fence=guard,
                    unleased_at=self._clock() if require_unleased else None,
                )
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("workflow: could not persist run state for %s", run_id)
            return False

    def load(self, run_id: str) -> DurableRun | None:
        row = self._db.run_state_get(run_id)
        return DurableRun.from_row(row) if row is not None else None

    def recent(self, limit: int) -> list[DurableRun]:
        return [DurableRun.from_row(row) for row in self._db.run_state_recent(limit)]

    def paused_on(self, pause_reason: str, limit: int = 50) -> list[DurableRun]:
        return [
            DurableRun.from_row(row) for row in self._db.run_state_by_pause(pause_reason, limit)
        ]

    # --- the lease ------------------------------------------------------

    def acquire(self, run_id: str) -> bool:
        now = self._clock()
        fence = self._db.acquire_run_lease(run_id, self._holder, ttl_seconds=self._ttl, now=now)
        won = fence is not None
        if won:
            with self._lock:
                self._renewed[run_id] = now
                # The fence of THIS acquisition: the token every write made
                # while we own the run has to present (issue #12).
                self._fences[run_id] = int(fence)
                self._evict_locked()
            # From here the run is ours for as long as we keep saying so, on a
            # clock of our own — never only when it finishes a node.
            self._heartbeat.start(run_id)
        return won

    def _evict_locked(self) -> None:
        """Hold the ceiling, oldest first — but never at the cost of a run this
        store is still INSIDE.

        Oldest-first alone picks exactly the wrong victim: the oldest entry is
        the LONG run, and a process cycling a thousand short ones while it works
        would evict the live run's own fence and then refuse its own audit
        events. So a fence whose lease we are still renewing is skipped, and the
        cap yields to it: what is left is bounded by concurrent live runs, which
        the registry caps anyway."""
        while len(self._fences) > _FENCE_MEMORY:
            victim = next((run_id for run_id in self._fences if run_id not in self._renewed), None)
            if victim is None:
                return  # every fence we remember belongs to a run we still hold
            self._fences.pop(victim)

    def renew(self, run_id: str, *, force: bool = False) -> bool:
        """Push our lease out while the run works; False when it is no longer
        ours. Rate-limited (unless ``force``) and silent.

        Called from leaf-completion threads (every cached cell renews too) and
        from the heartbeat, so it must be cheap and must never raise into them:
        a lease write that loses a race is covered by the TTL. ``force`` is the
        heartbeat's own call — it IS the pace, so the rate limiter (which exists
        to stop finished cells from hammering the row) must not swallow it."""
        now = self._clock()
        with self._lock:
            last = self._renewed.get(run_id)
            if not force and last is not None and now - last < self._ttl / 3:
                return True  # renewed a moment ago; the row is already fresh
            self._renewed[run_id] = now
        try:
            return bool(
                self._db.renew_run_lease(run_id, self._holder, ttl_seconds=self._ttl, now=now)
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("workflow: lease renewal failed for run %s", run_id)
            return True  # one lost write is what the TTL is for

    def _beat(self, run_id: str) -> bool:
        """What the heartbeat calls: a renewal that is never rate-limited."""
        return self.renew(run_id, force=True)

    def release(self, run_id: str) -> bool:
        # The heartbeat stops FIRST: a tick that outlived the release would put
        # the lease back and leave the run looking alive with nobody in it.
        self._heartbeat.stop(run_id)
        with self._lock:
            self._renewed.pop(run_id, None)
            # The FENCE is deliberately kept (the asymmetry with ``_renewed``
            # above is the point): a straggler thread from the released stretch
            # is exactly who must still be fenced, and a store that forgot its
            # fence would write UNFENCED afterwards — the hole this closes.
            # It stays accurate too: nobody else has acquired, so it is still
            # the run's current fence and an honest late write still lands.
        try:
            return bool(self._db.release_run_lease(run_id, self._holder))
        except Exception:  # pragma: no cover - defensive
            return False

    def fence_of(self, run_id: str) -> Any:
        """The fence of the acquisition this store holds (or last held) for the
        run. Callers bind it ONCE per stretch (``RunState.fence``) rather than
        reading it per write: a run re-acquired by this same process gets a new
        fence, and a straggler from the stretch before must not borrow it.

        Three answers, and the last two are NOT the same:

        - ``int`` — the fence of the acquisition this store holds for the run;
        - ``None`` — the run has no ownership fence at all (a pre-#12 database,
          or a run nobody ever leased): write unfenced, as before;
        - ``EVICTED`` — the run IS fenced and this store cannot present its
          fence, because ``_FENCE_MEMORY`` pushed it out or because this store
          never owned the run. REFUSE; writing unfenced here is the fail-open
          the fence exists to prevent.

        The database, not a second bounded cache, is what tells the last two
        apart: the fence row outlives the lease, so "is this run fenced?" is a
        fact on disk rather than something a process can forget. Consulted only
        on a MISS, so the hot path stays a dict lookup.
        """
        with self._lock:
            if run_id in self._fences:
                return self._fences[run_id]
        return EVICTED if self._is_fenced(run_id) else None

    def _is_fenced(self, run_id: str) -> bool:
        """Has this run EVER been acquired under the fencing contract?

        Fails CLOSED: a read that cannot answer degrades to "fenced", so a
        database that is momentarily unreadable refuses writes instead of
        waving through the ones the fence is there to stop."""
        try:
            return self._db.run_fence_of(run_id) is not None
        except Exception:  # pragma: no cover - defensive
            logger.warning("workflow: could not read the ownership fence of run %s", run_id)
            return True

    def lease_expiry(self, run_id: str) -> float | None:
        """When the live lease on this run expires — None when nobody holds one."""
        return self._db.run_lease_expiry(run_id, self._clock())

    def mark_cancelled(self, run_id: str, *, extra_faults: list[str] | None = None) -> str:
        """Stop a run this process only knows from its line. One of:

        - ``"cancelled"`` — the line now says so;
        - ``"missing"`` — there is no such line;
        - ``"finished"`` — the run already ended with a real verdict
          (``complete``/``degraded``/``failed``), so cancelling it would ERASE
          that outcome and answer ok (dogfood candidate ii). An already
          ``cancelled`` line is deliberately NOT finished: a second cancel says
          the same thing as the first and overwrites nothing anyone will miss;
        - ``"busy"`` — somebody holds a LIVE lease on the run, so this cancel
          would have written over a process that is still inside it. The caller
          says so instead; a run live in THIS process takes the cooperative path
          (``engine.request_cancel``) and never reaches here at all.

        The lease is not checked here and honoured later: the condition rides in
        the write's own statement (``require_unleased``). Read-then-write left a
        window in which an acquisition landed between the two, and the cancel
        then replaced a live owner's line with ``cancelled`` — a run reading as
        stopped with a process still working inside it.

        The pause bookkeeping is cleared, not kept: a cancelled run has nothing
        left to wait for, and a resume_at on a cancelled row would re-arm a timer
        for it on the next cold start — the resurrection WF-19 forbids.

        ``extra_faults`` appends to the run's carried faults so a cancel that had
        a REASON can say it on the run's own line (#43: a ``route_fault`` pause
        answered ``abort``). Appended, never substituted: the faults the run
        already collected are why somebody is cancelling it."""
        row = self.load(run_id)
        if row is None:
            return "missing"
        if row.status in FINISHED_STATUSES:
            return "finished"
        written = self.save(
            # UNFENCED on purpose: this is the ownerless path (the caller only
            # reaches it with no live lease on the run), and the run may well
            # have been owned by a THIRD process since this store last held it —
            # a stale fence of ours would silently drop the cancellation.
            run_id=row.run_id,
            name=row.name,
            owner=row.owner,
            status="cancelled",
            pause_reason=None,
            checkpoint=None,
            route_fault=None,
            resume_at=None,
            attempts=row.attempts,
            prior_faults=row.prior_faults + list(extra_faults or []),
            prior_degraded=row.prior_degraded,
            prior_recovered=row.prior_recovered,
            prior_rerouted=row.prior_rerouted,
            prior_substitutions=row.prior_substitutions,
            prior_advisory=row.prior_advisory,
            prior_artifact_advisories=row.prior_artifact_advisories,
            prior_replay_divergences=row.prior_replay_divergences,
            prior_leaf_respawns=row.prior_leaf_respawns,
            prior_overrun=row.prior_overrun,
            prior_uncertain=row.prior_uncertain,
            prior_cells_replayed=row.prior_cells_replayed,
            prior_saved=row.prior_saved,
            tainted=row.tainted,
            spec=row.spec,
            args=row.args,
            token_budget=row.token_budget,
            # Carried, not dropped: a cancelled run is still a run somebody wants
            # to see how far it got.
            progress=row.progress,
            fence=None,
            # ...and the one condition an unfenced write still has to meet.
            require_unleased=True,
        )
        if not written:
            # Refused by the guard: an owner took the run inside the window this
            # used to leave open. Nothing was written, so there is nothing to
            # undo — and nothing of theirs was touched.
            return "busy"
        self.release(run_id)
        return "cancelled"

    def is_stale(self, row: DurableRun) -> bool:
        """A row that claims to be running with nobody holding its lease: the
        process that owned it is gone."""
        return row.status == "running" and self.lease_expiry(row.run_id) is None

    def now(self) -> float:
        return self._clock()

    def shutdown(self) -> None:
        """No heartbeat outlives the service that armed it: this process is
        leaving, and a lease it keeps renewing is a run nobody may resume."""
        self._heartbeat.shutdown()


# --- read-side shaping (shared by the in-memory and durable paths) --------


def pause_fields(
    status: str,
    pause_reason: str | None,
    resume_at: float | None,
    attempts: int,
    checkpoint: dict | None,
    *,
    route_fault: dict | None = None,
    token_budget: int | None = None,
    operator_cap: int | None = None,
    spent: int | None = None,
) -> dict | None:
    """What a paused run tells the polling agent: why, when it retries, and how
    many tries it has already spent — plus the one remedy that applies.

    ``operator_cap`` (issue #47) redirects the budget remedy to the human who
    started this process — but only when the cap actually BINDS this run's
    ceiling (``cap <= token_budget``). A cap above it bars nothing: a human can
    still authorize a bigger token_budget and it will run, so the ordinary hint
    stays right. ``token_budget`` is the RUN's ceiling (persisted, possibly
    written by another process under another cap) and ``operator_cap`` is THIS
    process's: two different numbers, reported as two numbers.

    ``spent`` outranks the pause reason. A run at or over this process's ceiling
    is refused before it spawns no matter WHY it stopped — including a quota
    pause whose auto-resume keeps firing into that refusal, never incrementing an
    attempt. Reporting its ``resume_at`` would promise a retry this process
    cannot deliver, so the retry is dropped and the remedy names the operator."""
    if status != "paused":
        return None
    fields: dict[str, Any] = {
        "reason": pause_reason,
        "resume_at": resume_at,
        "attempts": attempts,
    }
    if operator_cap is not None and spent is not None and spent >= operator_cap:
        # Nothing this process can launch will get past the cap (#47). Whatever
        # the pause reason, that is the fact that decides what happens next.
        fields["resume_at"] = None
        spent_hint = OPERATOR_SPENT_HINT.format(spent=spent, cap=operator_cap)
        if pause_reason == CHECKPOINT:
            fields["checkpoint"] = checkpoint  # the question stays visible
        elif pause_reason == ROUTE_FAULT:
            fields["route"] = route_fault  # ...and so does the dead route
            # TWO facts, and the cap does not outrank the other one: a ceiling
            # raised over a route that still refuses buys nothing, and dropping
            # the route remedy here would also drop the only place the SUP-04
            # boundary is stated. Concatenate; never replace (#43).
            spent_hint = f"{route_fault_hint(route_fault)}. ALSO: {spent_hint}"
        fields["hint"] = spent_hint
        return fields
    if pause_reason == TOKEN_BUDGET_EXHAUSTED:
        # Nothing will wake this run on its own — say what does. WHOSE ceiling it
        # is decides which remedy is honest (#47).
        fields["hint"] = (
            OPERATOR_PAUSE_HINT.format(total=token_budget, cap=operator_cap)
            if cap_binds(token_budget, operator_cap)
            else (
                "the run spent its token budget; nothing will resume it on its own — "
                "report the available token_budget/spend fields and the case for more to "
                "the HUMAN; only after the human supplies a larger cap verbatim, use "
                "run_workflow(resume_run_id=..., token_budget=<human-authorized cap>)"
            )
        )
    elif pause_reason == CHECKPOINT:
        # Waiting on a HUMAN: no amount of time and no bigger budget helps, so
        # say the one thing that does — and say WHAT it is waiting for.
        fields["checkpoint"] = checkpoint
        fields["hint"] = (
            "this run is paused at a checkpoint waiting for the HUMAN's answer — "
            "relay it only when the human supplied it verbatim with "
            "run_workflow(resume_run_id=..., checkpoint_answers={<node_id>: "
            "<human answer>}); the agent never invents an answer or a default. A plain "
            "resume may use a declared default only when the human supplied that "
            "default before the run"
        )
    elif pause_reason == ROUTE_FAULT:
        # A dead ROUTE (#43). Nothing waits it out and no ceiling raises it: the
        # remedy is a different route, and WHO may choose one is the SUP-04
        # boundary the hint states in full. The payload names what died so the
        # reader never has to guess it out of the fault prose.
        fields["route"] = route_fault
        fields["hint"] = route_fault_hint(route_fault)
    elif pause_reason == USER_PAUSE:
        fields["hint"] = (
            "you paused this run; nothing will resume it on its own — its "
            "finished nodes are kept, so run_workflow(resume_run_id=...) "
            "continues it whenever you want (no budget raise needed)"
        )
    return fields


def durable_rollup(
    row: DurableRun, *, spent_total: int, stale: bool, operator_cap: int | None = None
) -> dict:
    """The status of a run this process never launched, read off its line.

    Deliberately NOT a seventh status value: ``stale`` is a FIELD on a run that
    is still, as far as any row knows, running. Inventing a status would ripple
    into every consumer that switches on one — and the honest thing to report is
    "running, and its owner is gone", which is two facts."""
    out: dict[str, Any] = {"run_id": row.run_id, "status": row.status}
    pause = pause_fields(
        row.status,
        row.pause_reason,
        row.resume_at,
        row.attempts,
        row.checkpoint,
        route_fault=row.route_fault,
        token_budget=row.token_budget,
        operator_cap=operator_cap,
        spent=spent_total,
    )
    if pause:
        out.update(pause)
    out["tokens_spent_total"] = spent_total
    if row.token_budget is not None:
        out["token_budget"] = {
            "total": row.token_budget,
            "spent": spent_total,
            "remaining": max(0, row.token_budget - spent_total),
            # Two numbers, two questions (issue #71). ``overrun`` is the
            # arithmetic against the ceiling in force now, derived here exactly
            # as ``Budget.overrun`` derives it live. ``overrun_max`` is the
            # HIGH-WATER MARK a renewal must not erase — the one field of the
            # five a reader cannot derive from the others, which is why it is
            # the one that is stored. Both unconditional: a 0 is a claim
            # ("never over any ceiling"), not a silence to interpret.
            "overrun": max(0, spent_total - row.token_budget),
            "overrun_max": max(
                row.prior_overrun, max(0, spent_total - row.token_budget)
            ),
        }
    # Same shape and same None-when-empty rule as the live ``progress_fields``,
    # so the two paths are indistinguishable to a reader (WF-30).
    if isinstance(row.progress, dict) and row.progress.get("total"):
        out["progress"] = row.progress
    if row.prior_faults:
        out["faults_total"] = list(row.prior_faults)
    if row.prior_recovered:
        out["recovered_faults"] = list(row.prior_recovered)
    # Unconditional, unlike the list above: an advisory is what reconciles a
    # ``complete`` next to a fault, so "this run was advised about nothing" is a
    # claim worth making rather than a silence to interpret (#45).
    out["advisory_faults"] = list(row.prior_advisory)
    out["leaf_respawns"] = row.prior_leaf_respawns
    # Same unconditional rule as the live rollup: 0 is an assertion, not silence.
    out["usage_uncertain_leaves"] = row.prior_uncertain
    # What the node cache served this run, for a reader who never owned it
    # (#61). Persisted rather than recomputed: the cells are on disk, but which
    # of them a stretch REPLAYED is a fact about that stretch, not about the
    # cache — and a fresh process has no engine to ask.
    out["cells_replayed"] = row.prior_cells_replayed
    out["tokens_saved"] = row.prior_saved
    if row.name:
        out["name"] = row.name
    if row.status == "running":
        out["stale"] = stale
        out["hint"] = STALE_HINT if stale else BUSY_HINT
    return out


def progress_fields(state: Any) -> dict | None:
    """The run's live per-node progress, or None while there is nothing to say
    (no engine, or one that has not reached its first node yet)."""
    if state.engine is None:
        return None
    snapshot = state.engine.progress_snapshot()
    return snapshot if snapshot["total"] else None


def live_progress(state: Any) -> dict | None:
    """The snapshot to WRITE to a run's line — the same None-when-empty rule the
    read side uses, so a run that never reached a node persists no progress at
    all rather than an empty block every reader has to special-case."""
    return progress_fields(state)


def live_entry(state: Any) -> dict:
    """One row of ``list_runs`` — honest zeros for a run whose engine never was.

    ``nodes_done``/``nodes_total`` are this run's own DAG (what the live tracker
    counts), not the rollup's nested-folded ``nodes_total``: the listing is a
    "where is it" glance, and the full rollup is one workflow_status away.

    ``overrun_max`` (H11, #81, follow-up of #71) uses ``run_overrun`` — the
    SAME high-water-mark fold ``list_entry``'s durable side and the live
    rollup both use — so ``list_entry``'s claim of "the same shape the live
    listing emits" stays true: a run this process still owns and one it only
    knows from the durable line render an identical ``+N over`` off the same
    render_run_row, whichever half of ``list_runs``'s merge produced the row."""
    budget = state.engine.budget if state.engine is not None else None
    progress = state.engine.progress_snapshot() if state.engine is not None else None
    entry: dict[str, Any] = {
        "run_id": state.run_id,
        "name": state.name,
        "status": state.status,
        "nodes_done": progress["done"] if progress else 0,
        "nodes_total": progress["total"] if progress else 0,
        "tokens_spent": budget.tokens_spent if budget is not None else 0,
        "token_budget": budget.token_budget if budget is not None else None,
        "overrun_max": run_overrun(state),
    }
    if state.status == "paused" and state.pause_reason is not None:
        entry["pause_reason"] = state.pause_reason
    return entry


def list_entry(row: DurableRun, *, spent: int, stale: bool) -> dict:
    """One ``workflow_list`` row for a run only SQLite knows about — the same
    shape the live listing emits, off the progress its owner persisted. Zeros
    only when nothing was ever written (a run that died before its first node).

    ``overrun_max`` (H11, #81, follow-up of #71): the SAME high-water-mark
    arithmetic ``durable_rollup`` already uses for ``workflow_status`` —
    ``row.prior_overrun`` folded with the ceiling in force now — so the
    zero-cost read path (``lohra workflow list``/``watch``) can say a run
    overran without an agent ever spending a turn on ``workflow_status`` to
    ask. Unconditional like the rest of this dict's overrun doctrine: 0 is the
    claim "never over any ceiling", not a silence to interpret."""
    progress = row.progress if isinstance(row.progress, dict) else None
    entry: dict[str, Any] = {
        "run_id": row.run_id,
        "name": row.name,
        "status": row.status,
        "nodes_done": int(progress.get("done") or 0) if progress else 0,
        "nodes_total": int(progress.get("total") or 0) if progress else 0,
        "tokens_spent": spent,
        "token_budget": row.token_budget,
        "overrun_max": (
            max(row.prior_overrun, max(0, spent - row.token_budget))
            if row.token_budget is not None
            else 0
        ),
    }
    if row.status == "running" and stale:
        entry["stale"] = True
    if row.status == "paused" and row.pause_reason is not None:
        entry["pause_reason"] = row.pause_reason
    return entry


def carried_recovered(prior_recovered: list[str], result: Any) -> list[str]:
    """Every fault this run has RECOVERED from, across its stretches (Q2, #43).

    The cumulative sibling of ``carried_faults``: a subset of the list that one
    returns, and durable for the same reason — the verdict of a LATER stretch is
    computed off a fresh ``RunResult`` that never saw the earlier series."""
    return list(prior_recovered) + list(result.recovered_faults if result is not None else [])


def carried_rerouted(prior_rerouted: list[str], result: Any) -> list[str]:
    """Every NODE this run had re-routed, across its stretches (#43 + #63).

    **Node ids, never prose.** The single consumer of this list is
    ``library._save_template(rerouted_nodes=…)``, which stamps
    ``meta.rerouted_nodes`` on a certified template — a list of the nodes that
    only got there on a route somebody supplied mid-run. The command channel has
    always written ids here (``service`` appends ``answered.node_id`` when it
    applies an answer, and puts the human-readable ``reroute_fault`` in
    ``prior_faults`` instead), so the envelope must too: appending a sentence
    would publish a template whose ``rerouted_nodes`` names no node at all.

    Same list for both channels on purpose: what the stamp has to say is "this
    works, and here is the emergency route it needed", and the reader of that
    stamp does not care which surface supplied the route.

    Durable for the reason ``carried_faults`` is: a later stretch computes off a
    fresh ``RunResult`` that never saw the re-route an earlier one made."""
    return list(prior_rerouted) + [
        node_id
        for entry in (result.reroutes if result is not None else [])
        if isinstance(entry, dict)
        for node_id in (entry.get("node_id"),)
        if isinstance(node_id, str) and node_id
    ]


def _substitution_list(value: Any) -> list[dict]:
    """The ``{node, from, to}`` rows of a payload, normalised and bounded.

    Read defensively for the reason ``_string_list`` is: the line is JSON on
    somebody's disk, possibly written by another version. Only the three keys
    this record means are kept, only as strings, and a row missing any of them
    is dropped whole — a half-named substitution names nothing.
    """
    if not isinstance(value, list):
        return []
    rows: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        row = {key: entry.get(key) for key in ("node", "from", "to")}
        if all(isinstance(item, str) and item for item in row.values()):
            rows.append(row)
    return rows


def carried_substitutions(prior_substitutions: list[dict], result: Any) -> list[dict]:
    """Every model this run had SUBSTITUTED, across its stretches (#85).

    The structured sibling of ``carried_rerouted``, and durable for exactly the
    reason that one is: a later stretch computes off a fresh ``RunResult`` that
    never saw the substitution an earlier one made, so a run that substituted,
    paused, resumed and certified would publish a template whose one measured
    run reads as a spec the author got right the first time. The prose survives
    through ``prior_advisory`` and the node id through ``prior_rerouted``; this
    is the only carrier of WHICH model replaced WHICH.
    """
    return _substitution_list(
        list(prior_substitutions)
        + list(result.model_substitutions if result is not None else [])
    )


def carried_advisory(prior_advisory: list[str], result: Any) -> list[str]:
    """Every fault this run was merely ADVISED about, across its stretches (#45).

    The third sibling of ``carried_faults``/``carried_recovered``, durable for
    the same reason: a later stretch's ``RunResult`` never saw the divergence an
    earlier one was told about, and the fault travels forward regardless."""
    return list(prior_advisory) + list(result.advisory_faults if result is not None else [])


def carried_faults(prior_faults: list[str], result: Any) -> tuple[list[str], bool]:
    """(faults so far, did an earlier stretch really fail) after ``result``.

    A pause is not a lesson about the spec (waiting, or a raised ceiling, is the
    whole remedy), so the pause's own fault is discounted — and so is every fault
    the pause CAUSED (the leaves it stops on purpose, because they would all 429
    too). Both are administrative: the run is coming back, and it was coming back
    precisely BECAUSE of them. Everything else counts, which is what keeps a
    crashed-and-resumed run from being certified as a template on the strength of
    its last clean stretch.

    An ADVISORY fault (#45) is discounted on the same grounds, and so is a fault
    a same-route re-spawn RECOVERED from (Q2, #43): the provider really did refuse, but the node produced its
    output and the run carried on, so it is no more a verdict about the spec than
    a pause is. Only THIS stretch's recoveries are consulted, and that is the
    whole story: the verdict of each stretch is sealed here, at its end, and
    carried forward as the ``prior_degraded`` boolean. Reaching back into an
    earlier stretch's recovered list would not add anything a stretch can use —
    its own faults are the only ones it can judge — and it would open a real
    hazard, since faults are matched by text and a LATER death can read exactly
    like an EARLIER one that was fixed. ``carried_recovered`` still carries the
    list across stretches, for the rollup to report, not for the verdict.

    A MULTISET, for that same collision reason: two leaves stopped by one pause
    can land byte-identical messages, and discounting by membership would retire
    a third fault that nothing accounts for.

    Discounted, never hidden: all of them stay in ``faults``."""
    faults = list(prior_faults) + list(result.faults if result is not None else [])
    if result is None:
        return faults, False
    administrative = Counter(result.pause_faults)
    administrative.update(result.recovered_faults)
    # An ADVISORY is discounted on its own grounds (#45): the node concluded and
    # the harness corrected the number, so it is no more a verdict about the
    # spec than a pause or a recovery is.
    administrative.update(result.advisory_faults)
    # ...and so is a fault the OPERATOR's envelope re-routed around (#63): the
    # remedy came out of a list the operator wrote before the run, and the node
    # that carried it went on to produce its output.
    administrative.update(result.rerouted_faults)
    if result.pause_fault is not None:
        administrative.update([result.pause_fault])
    return faults, bool(Counter(result.faults) - administrative)


def run_artifact_advisories(state: Any) -> int:
    """The WHOLE run's artifact-claim advisories (#45/#75): what earlier
    stretches recorded plus what this one has."""
    segment = state.result.artifact_advisories if state.result is not None else 0
    return int(state.prior_artifact_advisories) + int(segment)


def run_replay_divergences(state: Any) -> int:
    """The WHOLE run's divergent REPLAYS (#75): what earlier stretches recorded
    plus what this one has. ONE definition, like the counter beside it, so the
    durable line and the certified template cannot drift apart."""
    segment = state.result.replay_divergences if state.result is not None else 0
    return int(state.prior_replay_divergences) + int(segment)


def run_leaf_respawns(state: Any) -> int:
    """The WHOLE run's extra-leaf count: what earlier stretches paid plus what
    this one has. One definition, so the durable line, the live rollup and the
    template metadata cannot drift apart."""
    segment = state.result.leaf_respawns if state.result is not None else 0
    return int(state.prior_leaf_respawns) + int(segment)


def run_overrun(state: Any) -> int:
    """The WHOLE run's overrun HIGH-WATER MARK: the most it was ever over a
    ceiling, across every stretch and every ceiling it ran under (#71).

    A MAX, unlike the sums beside it, because each stretch measures itself
    against the ceiling in force then. The canonical path is overrun -> pause ->
    the human RAISES the ceiling -> complete: the live number is 0 by the end,
    and reporting that would certify a run that overspent as one that never did.

    One definition, so the durable line, the live rollup and the template
    metadata cannot drift apart — the same rule ``run_leaf_respawns`` follows."""
    live = state.engine.budget.overrun if getattr(state, "engine", None) is not None else 0
    return max(int(state.prior_overrun), int(live))


def run_uncertain(state: Any) -> int:
    """The WHOLE run's count of leaves whose bill is unknown: what earlier
    stretches lost mid-stream plus what this one has. One definition, shared by
    the durable line and the live rollup, so the two cannot drift (issue #42)."""
    segment = state.result.usage_uncertain_leaves if state.result is not None else 0
    return int(state.prior_uncertain) + int(segment)


def run_replay(state: Any) -> tuple[int, int]:
    """The WHOLE run's ``(cells replayed, tokens saved)``: what earlier stretches
    served out of the cache plus what this one has (#61). One definition, so the
    durable line and the live rollup cannot drift apart.

    Read off the LIVE engine when there is one, not off ``state.result``: the
    result only exists once the stretch is terminal, and a run still replaying
    its way through a resumed DAG is exactly when this number is worth having.
    The engine's own counters ARE that result (``run`` holds the object it
    returns), so the two readings can never disagree."""
    engine = getattr(state, "engine", None)
    if engine is not None:
        cells, saved = engine.replay_totals()
    else:
        result = state.result
        cells = result.cells_replayed if result is not None else 0
        saved = result.tokens_saved if result is not None else 0
    return (int(state.prior_cells_replayed) + int(cells),
            int(state.prior_saved) + int(saved))


def busy_error(run_id: str, expiry: float | None, now: float) -> str:
    """What the loser of a cross-process resume is told (WF-29).

    Says WHEN the lease lapses, because that is the number that decides what to
    do next: wait it out, or come back after the other process is gone."""
    remaining = max(0, int(expiry - now)) if expiry is not None else 0
    return (
        f"workflow run {run_id!r} is being resumed by another process (its lease "
        f"expires in ~{remaining}s) — poll it with workflow_status instead of "
        "launching a second engine on the same run"
    )


def view_of(state: Any) -> DurableRun:
    """A live run as the durable line would describe it, so one resume path
    serves both: memory is the same data, one write fresher."""
    faults, degraded = carried_faults(state.prior_faults, state.result)
    return DurableRun(
        run_id=state.run_id,
        name=state.name,
        owner=state.owner,
        status=state.status,
        pause_reason=state.pause_reason,
        checkpoint=state.checkpoint,
        route_fault=state.route_fault,
        resume_at=state.resume_at,
        attempts=state.attempts,
        prior_faults=faults,
        prior_degraded=state.prior_degraded or degraded,
        prior_recovered=carried_recovered(state.prior_recovered, state.result),
        # THIS stretch's re-routes included (#63). ``view_of`` is what a resume
        # in the SAME process reads (``_prior`` prefers the live state over the
        # line), so a bare ``list(state.prior_rerouted)`` here would hand the
        # next stretch an empty list and ERASE what the durable line already
        # recorded — the one path where a memory view being "one write fresher"
        # made it staler instead.
        prior_rerouted=carried_rerouted(state.prior_rerouted, state.result),
        # ...and the same correction for the same reason (#85): a resume in
        # THIS process reads the memory view, so a bare copy of the field
        # would erase the substitutions the durable line already carries.
        prior_substitutions=carried_substitutions(
            state.prior_substitutions, state.result
        ),
        prior_advisory=carried_advisory(state.prior_advisory, state.result),
        prior_artifact_advisories=run_artifact_advisories(state),
        prior_replay_divergences=run_replay_divergences(state),
        prior_leaf_respawns=run_leaf_respawns(state),
        prior_overrun=run_overrun(state),
        prior_uncertain=run_uncertain(state),
        tainted=state.tainted,
        spec=state.spec_dict,
        args=state.args or {},
        token_budget=state.engine.budget.token_budget if state.engine is not None else None,
        progress=live_progress(state),
    )
