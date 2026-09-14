"""Event-driven deadlines for tests that require a specific live-leaf order."""

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
