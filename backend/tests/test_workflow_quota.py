"""Provider quota exhaustion: pause + auto-resume (CC-parity M4, fatia B).

A 429 is not "the leaf died": every leaf after it dies the same way, so a run
that keeps scheduling burns the whole spec into a cascade of nulls and then
teaches ``library`` that the SHAPE was bad. The honest outcome is to stop, keep
the completed cells in the resume cache, report ``paused`` with the reason, and
come back later.

Four layers, tested bottom-up:
- classification: a 429 (SDK object or duck-typed) and a Responses ``code`` are
  ``quota_exhausted``; ordinary errors are NOT (never sniff prose);
- propagation: loop -> sub-session -> collect;
- engine/service: pause instead of nulling, skip ``record_outcome``;
- auto-resume: a timer re-calls ``start(resume_run_id=...)``, bounded and
  cancellable. Timers and the clock are INJECTED — no real sleeps here.
"""

import threading

import anthropic
import httpx
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.loop import run_conversation
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.providers.errors import (
    QUOTA_EXHAUSTED,
    ProviderCallFailed,
    classify_provider_error,
    retry_after_seconds,
)
from lohra.state import SessionDB
from lohra.workflow import library, strategies
from lohra.workflow.autoresume import (
    MAX_RESUME_ATTEMPTS,
    MAX_RESUME_DELAY,
    MIN_RESUME_DELAY,
    AutoResumeScheduler,
    resume_delay,
)
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService, _is_live
from tests.test_loop import FakeClient, _text_response
from tests.test_workflow_pipeline import ScriptedClient


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _http_error(cls, status, *, retry_after=None):
    request = httpx.Request("POST", "https://api.example.com/v1/messages")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(status, request=request, headers=headers)
    return cls("rate limited", response=response, body=None)


def _rate_limited(retry_after="30"):
    return _http_error(anthropic.RateLimitError, 429, retry_after=retry_after)


