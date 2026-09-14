"""Stop-origin controls for the pipeline's newly deferred accounting (#111)."""

from copy import deepcopy
from threading import Event

import pytest

from lohra.state import SessionDB
from lohra.workflow import quiescence, strategies
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.runstate_store import RunStateStore
from tests.pipeline_deadlines import control_pipeline_deadlines
from tests.test_workflow_pipeline_accounting import _service
from tests.test_workflow_quota import _DuckError, _rate_limited


@pytest.mark.parametrize("prior", [None, "timeout", "failure"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("reason", ["quota", "route"])
def test_pause_pending_keeps_its_origin_and_prior_failures(tmp_path, monkeypatch, prior, cancel, reason):
    release, started, sealed = Event(), Event(), Event()
    earlier_started, pause_processed = Event(), Event()
    deadlines = {"earlier": Event(), "p": Event()}
    if prior != "timeout":
        del deadlines["earlier"]
    control_pipeline_deadlines(monkeypatch, deadlines)
    seal, hook = WorkflowEngine._seal, strategies._PipelineRun._hook
    first_stretch = True

    def respond(prompt):
        if not first_stretch:
            return "RECOVERED"
        if prompt == "earlier":
            earlier_started.set()
            if prior == "failure":
                raise RuntimeError("independent failure")
            assert release.wait(5)
            return "EARLIER"
        if prompt == "slow":
            started.set()
            assert release.wait(5)
            return "LATE"
        assert prompt == "quota" and started.wait(5)
        if reason == "route":
            raise _DuckError("dead route", status_code=401)
        raise _rate_limited("30")

    def observed_hook(pipeline, cell):
        done = hook(pipeline, cell)

        def observed_done(sub_id):
            done(sub_id)
            if cell.owner_node_id == "p" and cell.index == 1:
                pause_processed.set()

        return observed_done

    def observed_seal(engine, result):
        if cancel:
            engine.request_cancel()
        seal(engine, result)
        sealed.set()

    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.1)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.05)
    monkeypatch.setattr(WorkflowEngine, "_seal", observed_seal)
    monkeypatch.setattr(strategies._PipelineRun, "_hook", observed_hook)
    nodes = []
    if prior:
        nodes.append({"id": "earlier", "type": "pipeline", "items": ["x"],
                      "stages": [{"prompt": "earlier"}]})
    nodes.append({"id": "p", "type": "pipeline", "items": ["slow", "quota"],
                  "stages": [{"prompt": "${item}"}],
                  "depends_on": ["earlier"] if prior else []})
    spec = {"meta": {"name": "pause-pending"}, "nodes": nodes}
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, respond)
    try:
        run_id = svc.start(spec)["run_id"]
        state = svc._runs[run_id]
        if prior == "timeout":
            assert earlier_started.wait(5)  # unknown live usage, not a queued refund
            deadlines["earlier"].set()
        assert started.wait(5) and pause_processed.wait(5)
        assert state.engine.paused
        deadlines["p"].set()  # pause already owns this stop, before cleanup
        assert sealed.wait(5)
        before = deepcopy(state.engine._result)
        assert before.status == ("cancelled" if cancel else "paused")
        if not cancel:
            assert before.pause_reason == ("quota_exhausted" if reason == "quota" else "route_fault")
        assert before.usage_uncertain_leaves == (2 if prior == "timeout" else 1)
        # A later global pause must never relabel an independent earlier fault.
        assert not any(f.startswith("earlier") for f in before.pause_faults)
        if not cancel:
            assert any("p: leaf still running at seal" in f for f in before.pause_faults)
            assert any("p: pipeline timed out" in f for f in before.pause_faults)
        release.set()
        state.future.result(timeout=5)
        assert state.engine._result == before
        if cancel:
            return
        first_stretch = False
        deadlines.clear()  # recovery must finish; no induced deadline this stretch
        svc.shutdown()
        db.close()
        db = SessionDB(tmp_path / "state.db")
        row = RunStateStore(db, holder="read-only-control").load(run_id)
        assert row.prior_degraded is bool(prior)
        svc = _service(db, tmp_path, respond)
        assert "error" not in svc.start(None, resume_run_id=run_id)
        svc._runs[run_id].future.result(timeout=5)
        # Per-stretch status can be complete with a genuinely degraded history;
        # the carried flag (used for certification) is the cross-stretch oracle.
        assert svc.status(run_id)["status"] == "complete"
        assert svc._runs[run_id].prior_degraded is bool(prior)
    finally:
        for deadline in deadlines.values():
            deadline.set()
        release.set()
        svc.shutdown()
        db.close()
