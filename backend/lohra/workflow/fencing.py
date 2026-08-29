"""The vocabulary of ownership fencing (issue #12) — one sentinel, no cycle.

The fence itself lives in SQLite (``workflow_run_fence``) and the policy lives
in ``runstate_store``; what lives HERE is the one value both the store and the
audit sink have to agree on, in a module neither of them can import in a circle
(``audit`` is imported by the engine, which the store imports back).

``None`` and ``EVICTED`` are the distinction the whole issue turns on:

- ``None`` — this run has no ownership fence at all (a database written before
  fencing shipped, a run nobody ever leased). Write unfenced, exactly as before;
- ``EVICTED`` — the run IS fenced, and this process cannot present the fence.
  REFUSE. A bounded memory that answered ``None`` here would turn its own
  eviction policy into a licence for a stale owner to write unfenced.
"""

from __future__ import annotations

from typing import Any


class _EvictedFence:
    """'I cannot present this run's fence' — never 'this run has no fence'."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<fence unavailable>"


# Singleton: callers compare with ``is``, never by equality.
EVICTED: Any = _EvictedFence()
