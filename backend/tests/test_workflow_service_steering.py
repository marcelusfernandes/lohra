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

    def steer_active(self, sub_id, text, *, on_settle=None):
        self.calls.append((sub_id, text))
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


def make_state(core=None, *, status="running", fenced=False, limits=None):
    core = core if core is not None else FakeCore(make_ctx())
    engine = SimpleNamespace(segment_id=SEG, steering_limits=limits or SteeringLimits())
    return SimpleNamespace(run_id=RUN, status=status, core=core, engine=engine, fenced=fenced)


def make_service(state=None, *, audit_enabled=True):
    svc = object.__new__(WorkflowService)
    svc._runs = {state.run_id: state} if state is not None else {}
    svc._lock = threading.Lock()
    svc._audit_enabled = audit_enabled
    svc._audit = FakeAudit()
    return svc


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
            out = svc.steer(RUN, "leaf-a", bad)
            assert "error" in out
            assert "non-empty" in out["error"]
        assert svc._audit.events == []

    def test_non_string_text_refused(self):
        svc = make_service(make_state())
        out = svc.steer(RUN, "leaf-a", None)
        assert "error" in out
        assert "non-empty" in out["error"]

    def test_oversize_text_refused(self):
        svc = make_service(make_state())
        out = svc.steer(RUN, "leaf-a", "x" * (MAX_STEER_CHARS + 1))
        assert "error" in out
        assert "too long" in out["error"]
        assert svc._audit.events == []

    def test_exactly_max_chars_passes_validation(self):
        svc = make_service(make_state())
        out = svc.steer(RUN, "leaf-a", "x" * MAX_STEER_CHARS)
        assert out["ok"] is True


# -- run lookup -----------------------------------------------------------


class TestRunLookup:
    def test_unknown_run_refused(self):
        svc = make_service(None)
        out = svc.steer("missing", "leaf-a", "hello")
        assert "error" in out
        assert "no workflow run 'missing'" in out["error"]

    def test_fenced_run_refused(self):
        svc = make_service(make_state(fenced=True))
        out = svc.steer(RUN, "leaf-a", "hello")
        assert "error" in out
        # A fenced-out owner has nothing true left to say about the run.
        assert "no workflow run" in out["error"]

    def test_terminal_run_refused(self):
        for status in ("complete", "failed", "cancelled", "paused"):
            svc = make_service(make_state(status=status))
            out = svc.steer(RUN, "leaf-a", "hello")
            assert "error" in out
            assert "is not running" in out["error"]
            assert status in out["error"]

    def test_missing_core_or_engine_refused(self):
        state = make_state()
        state.core = None
        out = make_service(state).steer(RUN, "leaf-a", "hello")
        assert "error" in out
        assert "no live engine/core" in out["error"]

        state = make_state()
        state.engine = None
        out = make_service(state).steer(RUN, "leaf-a", "hello")
        assert "error" in out
        assert "no live engine/core" in out["error"]


# -- causal identity -------------------------------------------------------


class TestCausalIdentity:
    def test_missing_causal_context_refused(self):
        state = make_state(FakeCore(None))
        out = make_service(state).steer(RUN, "leaf-a", "hello")
        assert "error" in out
        assert "no causal identity" in out["error"]

    def test_wrong_run_refused(self):
        state = make_state(FakeCore(make_ctx(run_id="other-run")))
        out = make_service(state).steer(RUN, "leaf-a", "hello")
        assert "error" in out
        assert "does not belong to run" in out["error"]
        assert out["causal_run_id"] == "other-run"

    def test_wrong_segment_refused(self):
        state = make_state(FakeCore(make_ctx(segment_id="old-seg")))
        out = make_service(state).steer(RUN, "leaf-a", "hello")
        assert "error" in out
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
        out = svc.steer(RUN, "leaf-a", "hello")

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
        assert set(data) == {"leaf_used", "run_used", "corrections_used"}
        assert all(isinstance(v, int) for v in data.values())
        assert_text_absent(svc, "hello")


class TestSuccess:
    def test_success_contract_and_accepted_before_read_order(self):
        # on_settle fires synchronously INSIDE steer_active, i.e. before the
        # service has marked the steer accepted: the outcome must park, and
        # the flush must land AFTER the accepted event.
        core = FakeCore(make_ctx(), settle_in_call="read")
        svc = make_service(make_state(core))
        out = svc.steer(RUN, "leaf-a", "hello world")

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
        out = svc.steer(RUN, "leaf-a", "hello")
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
        out = svc.steer(RUN, "leaf-a", "hello")
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
        out = svc.steer(RUN, "leaf-a", "hello")

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
        out = svc.steer(RUN, "leaf-a", "hello")
        assert out["ok"] is True
        assert svc._audit.events == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
