"""Per-node progress for a workflow run (CC-parity M6) — "where is this run?".

The token budget already proved the pattern: a run in flight has no ``RunResult``
yet, and in flight is exactly when the agent needs to see something. This is the
same live read for the DAG itself — which nodes are done, which one is running,
which are still waiting.

Its own lock, deliberately. The states are written from TWO disciplines: the run
thread (the scalar node loop) and the pipeline's concurrent ``on_done`` workers
reporting settled items. A reader (the agent's status call, on a third thread)
must never see a half-written map, and ``snapshot`` must never hand out the live
structures — it builds fresh dicts under the lock, so a caller mutating what it
got cannot corrupt the run's own bookkeeping.

Scope: ONE engine's own DAG. A nested `workflow` node runs on a child engine with
a tracker of its own, so the parent reports that node as a single running/settled
node — unlike the rollup's ``nodes_total``, which folds the nested counts in.
"""

from __future__ import annotations

import threading
from typing import Any

PENDING = "pending"
RUNNING = "running"
COMPLETE = "complete"
NULL = "null"

# A node is "done" once it has settled, however it settled. Counting a nulled
# node as pending would leave a terminal run reporting work that will never
# happen; the per-node state still says ``null``, so done never means "went well".
_SETTLED = (COMPLETE, NULL)


class ProgressTracker:
    """Thread-safe map of node id -> state, plus per-item counts for fan-outs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._order: list[str] = []
        self._states: dict[str, str] = {}
        self._items: dict[str, tuple[int, int]] = {}

    def reset(self, node_ids: list[str]) -> None:
        """Start a run: every node pending, in topological order."""
        with self._lock:
            self._order = list(node_ids)
            self._states = {node_id: PENDING for node_id in self._order}
            self._items = {}

    def mark_running(self, node_id: str) -> None:
        with self._lock:
            if node_id in self._states:
                self._states[node_id] = RUNNING

    def settle(self, node_id: str, output: Any) -> None:
        """Record how a node finished. ``None`` output is a nulled node — the
        distinction the rollup's ``null_rate`` rests on, kept visible here too."""
        with self._lock:
            if node_id in self._states:
                self._states[node_id] = NULL if output is None else COMPLETE

    def note_items(self, node_id: str, done: int, total: int) -> None:
        """Intra-node progress for a fan-out (a ``pipeline``'s settled items).

        Called from the pipeline's concurrent workers, so it takes the lock like
        everything else — and never blocks on anything but this lock.

        MONOTONIC: those workers read their count under the pipeline's own lock
        and publish here after releasing it, so two of them can land out of
        order. A settled count only ever grows, so a smaller report is a
        straggler — taking it would leave a finished fan-out permanently
        claiming fewer items than it settled."""
        with self._lock:
            if node_id not in self._states:
                return
            current = self._items.get(node_id)
            if current is not None and current[0] >= done:
                return
            self._items[node_id] = (max(0, done), max(0, total))

    def snapshot(self) -> dict[str, Any]:
        """{total, done, running, pending, nodes:[{id, state, items?}]} — freshly
        built under the lock, so nothing shared escapes."""
        with self._lock:
            nodes: list[dict[str, Any]] = []
            counts = {PENDING: 0, RUNNING: 0, COMPLETE: 0, NULL: 0}
            for node_id in self._order:
                state = self._states.get(node_id, PENDING)
                counts[state] += 1
                entry: dict[str, Any] = {"id": node_id, "state": state}
                items = self._items.get(node_id)
                if items is not None:
                    entry["items"] = {"done": items[0], "total": items[1]}
                nodes.append(entry)
            return {
                "total": len(self._order),
                "done": sum(counts[state] for state in _SETTLED),
                "running": counts[RUNNING],
                "pending": counts[PENDING],
                "nodes": nodes,
            }
