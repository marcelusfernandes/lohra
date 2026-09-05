"""Quiescence after a cancel (issue #8 / issue #42-B).

`core.cancel` is COOPERATIVE: the running turn reads the interrupt flag at the
top of its loop, again before dispatching tool calls (issue #42-A) and, on a
streaming turn, between the events of the stream (épico E3) -- so a cancel
during a provider round-trip settles with zero tools dispatched, and without
waiting the generation out. What keeps going after the engine gave up is a leaf
ALREADY inside a long `terminal`/`write_file`, or inside a NON-streaming call.
Both engine cancel sites used to walk away immediately -- and every leaf of a
run shares the run's filesystem scope (`working_root`, any `fs_allow` root, and
the shell if the operator allowed one), so the zombie writes exactly where its
successor reads.

The fix is not "cancel harder": it is to WAIT a short, bounded moment for the
leaf to settle, and to SAY so in the fault when it did not. An honest "still
running" is what tells the author that the material state under the successor
was not quiet. The tests below drive the still-alive branch with leaves blocked
INSIDE the client, where no flag reaches; the streaming abort that now settles
the common case has its own file (`test_stream_abort.py`).

Timings are injected and tiny; every gate is released in a `finally` so a
failing assertion can never hang the suite.
"""

import threading
import time

import pytest

from lohra.workflow import quiescence, strategies
from lohra.workflow.quiescence import (
    QuiescenceReport,
    await_quiescence,
    quiescence_timeout,
)
from lohra.workflow.schema import validate_spec
from tests.test_workflow_lifecycle import _core, _engine, _faults, db  # noqa: F401


# --- the helper on its own ---------------------------------------------------


class FakeCore:
    """The two calls `await_quiescence` makes, scripted."""

    def __init__(self, statuses):
        self._statuses = dict(statuses)
        self.collected: list[tuple[str, float | None]] = []

    def collect(self, sub_id, *, wait=False, timeout=None):
        self.collected.append((sub_id, timeout))
        status = self._statuses.get(sub_id)
        if status is None:
            return {"error": f"no sub-session {sub_id!r}"}
        return {"status": status}


def test_a_terminal_sub_session_settles_with_no_wait():
    core = FakeCore({"s1": "complete"})
    report = await_quiescence(core, ["s1"], timeout_s=5.0)
    assert report.settled == ("s1",)
    assert report.still_alive == ()
    assert report.clean is True
    assert "settled in" in report.clause()


def test_an_unknown_sub_session_is_not_waited_on():
    core = FakeCore({})
    report = await_quiescence(core, ["ghost"], timeout_s=5.0)
    assert report.settled == ("ghost",)
    assert report.still_alive == ()


def test_a_leaf_still_running_at_the_cap_is_reported_as_alive():
    core = FakeCore({"s1": "running"})
    report = await_quiescence(core, ["s1"], timeout_s=0.5)
    assert report.still_alive == ("s1",)
    assert report.clean is False
    assert "STILL RUNNING after" in report.clause()
    assert "working_root" in report.clause()
    assert report.suffix().startswith("cancelled;")


def test_one_call_spends_one_cap_however_many_leaves_it_was_given():
    """The cap covers the CALL: the leaves handed in together share it, so the
    pipeline barrier cancelling N stragglers never buys N * timeout. (Callers
    that collect leaf by leaf pay one cap per call — see the module docstring.)
    """
    core = FakeCore({"a": "running", "b": "running", "c": "running"})
    ticks = iter([0.0, 0.0, 0.4, 0.8, 1.0])
    await_quiescence(core, ["a", "b", "c"], timeout_s=1.0, clock=lambda: next(ticks))
    budgets = [timeout for _, timeout in core.collected]
    assert budgets == [1.0, pytest.approx(0.6), pytest.approx(0.2)]
    assert all(budget >= 0 for budget in budgets)


def test_a_collect_that_raises_never_breaks_the_cleanup():
    class Exploding:
        def collect(self, sub_id, *, wait=False, timeout=None):
            raise RuntimeError("boom")

    report = await_quiescence(Exploding(), ["s1"], timeout_s=0.1)
    assert report.settled == ("s1",)  # nothing to wait on beats a dead run thread


def test_no_sub_ids_is_a_clean_empty_report():
    report = await_quiescence(FakeCore({}), [], timeout_s=1.0, clock=lambda: 7.0)
    assert report == QuiescenceReport(limit=1.0)
    assert report.clause() == ""
    assert report.suffix() == "cancelled"


def test_the_cap_is_configurable_by_the_operator(monkeypatch):
    monkeypatch.delenv(quiescence.QUIESCENCE_ENV, raising=False)
    assert quiescence_timeout() == quiescence.CANCEL_QUIESCENCE_TIMEOUT
    monkeypatch.setenv(quiescence.QUIESCENCE_ENV, "0.25")
    assert quiescence_timeout() == 0.25
    for bad in ("", "-1", "nan", "abrakadabra"):
        monkeypatch.setenv(quiescence.QUIESCENCE_ENV, bad)
        assert quiescence_timeout() == quiescence.CANCEL_QUIESCENCE_TIMEOUT


# --- the real engine path ----------------------------------------------------


def _two_node_spec():
    return validate_spec(
        {
            "meta": {"name": "quiesce"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "slow work", "timeout": 0.2},
                {"id": "b", "type": "agent", "prompt": "fast work", "depends_on": ["a"]},
            ],
        }
    )


