"""The unified workflow budget — concurrency width, leaf lifetime, token spend.

Bounded BY CONSTRUCTION, never unbounded. Width and the per-run leaf lifetime are
counted here (``check_fanout``, consulted before every fan-out); the TOKEN budget
(spec §7.1) is the third axis: an operator-set ceiling on what a whole run may
cost, charged from every leaf the engine collects.

The token gate is deliberately SOFT — it is read before a spawn, never mid-call:
a leaf already in flight is work already paid for, so it finishes and is charged
even if that overruns the total. Refusing the NEXT spawn is what bounds the run.
Overrun therefore means ``tokens_spent`` can exceed ``token_budget``;
``tokens_remaining`` clamps at zero, ``tokens_spent`` stays honest.

Still out of scope: the process-global agent semaphore (§7.3) — no cap spans
concurrent runs today.
"""

from __future__ import annotations

import threading

DEFAULT_POOL_WIDTH = 4
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


class FanoutRejected(Exception):
    """A fan-out exceeded the budget — rejected and logged, never silently capped."""


class TokenBudgetExhausted(Exception):
    """A leaf spawn was refused: the run has spent its whole token budget.

    Raised by the engine's spawn funnel AFTER it latches the run's pause, so
    callers only have to settle the cell they were about to start.
    """


class Budget:
    def __init__(
        self,
        *,
        pool_width: int = DEFAULT_POOL_WIDTH,
        max_fanout: int = DEFAULT_MAX_FANOUT,
        lifetime: int = DEFAULT_LIFETIME,
        token_budget: int | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self.pool_width = max(1, pool_width)
        self.max_fanout = max(1, max_fanout)
        self._lifetime = max(1, lifetime)
        self._spawned = 0
        # None = no token ceiling asked for (the pre-M5 behaviour, unchanged).
        # A non-zero start is a RESUME picking up where the paused run stopped.
        self._token_budget = token_budget
        self._tokens_in = max(0, tokens_in)
        self._tokens_out = max(0, tokens_out)
        self._charges = 0  # leaves whose real cost this run has measured
        self._lock = threading.Lock()

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

    def charge_tokens(self, tokens_in: int, tokens_out: int) -> None:
        """Record one collected leaf's cost. Charged even past the total: the
        call already happened, and hiding it would understate the real spend.

        A leaf that reported nothing (a dead one) is not counted as a measured
        leaf — averaging its zero in would make every leaf look cheaper than the
        ones this run actually pays for."""
        with self._lock:
            self._tokens_in += max(0, tokens_in)
            self._tokens_out += max(0, tokens_out)
            if tokens_in > 0 or tokens_out > 0:
                self._charges += 1

    @property
    def est_leaf_cost(self) -> int:
        """What one MORE leaf is assumed to cost: this run's own measured average
        once anything has been charged, else the static estimate.

        A resume starts with a seeded spend but no measurement count, so it falls
        back to the estimate until its first leaf lands. Accepted (v1): the cost
        per cell is in the cache, but averaging rows written by a different
        stretch of the run is not obviously better than the constant."""
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
        """{total, spent, remaining} for the rollup, or None with no budget set."""
        if self._token_budget is None:
            return None
        return {
            "total": self._token_budget,
            "spent": self.tokens_spent,
            "remaining": self.tokens_remaining,
        }