class _DuckError(Exception):
    """No SDK involved: only the shape the classifier duck-types on."""

    def __init__(self, message, *, status_code=None, code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after


# --- 1. classification -------------------------------------------------


def test_sdk_rate_limit_errors_are_quota():
    assert classify_provider_error(_rate_limited()) == QUOTA_EXHAUSTED
    assert classify_provider_error(_http_error(openai.RateLimitError, 429)) == QUOTA_EXHAUSTED


def test_duck_typed_429_is_quota():
    assert classify_provider_error(_DuckError("nope", status_code=429)) == QUOTA_EXHAUSTED


def test_responses_failure_code_is_quota():
    exc = ProviderCallFailed("Responses API failed: usage_limit_reached out of credit",
                             code="usage_limit_reached")
    assert classify_provider_error(exc) == QUOTA_EXHAUSTED


def test_responses_stream_failure_keeps_the_code_on_the_exception():
    """The Responses backend reports quota as an error CODE with no HTTP status;
    formatting it into the message would throw away the only usable signal."""
    from lohra.agent.client import assemble_responses_stream

    event = {"type": "response.failed",
             "response": {"error": {"code": "usage_limit_reached", "message": "no credit"}}}
    with pytest.raises(ProviderCallFailed) as caught:
        assemble_responses_stream([event])
    assert caught.value.code == "usage_limit_reached"
    assert classify_provider_error(caught.value) == QUOTA_EXHAUSTED


def test_ordinary_errors_are_not_quota():
    assert classify_provider_error(RuntimeError("boom")) is None
    assert classify_provider_error(_http_error(anthropic.APIStatusError, 500)) is None
    assert classify_provider_error(_DuckError("server", status_code=503)) is None


def test_prose_mentioning_rate_limit_is_not_classified():
    # Never classify by text: a tool result quoting "429 rate limit exceeded"
    # would otherwise pause a perfectly healthy run.
    assert classify_provider_error(RuntimeError("429 rate limit exceeded")) is None


def test_retry_after_is_read_from_header_or_attribute():
    assert retry_after_seconds(_rate_limited("45")) == 45.0
    assert retry_after_seconds(_DuckError("x", status_code=429, retry_after=12)) == 12.0
    assert retry_after_seconds(_rate_limited(None)) is None
    assert retry_after_seconds(_rate_limited("soon")) is None  # non-numeric header


# --- 2. propagation: loop -> sub-session -> collect --------------------


def _agent(client):
    return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"), client=client)


def test_loop_annotates_the_error_kind():
    result = run_conversation(_agent(FakeClient([_rate_limited("30")])), "oi")
    assert result["error_kind"] == QUOTA_EXHAUSTED
    assert result["retry_after"] == 30.0


def test_loop_leaves_ordinary_errors_unclassified():
    result = run_conversation(_agent(FakeClient([RuntimeError("boom")])), "oi")
    assert result["error"] == "boom"
    assert result["error_kind"] is None


def test_collect_exposes_the_error_kind(db):
    core = OrchestrationCore(db, lambda: _agent(FakeClient([_rate_limited("7")])))
    try:
        sub_id = core.spawn("go")
        core.collect(sub_id, wait=True, timeout=5)
        out = core.collect(sub_id)
        assert out["status"] == "error"
        assert out["error_kind"] == QUOTA_EXHAUSTED
        assert out["retry_after"] == 7.0
    finally:
        core.shutdown()


def test_a_complete_sub_carries_no_error_kind(db):
    core = OrchestrationCore(db, lambda: _agent(FakeClient([_text_response("fine")])))
    try:
        sub_id = core.spawn("go")
        core.collect(sub_id, wait=True, timeout=5)
        assert core.collect(sub_id)["error_kind"] is None
    finally:
        core.shutdown()


# --- 3. engine: pause instead of a cascade of nulls --------------------


def _core(db, responder, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _quota_responder(_prompt):
    raise _rate_limited("30")


def _three_node_spec():
    return validate_spec(
        {
            "meta": {"name": "q"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go"},
                {"id": "b", "type": "agent", "prompt": "then ${a}"},
                {"id": "c", "type": "agent", "prompt": "last", "depends_on": ["b"]},
            ],
        }
    )


def test_engine_pauses_instead_of_nulling_every_node(db):
    core = _core(db, _quota_responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(_three_node_spec(), {})
        assert result.status == "paused"
        assert result.pause_reason == QUOTA_EXHAUSTED
        assert result.retry_after == 30.0
        # Only the node that actually ran is nulled — the rest never ran, and
        # nulling them would poison null_rate and read as "the leaves died".
        assert result.null_count == 1
        assert "b" not in result.outputs and "c" not in result.outputs
        assert any("quota exhausted at 'a'" in f and "retry_after=30" in f for f in result.faults)
    finally:
        core.shutdown()


def test_quota_fault_is_recorded_once_across_parallel_leaves(db):
    spec = validate_spec(
        {
            "meta": {"name": "q"},
            "nodes": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "branches": [
                        {"id": "x", "type": "agent", "prompt": "1"},
                        {"id": "y", "type": "agent", "prompt": "2"},
                        {"id": "z", "type": "agent", "prompt": "3"},
                    ],
                }
            ],
        }
    )
    core = _core(db, _quota_responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.status == "paused"
        assert sum("quota exhausted" in f for f in result.faults) == 1
    finally:
        core.shutdown()


def test_pipeline_leaf_quota_pauses_the_run(db, monkeypatch):
    # Small barrier: the design releases it via engine.stopped, so a regression
    # is a fast fault instead of a 30-minute hang.
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 2.0)
    spec = validate_spec(
        {
            "meta": {"name": "q"},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": ["a", "b", "c", "d"],
                    "stages": [{"type": "agent", "prompt": "do ${item}"}],
                }
            ],
        }
    )
    core = _core(db, _quota_responder, pool_width=2)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.status == "paused"
        assert result.pause_reason == QUOTA_EXHAUSTED
        assert any("quota exhausted" in f for f in result.faults)
    finally:
        core.shutdown()


