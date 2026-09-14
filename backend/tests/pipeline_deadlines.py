"""Event-driven deadlines for tests that require a specific live-leaf order."""

from lohra.orchestration.core import OrchestrationCore
from lohra.workflow import strategies


def control_pipeline_deadlines(monkeypatch, deadlines):
    """Select when the real pipeline wait expires, after the test's prerequisites.

    ``deadlines`` maps local node ids to Events. A selected node waits for its
    Event, then observes its real completion flag with a zero-time wait: only an
    unfinished pipeline enters the real _expire/cleanup/seal path. Other nodes
    must finish normally. Clear the mapping before resume to test recovery
    without reusing the first stretch's deliberately expired deadline.

    The five-second waits are harness failure watchdogs, not workflow timeouts.
    Only each pipeline's own barrier Event is patched, never threading.Event
    globally, the scheduler, Core acceptance, callbacks, or accounting.
    """
    init = strategies._PipelineRun.__init__

    def controlled_init(pipeline, *args, **kwargs):
        init(pipeline, *args, **kwargs)
        deadline = deadlines.get(pipeline._node.id)
        wait = pipeline._done.wait

        def controlled_wait(timeout=None):
            if deadline is None:
                assert wait(5), f"{pipeline._node.id}: expected natural pipeline completion"
                return True
            assert deadline.wait(5), f"{pipeline._node.id}: test never released its deadline"
            return wait(0)

        monkeypatch.setattr(pipeline._done, "wait", controlled_wait)

    monkeypatch.setattr(strategies._PipelineRun, "__init__", controlled_init)


def control_scalar_deadlines(monkeypatch, deadlines):
    """Select a scalar collect deadline after the same live-provider prerequisite.

    Only blocking agent collects for the selected node ids are controlled. The
    real Core collect reads the unfinished Future with timeout zero, leaving the
    engine's timeout/cancel/quiescence path intact. Clearing the mapping requires
    natural completion on resume, guarded by the same five-second watchdog.
    Nonblocking accounting observations and other nodes are untouched.
    """
    collect = OrchestrationCore.collect
    selected = frozenset(deadlines)

    def controlled_collect(core, sub_id, *, wait=False, timeout=None):
        snapshot = core.causal_snapshot(sub_id) if wait else None
        context = snapshot["causal_context"] if snapshot else None
        if context is None or context.role != "agent" or context.node_path[-1] not in selected:
            return collect(core, sub_id, wait=wait, timeout=timeout)
        deadline = deadlines.get(context.node_path[-1])
        if deadline is not None:
            assert deadline.wait(5), "test never released its scalar deadline"
        result = collect(core, sub_id, wait=True, timeout=0 if deadline is not None else 5)
        if deadline is None:
            assert result["status"] != "running", "expected natural scalar completion"
        return result

    monkeypatch.setattr(OrchestrationCore, "collect", controlled_collect)
