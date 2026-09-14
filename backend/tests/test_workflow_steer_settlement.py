"""#69: actual service/core supervision settles through SQLite without Core locks."""

import json
from threading import Event

import pytest

from lohra.agent.agent import Agent
from lohra.gateway.session import GatewaySession
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService
from tests.test_loop import FakeClient, _text_response
from tests.test_workflow_quota import TimerFactory


@pytest.mark.parametrize("discarded", [False, True])
def test_service_settlement_and_budget_survive_sqlite_reopen(tmp_path, monkeypatch, discarded):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    path = str(tmp_path / "state.db")
    db = SessionDB(path)
    client = FakeClient([_text_response("first"), _text_response("followup")])
    entered, release = Event(), Event()
    original_submit = GatewaySession.submit

    def paused_submit(self, text, emit):
        result = original_submit(self, text, emit)
        if not entered.is_set():
            entered.set()  # after loop/persistence, before Core finalization
            assert release.wait(5)
        return result

    monkeypatch.setattr(GatewaySession, "submit", paused_submit)
    service = WorkflowService(
        base_child_factory=lambda: Agent(
            model="synthetic",
            provider=get_provider_profile("anthropic"),
            client=client,
        ),
        db=db,
        home=tmp_path,
        lease_timer_factory=TimerFactory(),
    )
    observations, releases, snapshots = [], [], []
    try:
        run_id = service.start(
            {
                "meta": {"name": "settlement", "version": 1},
                "nodes": [{"id": "a", "type": "agent", "prompt": "initial"}],
            },
            {},
        )["run_id"]
        assert entered.wait(5)
        state = service._get(run_id)
        core = state.core
        sid = next(iter(core._children))
        session = core._children[sid].session
        ctx = core.causal_snapshot(sid)["causal_context"]
        original_audit, original_release = service._steer_audit, db.steering_release

        def observe(kind):
            observations.append((kind, core._lock.locked(), session._inbox_lock.locked()))
            if not core._lock.locked():
                snapshots.append(core.causal_snapshot(sid)["causal_context"])

        def audit(kind, *args, **kwargs):
            observe(kind)
            return original_audit(kind, *args, **kwargs)

        def refund(target):
            observe("sqlite.release")
            released = original_release(target)
            releases.append(released)
            return released

        monkeypatch.setattr(service, "_steer_audit", audit)
        monkeypatch.setattr(db, "steering_release", refund)
        instruction = "private-steer-canary"
        accepted = service.steer(
            run_id, sid, instruction, segment_id=ctx.segment_id, attempt=ctx.attempt, turn=ctx.turn
        )
        assert accepted["ok"] is True and accepted["queued"] is True
        assert db.steering_used(run_id) == 1
        if discarded:
            # Exercise Core's epilogue discard, not service._stop_all's early discard.
            core.cancel(sid)
        release.set()
        service.status(run_id, wait=True, timeout=5)
        assert service._audit.flush(timeout=5)
        assert releases == ([True] if discarded else [])
        assert observations and all(row[1:] == (False, False) for row in observations)
        assert snapshots == [ctx] * len(observations)
        assert len(client.calls) == (1 if discarded else 2)
        assert session.drain_steers() == []
        core.cancel(sid)
        core.shutdown(wait=False)
        assert releases == ([True] if discarded else [])
    finally:
        release.set()
        service.shutdown()
        db.close()
    reopened = SessionDB(path)
    try:
        assert reopened.steering_used(run_id) == (0 if discarded else 1)
        page = reopened.audit_query(run_id, limit=100)
        events = [event for event in page["events"] if event["event_type"].startswith("steering.")]
        assert [event["event_type"] for event in events] == [
            "steering.accepted",
            "steering.discarded" if discarded else "steering.read",
        ]
        assert instruction not in json.dumps(events)
    finally:
        reopened.close()