def test_quota_cancels_the_leaves_still_in_flight(db):
    """Don't burn calls that are already doomed: every leaf still running when
    the quota lands is cancelled (they would all 429 too)."""
    seen: list[str] = []
    release = threading.Event()

    def responder(prompt):
        if "boom" in prompt:
            raise _rate_limited("30")
        release.wait(5)  # still in flight when the quota lands
        return "late"

    spec = validate_spec(
        {
            "meta": {"name": "q"},
            "nodes": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "branches": [
                        {"id": "dead", "type": "agent", "prompt": "boom"},
                        {"id": "slow", "type": "agent", "prompt": "wait here"},
                    ],
                }
            ],
        }
    )
    core = _core(db, responder, pool_width=4)
    original = core.cancel

    def spy(sub_id):
        seen.append(sub_id)
        release.set()  # the cancel is what unblocks it (interrupt is cooperative)
        return original(sub_id)

    core.cancel = spy
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.status == "paused"
        assert seen, "the in-flight leaf was never cancelled"
    finally:
        release.set()
        core.shutdown()


_INNER = {"meta": {"name": "inner"}, "nodes": [{"id": "i", "type": "agent", "prompt": "go"}]}


def test_nested_workflow_quota_pauses_the_parent(db):
    spec = validate_spec(
        {
            "meta": {"name": "outer"},
            "nodes": [
                {"id": "n", "type": "workflow", "ref": "inner"},
                {"id": "after", "type": "agent", "prompt": "later", "depends_on": ["n"]},
            ],
        }
    )
    core = _core(db, _quota_responder)
    try:
        engine = WorkflowEngine(core, budget=Budget(), loader={"inner": _INNER}.get)
        result = engine.run(spec, {})
        assert result.status == "paused"
        assert "after" not in result.outputs  # the parent stopped scheduling too
    finally:
        core.shutdown()


def test_cancel_still_wins_over_quota(db):
    core = _core(db, _quota_responder)
    try:
        engine = WorkflowEngine(core, budget=Budget())
        engine.request_cancel()
        result = engine.run(_three_node_spec(), {})
        assert result.status == "cancelled"
    finally:
        core.shutdown()


# --- 4. service: paused is terminal-for-resume, no record_outcome ------


