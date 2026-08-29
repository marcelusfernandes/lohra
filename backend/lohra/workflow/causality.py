"""Immutable causal identity for workflow leaf executions (OBS-02).

The workflow layer owns this vocabulary.  ``OrchestrationCore`` only transports
an opaque value alongside a sub-session, so orchestration remains reusable by
delegation and agent-facing background sessions.

``CausalContext`` identifies an execution, not an audit event.  The generated
``sub_id`` and the ordered turns/events inside that sub-session are additional
coordinates supplied by orchestration.  A cache hit has no sub-session at all;
it reuses the same ``cell_id`` and must be recorded by the workflow audit sink as
replay rather than fabricated as a leaf execution.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CausalContext:
    """Workflow-owned coordinates attached before an isolated leaf is spawned.

    ``run_id`` is stable across resume; ``segment_id`` distinguishes each
    execution stretch. ``node_path`` namespaces nested DAGs. ``cell_id`` is the
    content-addressed workflow cell. Fan-out coordinates use ``branch_path``;
    pipelines additionally expose item/stage directly because those are primary
    query dimensions. ``attempt`` orders execution attempts within the same
    cell/role, including a corrective turn; ``turn`` orders turns within one
    sub-session. A busy-inbox steer inherits the turn already in progress.
    """

    run_id: str
    segment_id: str
    node_path: tuple[str, ...]
    cell_id: str
    role: str
    item_index: int | None = None
    stage_index: int | None = None
    branch_path: tuple[int, ...] = ()
    attempt: int = 0
    turn: int = 0
