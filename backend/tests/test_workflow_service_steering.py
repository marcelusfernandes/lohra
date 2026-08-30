"""Contract tests for ``WorkflowService.steer`` — the minimal harness.

The service is built with ``object.__new__`` and only the attributes the
steer path touches (``_runs``, ``_lock``, ``_audit_enabled``, ``_audit``);
state/core/engine are fakes, but the steering budget is the REAL
``SteeringLimits`` so reservation/settlement counters are exercised for
true.
"""

import json
import threading
from types import SimpleNamespace

import pytest

from lohra.state import SessionDB
from lohra.workflow.audit import AuditTrail
from lohra.workflow.causality import CausalContext
from lohra.workflow.service import WorkflowService
from lohra.workflow.steering import SteeringLimits
from lohra.workflow.supervision import MAX_STEER_CHARS

RUN = "run-1"
SEG = "seg-1"


def make_ctx(run_id: str = RUN, segment_id: str = SEG) -> CausalContext:
    return CausalContext(
        run_id=run_id,
        segment_id=segment_id,
        node_path=("node",),
        cell_id="cell-1",
        role="leaf",
    )


class FakeCore:
    """Records steer_active calls; can fire on_settle before returning."""

    def __init__(self, ctx, *, steer_result=None, settle_in_call=None):
        self.ctx = ctx
        self.steer_result = (
            steer_result if steer_result is not None else {"ok": True, "queued": False}
        )
        self.settle_in_call = settle_in_call
        self.calls = []
        self.captured_on_settle = None

    def causal_snapshot(self, sub_id):
        if self.ctx is None:
            return None
        return {
            "causal_context": self.ctx,
            "causal_history": (self.ctx,),
            "causal_history_dropped": 0,
        }

    def steer_active(self, sub_id, text, *, expected_causal=None, on_settle=None):
        self.calls.append((sub_id, text))
        if expected_causal is not None and expected_causal != self.ctx:
            return {"error": "causal occurrence changed"}
        self.captured_on_settle = on_settle
        if self.settle_in_call is not None:
            on_settle(self.settle_in_call)
        return self.steer_result


class FakeAudit:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)
        return True


class FakeBudget:
    """Durable steering-budget seam: counts one run-wide slot in memory."""

    def __init__(self):
        self.used = 0

    def steering_reserve(self, run_id: str, *, limit: int) -> tuple[bool, int]:
        if self.used >= limit:
            return False, self.used
        self.used += 1
        return True, self.used

    def steering_release(self, run_id: str) -> bool:
        if self.used <= 0:
            return False
        self.used -= 1
        return True

    def steering_used(self, run_id: str) -> int:
        return self.used


def make_state(core=None, *, status="running", fenced=False, limits=None):
    core = core if core is not None else FakeCore(make_ctx())
    engine = SimpleNamespace(segment_id=SEG, steering_limits=limits or SteeringLimits())
    return SimpleNamespace(run_id=RUN, status=status, core=core, engine=engine, fenced=fenced)


def make_service(state=None, *, audit_enabled=True, budget=None):
    svc = object.__new__(WorkflowService)
    svc._runs = {state.run_id: state} if state is not None else {}
    svc._lock = threading.Lock()
    svc._audit_enabled = audit_enabled
    svc._audit = FakeAudit()
    svc._db = budget or FakeBudget()
    return svc


def steer(
    svc,
    run_id=RUN,
    sub_id="leaf-a",
    text="hello",
    *,
    segment_id=SEG,
    attempt=0,
    turn=0,
):
    return svc.steer(
        run_id,
        sub_id,
        text,
        segment_id=segment_id,
        attempt=attempt,
        turn=turn,
    )


def event_types(svc):
    return [e["event_type"] for e in svc._audit.events]


def assert_text_absent(svc, text):
    blob = json.dumps(svc._audit.events, default=str)
    assert text not in blob


# -- text validation ------------------------------------------------------