def _trace_spawn(core, log, lock):
    original = core.spawn

    def spy(prompt, **kwargs):
        with lock:
            log.append(f"spawn:{'a' if 'slow' in prompt else 'b'}")
        return original(prompt, **kwargs)

    core.spawn = spy


def test_a_cooperative_leaf_settles_before_its_successor_starts(db):  # noqa: F811
    """The discriminator: the cancelled leaf's LAST write lands before the
    successor's first spawn. Without the quiescence wait the successor starts
    microseconds after the cancel, while the zombie is still writing."""
    gate = threading.Event()
    log: list[str] = []
    lock = threading.Lock()

    def responder(prompt):
        if "slow" not in prompt:
            return "fast"
        gate.wait(5)
        time.sleep(0.15)  # the zombie's tail: work that lands AFTER the cancel
        with lock:
            log.append("leaf-a:returned")
        return "late"

    core = _core(db, responder)
    _trace_spawn(core, log, lock)
    original_cancel = core.cancel

    def cancel(sub_id):  # a COOPERATIVE leaf: the cancel really reaches it
        out = original_cancel(sub_id)
        gate.set()
        return out

    core.cancel = cancel
    try:
        result = _engine(core).run(_two_node_spec(), {})
        assert result.outputs["a"] is None
        assert result.outputs["b"] == "fast"
        assert "leaf timeout after" in _faults(result)
        assert "cancelled; settled in" in _faults(result)
        assert "STILL RUNNING" not in _faults(result)
        assert log.index("leaf-a:returned") < log.index("spawn:b")
    finally:
        gate.set()
        core.shutdown()


def test_a_non_cooperative_leaf_never_holds_the_run_past_the_cap(db, monkeypatch):  # noqa: F811
    """A leaf blocked inside a provider call cannot be aborted -- the run must
    move on at the cap and the fault must say the working root was not quiet."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.3)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1] if "slow" in prompt else "fast")
    try:
        started = time.monotonic()
        result = _engine(core).run(_two_node_spec(), {})
        elapsed = time.monotonic() - started
        assert result.outputs["a"] is None
        assert result.outputs["b"] == "fast"  # the run moved on
        assert "leaf timeout after" in _faults(result)
        assert "cancelled; " in _faults(result)  # the word the rollup classifies on
        assert "STILL RUNNING after 0.3s quiescence wait" in _faults(result)
        assert "working_root" in _faults(result)
        assert elapsed < 3.0  # the cap, not the leaf's own 5s
    finally:
        gate.set()
        core.shutdown()


def test_the_pipeline_barrier_waits_for_its_cancelled_leaves(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.3)
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.3)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    spec = validate_spec(
        {
            "meta": {"name": "p"},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": "${args.items}",
                    "stages": [{"prompt": "do ${item}"}],
                }
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {"items": ["x"]})
        assert result.outputs["p"] == [None]
        assert "pipeline timed out after" in _faults(result)
        assert "1 leaf(s) cancelled" in _faults(result)
        assert "STILL RUNNING after 0.3s quiescence wait" in _faults(result)
    finally:
        gate.set()
        core.shutdown()


def test_the_pipeline_barrier_reports_a_leaf_that_did_settle(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.3)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    original_cancel = core.cancel

    def cancel(sub_id):
        out = original_cancel(sub_id)
        gate.set()
        return out

    core.cancel = cancel
    spec = validate_spec(
        {
            "meta": {"name": "p"},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": "${args.items}",
                    "stages": [{"prompt": "do ${item}"}],
                }
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {"items": ["x"]})
        assert "pipeline timed out after" in _faults(result)
        assert "settled in" in _faults(result)
        assert "STILL RUNNING" not in _faults(result)
    finally:
        gate.set()
        core.shutdown()


# --- the contract's two boundaries (review adversarial) ----------------------


def test_the_stranded_path_never_blocks_an_on_done_worker(db, monkeypatch):  # noqa: F811
    """`_cancel_running` has TWO callers on opposite sides of the contract.

    The barrier's ``_expire`` runs on the node thread and may wait. The stranded
    TOCTOU path in ``_advance`` runs on the pool's ``on_done`` workers (a stage
    chaining into the next), where blocking is exactly what this module's
    contract forbids — a worker parked in a quiescence wait is a worker the
    pipeline cannot use to advance any other item."""
    from types import SimpleNamespace

    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 1.0)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    fake = SimpleNamespace(_engine=SimpleNamespace(core=core, account_leaf=lambda _s: None))
    try:
        sub_id = core.spawn("blocked")
        for _ in range(200):  # the turn has to be RUNNING for the path to matter
            if core.collect(sub_id).get("status") == "running":
                break
            time.sleep(0.01)
        started = time.monotonic()
        cancelled, report = strategies._PipelineRun._cancel_running(
            fake, [sub_id], quiesce=False
        )
        elapsed = time.monotonic() - started
        assert cancelled == 1
        assert elapsed < 0.3  # nowhere near the 1.0s cap
        assert report.settled == () and report.still_alive == ()
        assert report.clause() == ""  # nothing observed -> nothing claimed
    finally:
        gate.set()
        core.shutdown()


def test_each_caller_states_whether_it_may_wait(db, monkeypatch):  # noqa: F811
    """Anti-drift on the seam itself: `quiesce` is keyword-ONLY and has no
    default, so a third caller cannot inherit a policy it never thought about."""
    import inspect

    signature = inspect.signature(strategies._PipelineRun._cancel_running)
    parameter = signature.parameters["quiesce"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
