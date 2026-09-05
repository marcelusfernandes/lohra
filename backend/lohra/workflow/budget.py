"""The unified workflow budget — fan-out width, leaf lifetime, token spend.

Bounded BY CONSTRUCTION, never unbounded. Fan-out width and the per-run leaf
lifetime are counted here (``check_fanout``, consulted before every fan-out);
the TOKEN budget (spec §7.1) is the third axis: an operator-set ceiling on what
a whole run may cost, charged from every leaf the engine collects.

NOT covered here: how many leaves run AT ONCE. That is
``OrchestrationCore(max_concurrent=...)``'s job (``orchestration/core.py``) via
its ``ThreadPoolExecutor`` — the only place concurrency is actually enforced.
A ``pool_width`` used to live on this class too; it was removed (issue #73)
because nothing ever read it — the real cap was always the pool's.

The token gate is deliberately SOFT — it is read before a spawn, never mid-call:
a leaf already in flight is work already paid for, so it finishes and is charged
even if that overruns the total. Refusing the NEXT spawn is what bounds the run.
Overrun therefore means ``tokens_spent`` can exceed ``token_budget``;
``tokens_remaining`` clamps at zero, ``tokens_spent`` stays honest, and
``overrun`` (derived, never stored) is what makes the crossing legible.

The ceiling is a STOP LINE, not a post-mortem (issue #71): once this run has
measured a leaf of its own, "can what is left pay for one more?" is the question
the gate asks — ``spent >= total`` alone let a run whose remaining budget could
not buy a single leaf spawn it anyway, so the line was only ever crossed, never
held.

Still out of scope: the process-global agent semaphore (§7.3) — no cap spans
concurrent runs today.
"""

from __future__ import annotations

import threading

DEFAULT_MAX_FANOUT = 64
DEFAULT_LIFETIME = 1000  # max leaf spawns across the whole run

# The pause reason for a run stopped by its token budget (a sibling of the
# provider's quota_exhausted — same "paused", a very different remedy).
TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"

# What ONE leaf is assumed to cost before this run has any evidence of its own
# (spec §7.1's ``EST_TOKENS_PER_LEAF``). Deliberately generous: it only ever
# gates a BARRIER fan-out, where the alternative is dispatching the whole width
# against a ledger nothing has charged yet. The moment a leaf is charged, this
# run's OWN measured average supersedes it.
EST_TOKENS_PER_LEAF = 2000

# The advisory fault a crossing writes (issue #71). Shared so the one consumer
# that must NOT count it as something else — ``library``'s artifact-divergence
# stamp — recognises it by the same string the engine writes, and keeps
# recognising it through the ``sub[ref]:`` prefix a nested run adds.
TOKEN_BUDGET_OVERRUN = "token budget overrun"


class FanoutRejected(Exception):
    """A fan-out exceeded the budget — rejected and logged, never silently capped."""


class LifetimeExhausted(FanoutRejected):
    """One more leaf would exceed the run's declared lifetime — refused at the
    spawn funnel, atomically, so concurrent claimers cannot all be granted.

    A subclass of ``FanoutRejected`` on purpose: every strategy that already
    handles a cap trip keeps handling this one, so a per-spawn refusal can never
    escape as a bare engine fault in a node type that has not been re-read.
    """


class TokenBudgetExhausted(Exception):
    """A leaf spawn was refused: the run has spent its whole token budget.

    Raised by the engine's spawn funnel AFTER it latches the run's pause, so
    callers only have to settle the cell they were about to start.
    """