class TestTextValidation:
    def test_empty_text_refused(self):
        svc = make_service(make_state())
        for bad in ("", "   ", "\n\t "):
            out = steer(svc, RUN, "leaf-a", bad)
            assert "error" in out
            assert "non-empty" in out["error"]
        assert svc._audit.events == []

    def test_non_string_text_refused(self):
        svc = make_service(make_state())
        out = steer(svc, RUN, "leaf-a", None)
        assert "error" in out
        assert "non-empty" in out["error"]

    def test_oversize_text_refused(self):
        svc = make_service(make_state())
        out = steer(svc, RUN, "leaf-a", "x" * (MAX_STEER_CHARS + 1))
        assert "error" in out
        assert "too long" in out["error"]
        assert svc._audit.events == []

    def test_exactly_max_chars_passes_validation(self):
        svc = make_service(make_state())
        out = steer(svc, RUN, "leaf-a", "x" * MAX_STEER_CHARS)
        assert out["ok"] is True


# -- run lookup -----------------------------------------------------------


class TestRunLookup:
    def test_unknown_run_refused(self):
        svc = make_service(None)
        out = steer(svc, "missing", "leaf-a", "hello")
        assert "error" in out
        assert "no workflow run 'missing'" in out["error"]

    def test_fenced_run_refused(self):
        svc = make_service(make_state(fenced=True))
        out = steer(svc, RUN, "leaf-a", "hello")
        assert "error" in out
        # A fenced-out owner has nothing true left to say about the run.
        assert "no workflow run" in out["error"]

    def test_terminal_run_refused(self):
        for status in ("complete", "failed", "cancelled", "paused"):
            svc = make_service(make_state(status=status))
            out = steer(svc, RUN, "leaf-a", "hello")
            assert "error" in out
            assert "is not running" in out["error"]
            assert status in out["error"]

    def test_missing_core_or_engine_refused(self):
        state = make_state()
        state.core = None
        out = steer(make_service(state), RUN, "leaf-a", "hello")
        assert "error" in out
        assert "no live engine/core" in out["error"]

        state = make_state()
        state.engine = None
        out = steer(make_service(state), RUN, "leaf-a", "hello")
        assert "error" in out
        assert "no live engine/core" in out["error"]


# -- causal identity -------------------------------------------------------


class TestCausalIdentity:
    def test_missing_causal_context_refused(self):
        state = make_state(FakeCore(None))
        out = steer(make_service(state), RUN, "leaf-a", "hello")
        assert "error" in out
        assert "no causal identity" in out["error"]

    def test_wrong_run_refused(self):
        state = make_state(FakeCore(make_ctx(run_id="other-run")))
        out = steer(make_service(state), RUN, "leaf-a", "hello")
        assert "error" in out
        assert "does not belong to run" in out["error"]
        assert out["causal_run_id"] == "other-run"

    def test_stale_segment_attempt_or_turn_refused_before_budget(self):
        for overrides in (
            {"segment_id": "old-seg"},
            {"attempt": 1},
            {"turn": 1},
        ):
            svc = make_service(make_state())
            out = steer(svc, **overrides)
            assert "occurrence changed" in out["error"]
            assert out["stale"] is True
            assert svc._db.used == 0
            assert svc._audit.events == []

    def test_context_segment_that_is_not_engine_segment_refused(self):
        state = make_state(FakeCore(make_ctx(segment_id="old-seg")))
        out = steer(make_service(state), segment_id="old-seg")
        assert "does not belong to run" in out["error"]
        assert out["causal_segment_id"] == "old-seg"


# -- budget + injection ----------------------------------------------------


class TestExhaustion:
    def test_refused_with_counters_and_audited(self):
        limits = SteeringLimits(max_external_per_leaf=1)
        # Spend the leaf's only external slot out-of-band.
        assert limits.reserve_external("leaf-a").accepted is True
        assert limits.settle_external("leaf-a", "read") is True

        state = make_state(limits=limits)
        svc = make_service(state)
        out = steer(svc, RUN, "leaf-a", "hello")

        assert "error" in out
        assert "exhausted" in out["error"]
        assert out["exhausted"] is True
        assert out["reason"] == "leaf_limit"
        assert out["leaf_used"] == 1
        assert out["run_used"] == 1
        assert out["corrections_used"] == 1

        # Audited with numeric counters only, no injection ever happened.
        assert event_types(svc) == ["steering.exhausted"]
        data = svc._audit.events[0]["data"]
        assert data["reason"] == "leaf_limit"
        counters = {key: value for key, value in data.items() if key != "reason"}
        assert set(counters) == {"leaf_used", "run_used", "corrections_used"}
        assert all(isinstance(v, int) for v in counters.values())
        assert_text_absent(svc, "hello")