_SPEC = {"meta": {"name": "demo"}, "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]}


class FakeTimer:
    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn()


class TimerFactory:
    def __init__(self):
        self.timers: list[FakeTimer] = []

    def __call__(self, delay, fn):
        timer = FakeTimer(delay, fn)
        self.timers.append(timer)
        return timer

    @property
    def last(self) -> FakeTimer:
        return self.timers[-1]


def _service(db, home, responder, *, timers=None, max_attempts=MAX_RESUME_ATTEMPTS):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    svc = WorkflowService(base_child_factory=factory, db=db, home=home)
    if timers is not None:
        svc.set_autoresume(
            AutoResumeScheduler(svc.resume, timer_factory=timers, clock=lambda: 1000.0,
                                max_attempts=max_attempts)
        )
    return svc


def test_paused_run_skips_record_outcome(db, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(library, "record_outcome", lambda *a, **k: calls.append(a))
    svc = _service(db, tmp_path, _quota_responder, timers=TimerFactory())
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        # The shape did not fail — the provider did. Certifying (or blaming) this
        # spec from a quota stop is exactly the wrong lesson.
        assert calls == []
    finally:
        svc.shutdown()


def test_status_reports_reason_resume_at_and_attempts(db, tmp_path):
    timers = TimerFactory()
    svc = _service(db, tmp_path, _quota_responder, timers=timers)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert out["reason"] == QUOTA_EXHAUSTED
        assert out["attempts"] == 0
        assert out["resume_at"] == 1000.0 + timers.last.delay
    finally:
        svc.shutdown()


def test_a_paused_run_is_not_live(db, tmp_path):
    svc = _service(db, tmp_path, _quota_responder, timers=TimerFactory())
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        state = svc._get(run_id)
        # The whole auto-resume rests on this: a paused run must not read as
        # live, or start(resume_run_id=...) refuses its own retry forever.
        assert _is_live(state) is False
    finally:
        svc.shutdown()


# --- 5. auto-resume ----------------------------------------------------


def test_resume_delay_prefers_retry_after_then_backs_off():
    assert resume_delay(0, 300.0) == 300.0
    assert resume_delay(0, 5.0) == MIN_RESUME_DELAY  # never hammer under the floor
    assert resume_delay(0) == MIN_RESUME_DELAY
    assert resume_delay(3) == MIN_RESUME_DELAY * 8
    assert resume_delay(20) == MAX_RESUME_DELAY  # capped at 6h
    assert resume_delay(0, 99999.0) == MAX_RESUME_DELAY


def test_scheduler_stops_after_the_attempt_cap():
    timers = TimerFactory()
    sched = AutoResumeScheduler(lambda run_id: None, timer_factory=timers, max_attempts=3)
    assert sched.schedule("r", attempts=2) is not None
    assert sched.schedule("r", attempts=3) is None
    assert len(timers.timers) == 1


def test_auto_resume_restarts_the_run_with_resume_run_id(db, tmp_path):
    timers = TimerFactory()
    quota = {"on": True}

    def responder(_prompt):
        if quota["on"]:
            raise _rate_limited("30")
        return "recovered"

    svc = _service(db, tmp_path, responder, timers=timers)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        quota["on"] = False
        timers.last.fire()  # the timer's job: re-run the SAME run_id
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete"
        assert out["outputs"]["a"] == "recovered"
        assert svc._get(run_id).attempts == 1
    finally:
        svc.shutdown()


def test_a_re_paused_run_reschedules_until_the_cap(db, tmp_path):
    timers = TimerFactory()
    svc = _service(db, tmp_path, _quota_responder, timers=timers, max_attempts=1)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        timers.last.fire()
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert out["attempts"] == 1
        # Cap reached: no further timer, and status says so (manual resume only).
        assert len(timers.timers) == 1
        assert out["resume_at"] is None
    finally:
        svc.shutdown()


def test_cancel_cancels_the_pending_timer(db, tmp_path):
    timers = TimerFactory()
    svc = _service(db, tmp_path, _quota_responder, timers=timers)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.cancel(run_id)
        assert timers.last.cancelled is True
        timers.last.fire()  # a already-fired-but-cancelled timer must be inert
        assert svc.status(run_id)["status"] == "cancelled"
    finally:
        svc.shutdown()


def test_shutdown_cancels_pending_timers(db, tmp_path):
    timers = TimerFactory()
    svc = _service(db, tmp_path, _quota_responder, timers=timers)
    run_id = svc.start(_SPEC, {})["run_id"]
    assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
    svc.shutdown()
    assert timers.last.cancelled is True


def test_the_default_timer_never_keeps_the_process_alive():
    sched = AutoResumeScheduler(lambda run_id: None)
    try:
        assert sched.schedule("r", attempts=0) is not None
        timer = sched._timers["r"]
        assert timer.daemon is True and timer.is_alive()
    finally:
        sched.shutdown()


def test_a_failing_resume_does_not_kill_the_timer_thread():
    timers = TimerFactory()

    def boom(_run_id):
        raise RuntimeError("nope")

    sched = AutoResumeScheduler(boom, timer_factory=timers)
    sched.schedule("r", attempts=0)
    timers.last.fire()  # swallowed + logged: a raise here would strand the run


def test_resume_refuses_a_run_that_is_not_paused(db, tmp_path):
    svc = _service(db, tmp_path, lambda _prompt: "ok", timers=TimerFactory())
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert "error" in svc.resume(run_id)
        assert "error" in svc.resume("nope")
    finally:
        svc.shutdown()