class Budget:
    def __init__(
        self,
        *,
        max_fanout: int = DEFAULT_MAX_FANOUT,
        lifetime: int = DEFAULT_LIFETIME,
        token_budget: int | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        charges: int = 0,
    ) -> None:
        self.max_fanout = max(1, max_fanout)
        self._lifetime = max(1, lifetime)
        self._spawned = 0
        # None = no token ceiling asked for (the pre-M5 behaviour, unchanged).
        # A non-zero start is a RESUME picking up where the paused run stopped.
        self._token_budget = token_budget
        self._tokens_in = max(0, tokens_in)
        self._tokens_out = max(0, tokens_out)
        # Leaves whose real cost this run has MEASURED. Non-zero on a resume:
        # the earlier stretches' cells are measurements of this same run, and a
        # resume that forgot them would fall back to the static estimate and
        # answer "can what is left buy one more leaf?" with a number that has
        # nothing to do with this run (issue #71).
        self._charges = max(0, charges)
        self._lock = threading.Lock()

    @property
    def lifetime(self) -> int:
        """The ceiling this run declared — immutable, so no lock is needed."""
        return self._lifetime

    @property
    def lifetime_remaining(self) -> int:
        with self._lock:
            return max(0, self._lifetime - self._spawned)

    def check_fanout(self, width: int) -> None:
        """Raise FanoutRejected if a fan-out of ``width`` would exceed the budget."""
        if width > self.max_fanout:
            raise FanoutRejected(
                f"fan-out of {width} exceeds max_fanout {self.max_fanout}"
            )
        if width > self.lifetime_remaining:
            raise FanoutRejected(
                f"fan-out of {width} exceeds lifetime remaining {self.lifetime_remaining}"
            )

    def charge(self, count: int = 1) -> None:
        """Record ``count`` leaf spawns against the lifetime."""
        with self._lock:
            self._spawned += count

    def reserve(self, count: int = 1) -> bool:
        """Atomically claim ``count`` lifetime slots. False = refused, nothing taken.

        Check AND increment in ONE critical section. Reading
        ``lifetime_remaining`` and then calling ``charge`` also took this lock —
        twice, with a whole ``core.spawn`` (a DB write, a GatewaySession, a pool
        submit) in between. That window is where the pipeline's concurrent
        ``on_done`` workers all read remaining=1 and all decided "yes" (#14).

        The reservation IS the charge: whoever wins keeps the slot, and only a
        leaf that never ran gives it back via ``refund``. That is why there is no
        separate liquidation step — a two-phase reserve/commit would just
        reintroduce a window, in the other direction.

        Alternatives considered, and why this one:
        - **A spawn-count semaphore with no refund.** Same hard cap, simpler.
          Rejected because a slot consumed by a leaf the pool dropped before it
          ever started is a slot spent on nothing — and the pipeline drops leaves
          on every cancel and every quota pause, so the loss is routine, not
          exotic.
        - **Serializing the spawn decisions** (one lock held across
          ``core.spawn``). Correct, and it kills the whole point of the
          no-barrier pipeline: items would advance one at a time behind a lock
          held across DB I/O. This keeps the critical section to two integer
          operations.
        - **Refunding every failed leaf**, not just the ones that never ran.
          Rejected deliberately — see ``refund``.

        The token axis (``tokens_exhausted``) is left alone on purpose: this
        issue is about lifetime atomicity, and the token gate is SOFT by design
        (a leaf in flight is work already paid for). The shapes stay compatible
        should that gate ever want the same treatment: a claim that returns a
        boolean, never a check a caller re-derives.
        """
        if count <= 0:
            return True
        with self._lock:
            if self._spawned + count > self._lifetime:
                return False
            self._spawned += count
            return True

    def refund(self, count: int = 1) -> None:
        """Give back slots claimed for leaves that NEVER RAN.

        Only those. A leaf that reached the provider and failed stays charged:
        ``token_budget`` is None by default, so the lifetime is the only hard
        bound most runs have, and refunding real failures would let an
        always-failing retry shape spawn without end. "Never ran" is a bounded
        set — a spawn that raised, and a sub-session the pool dropped from its
        queue — and it is exactly the set that consumed nothing.

        Never below zero: a refund that outran its reservation would MINT
        lifetime, which is the same overrun this class exists to prevent.
        """
        if count <= 0:
            return
        with self._lock:
            self._spawned = max(0, self._spawned - count)

    # --- token axis (§7.1) ---------------------------------------------

    @property
    def token_budget(self) -> int | None:
        """The ceiling this run was given, or None for "no ceiling asked for"."""
        return self._token_budget

    @property
    def tokens_in(self) -> int:
        with self._lock:
            return self._tokens_in

    @property
    def tokens_out(self) -> int:
        with self._lock:
            return self._tokens_out

    @property
    def tokens_spent(self) -> int:
        """Everything charged so far — UNCLAMPED, so an overrun stays visible."""
        with self._lock:
            return self._tokens_in + self._tokens_out

    @property
    def tokens_remaining(self) -> int:
        """What is left to spend, floored at 0 (never a negative allowance)."""
        if self._token_budget is None:
            return 0
        return max(0, self._token_budget - self.tokens_spent)

    @property
    def tokens_exhausted(self) -> bool:
        """True once the run has spent its whole budget — read BEFORE a spawn.
        No budget means never exhausted."""
        if self._token_budget is None:
            return False
        return self.tokens_spent >= self._token_budget

    @property
    def overrun(self) -> int:
        """How far past the ceiling this run went — DERIVED, never stored, so it
        cannot drift from ``tokens_spent``. 0 without a ceiling, and 0 for every
        run that stayed inside one."""
        if self._token_budget is None:
            return 0
        return max(0, self.tokens_spent - self._token_budget)

    @property
    def has_measurement(self) -> bool:
        """True once this run has priced a leaf of its own — the condition under
        which ``est_leaf_cost`` is a MEASUREMENT rather than the static guess.

        The estimate gate reads this before refusing a spawn: refusing on the
        constant would stop a fresh run under a small ceiling before it ever
        learned what its leaves cost, which is the one case where the guess is
        worthless and the honest move is to buy the first measurement."""
        with self._lock:
            return self._charges > 0

    def charge_tokens(self, tokens_in: int, tokens_out: int) -> bool:
        """Record one collected leaf's cost. Charged even past the total: the
        call already happened, and hiding it would understate the real spend.

        A leaf that reported nothing (a dead one) is not counted as a measured
        leaf — averaging its zero in would make every leaf look cheaper than the
        ones this run actually pays for.

        Returns True for the ONE charge that CROSSES the ceiling (issue #71):
        computed inside the lock, so under a pipeline's concurrent ``on_done``
        workers exactly one of them sees it and the run gets one advisory fault
        per crossing rather than one per leaf that lands afterwards."""
        with self._lock:
            before = self._tokens_in + self._tokens_out
            self._tokens_in += max(0, tokens_in)
            self._tokens_out += max(0, tokens_out)
            if tokens_in > 0 or tokens_out > 0:
                self._charges += 1
            total = self._token_budget
            if total is None:
                return False
            return before <= total < self._tokens_in + self._tokens_out

    @property
    def est_leaf_cost(self) -> int:
        """What one MORE leaf is assumed to cost: this run's own measured average
        once anything has been charged, else the static estimate.

        A resume seeds BOTH halves of that average — the spend and the number of
        cells that produced it (issue #71) — so a resumed run keeps answering
        with its own measured rate instead of falling back to a constant that
        knows nothing about it. The count comes from the cached cells only,
        while the spend takes the larger of the cell and row ledgers, so a run
        that lost uncached leaves reads as MORE expensive per leaf than it was:
        the gate errs toward pausing, never toward spending."""
        with self._lock:
            spent, charges = self._tokens_in + self._tokens_out, self._charges
        if charges <= 0:
            return max(1, EST_TOKENS_PER_LEAF)
        return max(1, spent // charges)

    def affordable_leaves(self) -> int | None:
        """How many more leaves the rest of the budget buys at the estimated
        rate, or None for "no ceiling was asked for" (never a number, so a caller
        cannot accidentally compare against a 0 that means unlimited)."""
        if self._token_budget is None:
            return None
        return self.tokens_remaining // self.est_leaf_cost

    def snapshot(self) -> dict[str, int] | None:
        """{total, spent, remaining, overrun} for the rollup, or None with no
        budget set.

        ``overrun`` is reported ALWAYS, like its unconditional siblings in the
        rollup: a 0 is the positive claim "this run stayed inside its ceiling",
        and a key that appeared only on trouble would let a reader take silence
        for proof (issue #71)."""
        if self._token_budget is None:
            return None
        return {
            "total": self._token_budget,
            "spent": self.tokens_spent,
            "remaining": self.tokens_remaining,
            "overrun": self.overrun,
        }