class TestSuccess:
    def test_success_contract_and_accepted_before_read_order(self):
        # on_settle fires synchronously INSIDE steer_active, i.e. before the
        # service has marked the steer accepted: the outcome must park, and
        # the flush must land AFTER the accepted event.
        core = FakeCore(make_ctx(), settle_in_call="read")
        svc = make_service(make_state(core))
        out = steer(svc, RUN, "leaf-a", "hello world")

        assert out["ok"] is True
        assert out["queued"] is False
        # steer_active received the exact instruction and a settle callback.
        assert core.calls == [("leaf-a", "hello world")]
        assert callable(core.captured_on_settle)

        identity = out["identity"]
        assert identity["run_id"] == RUN
        assert identity["segment_id"] == SEG
        assert out["receipts"] == {
            "kind": "external",
            "leaf_used": 1,
            "run_used": 1,
            "corrections_used": 1,
        }

        assert event_types(svc) == ["steering.accepted", "steering.read"]
        accepted = svc._audit.events[0]
        assert accepted["data"] == {
            "leaf_used": 1,
            "run_used": 1,
            "corrections_used": 1,
        }
        assert_text_absent(svc, "hello world")

    def test_read_settlement_keeps_counters_spent(self):
        core = FakeCore(make_ctx())
        svc = make_service(make_state(core))
        out = steer(svc, RUN, "leaf-a", "hello")
        assert out["ok"] is True

        # "Later, from the core's thread": the read lands after acceptance.
        core.captured_on_settle("read")
        assert event_types(svc) == ["steering.accepted", "steering.read"]

        # The steer was spent: the leaf stays at its external ceiling.
        refused = state_limits(svc).reserve_external("leaf-a")
        assert refused.accepted is False
        assert refused.reason == "leaf_limit"

    def test_discarded_settlement_restores_counters(self):
        core = FakeCore(make_ctx(), settle_in_call="discarded")
        svc = make_service(make_state(core))
        out = steer(svc, RUN, "leaf-a", "hello")
        assert out["ok"] is True

        assert event_types(svc) == ["steering.accepted", "steering.discarded"]
        # The steer never landed: the slot went back to the budget.
        again = state_limits(svc).reserve_external("leaf-a")
        assert again.accepted is True
        assert again.leaf_used == 1
        assert again.run_used == 1
        assert again.corrections_used == 1


class TestCoreRejection:
    def test_rejection_rolls_back_reservation_and_audits_rejected(self):
        core = FakeCore(make_ctx(), steer_result={"error": "sub-session not accepting"})
        svc = make_service(make_state(core))
        out = steer(svc, RUN, "leaf-a", "hello")

        assert "error" in out
        assert "steer rejected by orchestration" in out["error"]
        assert out["rolled_back"] is True

        # The refused steer never spends a slot: a fresh reserve succeeds and
        # the counters read as if nothing had been reserved before.
        again = state_limits(svc).reserve_external("leaf-a")
        assert again.accepted is True
        assert again.leaf_used == 1
        assert again.corrections_used == 1
        assert again.run_used == 1

        assert event_types(svc) == ["steering.rejected"]
        assert svc._audit.events[0]["data"] == {}  # no reason/text in the audit
        assert_text_absent(svc, "hello")


def state_limits(svc):
    return svc._runs[RUN].engine.steering_limits


# -- audit disabled --------------------------------------------------------


class TestAuditDisabled:
    def test_disabled_audit_still_steers(self):
        core = FakeCore(make_ctx(), settle_in_call="read")
        svc = make_service(make_state(core), audit_enabled=False)
        out = steer(svc, RUN, "leaf-a", "hello")
        assert out["ok"] is True
        assert svc._audit.events == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])


# -- the REAL audit path: WorkflowService._steer_audit -> AuditTrail -> SQLite


