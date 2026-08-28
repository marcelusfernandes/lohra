"""Live events for a workflow run (WF-30) — the run tells you where it is.

The harness was built poll-friendly: ``ProgressTracker`` and ``Budget`` are read
mid-run by whoever asks (``workflow_status``). That serves the AGENT, which can
ask. It never served the HUMAN who typed ``lohra chat`` and then watched a blank
terminal until the turn was over — including the whole stretch where the CLI is
inside ``shutdown()`` draining the run's pool with nothing printing at all.

This is the push half: one optional sink, five kinds, all of them cheap.

- ``plan``  — the accepted DAG, emitted synchronously inside ``start`` so the
  topology is on screen BEFORE the first leaf spawns;
- ``node``  — a node went pending -> running -> complete|null, with the run's
  counters and spend at that moment;
- ``items`` — a fan-out's settled count, RATE-LIMITED (see below);
- ``fault`` — the fault text, the moment it is recorded rather than at the end;
- ``done``  — how the run really ended.

Three rules the implementation rests on:

**The sink never breaks the run.** It is somebody else's code (a terminal
writer, a gateway push) running on the run thread and on the pipeline's worker
threads. It is called outside every engine lock, its exceptions are swallowed
and logged — the ``on_run_done`` contract, applied to a callback that fires far
more often.

**The limiter's verdict is the durable write's verdict.** ``emit`` returns
whether the event passed the rate limiter, and it returns it whether or not a
sink is attached: the service hangs the run's durable progress write off the
same answer, so what reaches SQLite does not depend on anybody watching. The
width (``done == 0``) and the finish (``done == total``) are never dropped —
a limiter that ate either would leave a finished fan-out reporting 3/4 until the
terminal snapshot papered over it.

**Scope is ONE engine's DAG**, exactly like ``ProgressTracker``: a nested
``workflow`` node runs on a child engine with no sink, so the parent reports it
as the single node it is instead of interleaving two DAGs' counters.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# (run_id, kind, payload) — the sink a consumer attaches (CLI renderer, gateway).
OnEvent = Callable[[str, str, dict], None]

PLAN = "plan"
NODE = "node"
ITEMS = "items"
FAULT = "fault"
DONE = "done"

# A pipeline settles items from concurrent workers; one line per item would bury
# the terminal on a wide fan-out. One per second per node is enough to read.
ITEMS_MIN_INTERVAL = 1.0


class EventEmitter:
    """Serializes a run's live events onto one sink, and rate-limits ``items``.

    Its own lock, for the same reason ``ProgressTracker`` has one: the events are
    written from TWO disciplines (the run thread's node loop and the pipeline's
    concurrent ``on_done`` workers). The lock guards the limiter's bookkeeping
    and is RELEASED before the sink is called — a sink that blocks must never be
    able to hold the run's other threads behind it.
    """

    def __init__(
        self,
        sink: OnEvent | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        items_interval: float = ITEMS_MIN_INTERVAL,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._interval = max(0.0, float(items_interval))
        self._lock = threading.Lock()
        self._last_items: dict[tuple[str, str], float] = {}

    def set_sink(self, sink: OnEvent | None) -> None:
        """Point the live view somewhere. One sink per service."""
        with self._lock:
            self._sink = sink

    def emit(self, run_id: str, kind: str, payload: dict[str, Any]) -> bool:
        """Publish one event. True when it passed the rate limiter — which is
        also the answer to "should this transition reach the durable line?", so
        it is computed even with no sink attached."""
        with self._lock:
            if kind == ITEMS and not self._allow_items(run_id, payload):
                return False
            if kind == DONE:
                # The run is over: its per-node limiter bookkeeping is dead
                # weight in a long-lived process (the dashboard) from here on.
                self._forget(run_id)
            sink = self._sink
        if sink is not None:
            try:
                # A COPY: a sink that mutates what it got must not be able to
                # reach back into the run's own bookkeeping.
                sink(run_id, kind, dict(payload))
            except Exception:  # a broken sink is not a broken run
                logger.exception("workflow: live event sink failed for run %s", run_id)
        return True

    def tracked_nodes(self) -> int:
        """How many fan-out nodes the limiter is still remembering (bounded by
        construction: a run's entries are dropped when it reports ``done``)."""
        with self._lock:
            return len(self._last_items)

    # --- internals (called under the lock) ------------------------------

    def _allow_items(self, run_id: str, payload: dict[str, Any]) -> bool:
        done = int(payload.get("done") or 0)
        total = int(payload.get("total") or 0)
        forced = done <= 0 or (total > 0 and done >= total)
        key = (run_id, str(payload.get("node_id") or ""))
        now = self._clock()
        last = self._last_items.get(key)
        if not forced and last is not None and now - last < self._interval:
            return False
        # A forced event resets the pace too, so the width does not immediately
        # let the next item through.
        self._last_items[key] = now
        return True

    def _forget(self, run_id: str) -> None:
        for key in [key for key in self._last_items if key[0] == run_id]:
            del self._last_items[key]


def plan_payload(
    run_id: str, spec: Any, *, name: str = "", token_budget: int | None = None
) -> dict[str, Any]:
    """The accepted DAG, shaped for a reader — pure, so it is testable alone.

    Built from the VALIDATED spec, in the engine's own execution order, with the
    dependencies the engine really computes (explicit ``depends_on`` UNION the
    roots of every ``${ref}`` the node reads) — otherwise the printed plan would
    disagree with the order the run actually takes.

    Top level only: a ``parallel``/``pipeline``'s real width is a runtime fact
    (``items`` can be a ref), and its per-item progress arrives as ``items``
    events. The plan promises the shape, not the fan-out.
    """
    # Local import purely for symmetry with the rest of this module's lazy
    # helpers; ``graph`` imports only refs/nodes, so there is no cycle to dodge.
    from lohra.workflow.graph import dependencies, topological_order

    node_ids = {node.id for node in spec.nodes}
    nodes: list[dict[str, Any]] = []
    for node in topological_order(spec):
        entry: dict[str, Any] = {
            "id": node.id,
            "type": node.type,
            "depends_on": sorted(dependencies(node, node_ids)),
        }
        # Only what an operator reads as "which model is this costing me?".
        for field in ("tier", "model", "provider", "effort"):
            value = node.fields.get(field)
            if isinstance(value, str) and value:
                entry[field] = value
        nodes.append(entry)
    return {"run_id": run_id, "name": name, "token_budget": token_budget, "nodes": nodes}