class TestRealAuditPersistence:
    def test_accepted_then_read_persisted_in_order_without_text(self, tmp_path):
        db = SessionDB(str(tmp_path / "state.db"))
        trail = AuditTrail(db, enabled=True)
        try:
            core = FakeCore(make_ctx(), settle_in_call="read")
            svc = make_service(make_state(core))
            svc._audit = trail  # same seam, real sink
            out = steer(svc, RUN, "leaf-a", "steer me once")
            assert out["ok"] is True
            assert trail.flush(timeout=5.0) is True

            page = db.audit_query(RUN, limit=20)
            assert page["availability"] == "available"
            types = [event["event_type"] for event in page["events"]]
            assert types == ["steering.accepted", "steering.read"]

            # The steering counters are allow-listed data: they persist as
            # the plain ints the service sent, not as redaction markers.
            accepted = page["events"][0]
            assert accepted["data"] == {
                "leaf_used": 1,
                "run_used": 1,
                "corrections_used": 1,
            }
            assert all(event["identity"]["sub_id"] == "leaf-a" for event in page["events"])

            # The instruction text never entered the serialized payload.
            blob = json.dumps(page, default=str)
            assert "steer me once" not in blob
        finally:
            trail.shutdown()
            db.close()


# -- durable run budget: a REAL SessionDB outlives the service and the process


class TestDurableRunBudget:
    def test_run_budget_persists_across_reopen(self, tmp_path):
        path = str(tmp_path / "state.db")

        # Two accepted steers against the same durable budget.
        db1 = SessionDB(path)
        try:
            for i in range(2):
                core = FakeCore(make_ctx(), settle_in_call="read")
                svc = make_service(make_state(core), budget=db1)
                out = steer(svc, RUN, f"leaf-{i}", "hello")
                assert out["ok"] is True
                assert out["receipts"]["run_used"] == i + 1
        finally:
            db1.close()

        # Reopen: the run's spend survived the close, so the third steer is
        # accepted (ceiling 3) and the fourth is refused as exhausted.
        db2 = SessionDB(path)
        try:
            core = FakeCore(make_ctx(), settle_in_call="read")
            svc = make_service(make_state(core), budget=db2)
            out = steer(svc, RUN, "leaf-2", "hello")
            assert out["ok"] is True
            assert out["receipts"]["run_used"] == 3

            core = FakeCore(make_ctx(), settle_in_call="read")
            svc = make_service(make_state(core), budget=db2)
            out = steer(svc, RUN, "leaf-3", "hello")
            assert "error" in out
            assert out["exhausted"] is True
            assert out["reason"] == "run_limit"
            assert out["run_used"] == 3
        finally:
            db2.close()

    def test_discarded_settlement_releases_exactly_once(self):
        # One shared FakeBudget behind every service in this test: the
        # durable half of the run ceiling, exercised for real.
        budget = FakeBudget()

        # A steer whose outcome never arrives inside the call: the durable
        # slot stays spent while the steer is in flight.
        core = FakeCore(make_ctx())
        svc = make_service(make_state(core), budget=budget)
        out = steer(svc, RUN, "leaf-a", "hello")
        assert out["ok"] is True
        assert budget.steering_used(RUN) == 1

        # The core reports the steer never landed — twice, adversarially.
        # The first settles the open reservation and releases the durable
        # slot; the second finds no open local slot settled and must not
        # release anything: the release is exact-once.
        core.captured_on_settle("discarded")
        assert budget.steering_used(RUN) == 0
        core.captured_on_settle("discarded")
        assert budget.steering_used(RUN) == 0

        # The slot truly went back: against the SAME shared budget, three
        # fresh services (fresh local counters, read-settled in-call) are
        # accepted with distinct leaves, taking the run to its ceiling of 3.
        for i in range(3):
            read_core = FakeCore(make_ctx(), settle_in_call="read")
            fresh = make_service(make_state(read_core), budget=budget)
            out = steer(fresh, RUN, f"leaf-{i}", "hello")
            assert out["ok"] is True
            assert out["receipts"]["run_used"] == i + 1
        assert budget.steering_used(RUN) == 3

        # The fourth is refused by the durable run ceiling alone.
        fourth = FakeCore(make_ctx(), settle_in_call="read")
        fresh = make_service(make_state(fourth), budget=budget)
        out = steer(fresh, RUN, "leaf-3", "hello")
        assert "error" in out
        assert out["exhausted"] is True
        assert out["reason"] == "run_limit"
        assert out["run_used"] == 3
        assert budget.steering_used(RUN) == 3
