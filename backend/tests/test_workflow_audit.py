from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from lohra.state import SessionDB
from lohra.workflow.audit import (
    AUDIT_SCHEMA_VERSION,
    AuditTrail,
    gateway_audit_event,
    sanitize_audit_event,
)
from lohra.workflow.causality import CausalContext


def _context(*, run_id: str = "run-1", attempt: int = 0, turn: int = 0) -> CausalContext:
    return CausalContext(
        run_id=run_id,
        segment_id="segment-1",
        node_path=("pipe",),
        cell_id="cell-1",
        role="stage",
        item_index=2,
        stage_index=1,
        attempt=attempt,
        turn=turn,
    )


def _frame(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": kind, "session_id": "sub-1", "payload": payload},
    }


def test_gateway_event_is_metadata_only_and_private_state_is_excluded() -> None:
    secret = "CANARY-super-secret"
    event = gateway_audit_event(
        _frame(
            "tool.complete",
            {
                "tool_id": "tool-7",
                "name": "terminal",
                "args": {"command": f"echo {secret}"},
                "result": f"token={secret}",
                "reasoning": secret,
                "provider_data": {"encrypted_content": secret},
                "_audit_tool_name_known": True,
            },
        ),
        _context(),
        sub_id="sub-1",
    )

    assert event is not None
    encoded = json.dumps(event, sort_keys=True)
    assert secret not in encoded
    assert event["schema_version"] == AUDIT_SCHEMA_VERSION
    assert event["event_type"] == "tool.completed"
    assert event["provenance"] == "observed"
    assert event["identity"] == {
        "run_id": "run-1",
        "segment_id": "segment-1",
        "node_path": ["pipe"],
        "cell_id": "cell-1",
        "role": "stage",
        "item_index": 2,
        "stage_index": 1,
        "branch_path": [],
        "attempt": 0,
        "turn": 0,
        "sub_id": "sub-1",
    }
    assert event["data"]["tool_name"] == "terminal"
    assert event["data"]["arguments"]["state"] == "redacted"
    assert event["data"]["result"]["state"] == "redacted"
    assert event["data"]["private_state"] == "excluded_private_state"

    class NeverSerialize:
        def __str__(self) -> str:
            raise AssertionError("audit sanitizer traversed private content")

    opaque = gateway_audit_event(
        _frame(
            "tool.complete",
            {"name": "opaque", "args": {"value": NeverSerialize()}, "result": NeverSerialize()},
        ),
        _context(),
        sub_id="sub-1",
    )
    assert opaque is not None
    assert opaque["data"]["arguments"]["size"]["unit"] == "top_level_items"
    assert opaque["data"]["result"]["size"]["state"] == "unavailable"


def test_deltas_are_excluded_by_policy_not_copied() -> None:
    assert (
        gateway_audit_event(
            _frame("message.delta", {"text": "CANARY"}), _context(), sub_id="sub-1"
        )
        is None
    )
    completed = gateway_audit_event(
        _frame("message.complete", {"text": "CANARY", "status": "complete"}),
        _context(),
        sub_id="sub-1",
    )
    assert completed is not None
    assert completed["data"]["content"]["state"] == "excluded_by_policy"
    assert "CANARY" not in json.dumps(completed)


def test_sqlite_sequence_survives_a_new_store_and_retention_has_a_gap(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    db = SessionDB(str(path))
    trail = AuditTrail(db, max_events_per_run=3, queue_limit=8)
    for turn in range(5):
        trail.record_gateway(
            _frame("message.start", {}), _context(turn=turn), sub_id=f"sub-{turn}"
        )
    assert trail.flush(timeout=2)
    trail.shutdown()
    db.close()

    reopened = SessionDB(str(path))
    events = reopened.audit_events("run-1")
    assert events[0]["event_type"] == "audit.gap"
    assert events[0]["data"] == {
        "reason": "retention_limit",
        "dropped_count": 2,
        "before_seq": 3,
    }
    assert [event["seq"] for event in events[1:]] == [3, 4, 5]

    reopened.audit_append(
        gateway_audit_event(_frame("message.start", {}), _context(turn=5), sub_id="sub-5"),
        now=10.0,
        max_events=3,
        max_runs=64,
        retention_seconds=30 * 86400,
    )
    assert [event["seq"] for event in reopened.audit_events("run-1")[1:]] == [4, 5, 6]
    reopened.close()


class _BlockingDB:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def audit_append(self, event: dict[str, Any], **_: Any) -> None:
        self.entered.set()
        self.release.wait(2)
        with self._lock:
            self.events.append(event)



def test_known_builtin_tool_identity_survives_durable_boundary(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "known-tool.db"))
    trail = AuditTrail(db)
    trail.record_gateway(
        _frame(
            "tool.complete",
            {
                "tool_id": "call-7",
                "name": "terminal",
                "args": {"command": "private"},
                "result": "private",
                "_audit_tool_name_known": True,
            },
        ),
        _context(),
        sub_id="sub-1",
    )
    assert trail.flush(timeout=2)
    trail.shutdown()
    event = db.audit_events("run-1")[0]
    assert event["data"]["tool_name"] == "terminal"
    assert event["data"]["tool_name_state"] == "known_tool"
    assert event["data"]["tool_id"] == {"state": "observed", "characters": 6}
    assert event["data"]["arguments"]["size"] == {
        "state": "observed",
        "unit": "top_level_items",
        "value": 1,
    }
    db.close()


def test_slow_sink_does_not_block_producer_and_overflow_becomes_gap() -> None:
    db = _BlockingDB()
    trail = AuditTrail(db, queue_limit=2)
    trail.record_gateway(_frame("message.start", {}), _context(), sub_id="first")
    assert db.entered.wait(1)

    finished = threading.Event()

    def produce() -> None:
        for turn in range(20):
            trail.record_gateway(
                _frame("message.start", {}), _context(turn=turn), sub_id=f"sub-{turn}"
            )
        finished.set()

    producer = threading.Thread(target=produce)
    producer.start()
    assert finished.wait(0.5), "audit backpressure blocked the workflow producer"
    producer.join()

    db.release.set()
    assert trail.flush(timeout=2)
    trail.shutdown()
    gaps = [event for event in db.events if event["event_type"] == "audit.gap"]
    assert gaps
    assert gaps[0]["data"]["reason"] == "queue_overflow"
    assert gaps[0]["data"]["dropped_count"] >= 1


def test_corrupt_payload_is_reported_as_unavailable(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    event = gateway_audit_event(_frame("message.start", {}), _context(), sub_id="sub-1")
    assert event is not None
    db.audit_append(
        event,
        now=time.time(),
        max_events=10,
        max_runs=10,
        retention_seconds=100,
    )
    with db._lock:  # deterministic corruption injection at the storage boundary
        db._connection.execute(
            "UPDATE workflow_audit_events SET payload_json = '{' WHERE run_id = 'run-1'"
        )
        db._connection.commit()

    read = db.audit_events("run-1")
    assert read == [
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "event_type": "audit.unavailable",
            "provenance": "unavailable",
            "seq": 1,
            "identity": {"run_id": "run-1"},
            "data": {"reason": "corrupt_payload"},
        }
    ]
    db.close()


def test_service_persists_leaf_lifecycle_and_cache_replay_without_content(tmp_path: Path) -> None:
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient, _text_response

    canary = "CANARY-never-persist-me"
    db = SessionDB(str(tmp_path / "state.db"))

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response(canary)] * 4),
        )

    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    spec = {
        "meta": {"name": "audited"},
        "nodes": [{"id": "answer", "type": "agent", "prompt": f"say {canary}"}],
    }
    try:
        started = service.start(spec)
        run_id = started["run_id"]
        assert service.status(run_id, wait=True)["status"] == "complete"
        resumed = service.start(resume_run_id=run_id)
        assert service.status(resumed["run_id"], wait=True)["status"] == "complete"
    finally:
        service.shutdown()

    events = db.audit_events(run_id)
    kinds = [event["event_type"] for event in events]
    assert kinds.count("segment.started") == 2
    assert kinds.count("segment.completed") == 2
    assert "cache.missed" in kinds
    assert "node.started" in kinds
    assert "node.completed" in kinds
    assert "leaf.started" in kinds
    assert "leaf.completed" in kinds
    assert "cache.stored" in kinds
    assert "cache.replayed" in kinds
    assert canary not in json.dumps(events)
    segments = [
        event["identity"]["segment_id"]
        for event in events
        if event["event_type"] == "segment.started"
    ]
    assert len(set(segments)) == 2
    leaf = next(event for event in events if event["event_type"] == "leaf.started")
    assert leaf["identity"]["node_path"] == ["answer"]
    assert leaf["identity"]["cell_id"].startswith("audit:")
    db.close()


def test_event_bytes_and_number_of_retained_runs_are_bounded(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    huge_secret = "CANARY" * 10_000
    trail = AuditTrail(db, max_event_bytes=512, max_runs=2)
    for index in range(3):
        trail.record(
            {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "event_type": "test.huge",
                "provenance": "observed",
                "identity": {
                    "run_id": f"run-{index}",
                    "segment_id": "segment",
                    "node_path": ["node"],
                    "cell_id": "cell",
                    "role": "test",
                },
                "data": {"unsafe": huge_secret},
            }
        )
        assert trail.flush(timeout=2)
    trail.shutdown()

    with db._lock:
        rows = db._connection.execute(
            "SELECT run_id, LENGTH(CAST(payload_json AS BLOB)) FROM workflow_audit_events"
        ).fetchall()
    assert len(rows) == 2
    assert all(row[1] <= 512 for row in rows)
    assert sum(row[1] for row in rows if row[0] == "run-2") <= 512
    assert huge_secret not in "".join(
        row[0] for row in db._connection.execute(
            "SELECT payload_json FROM workflow_audit_events"
        ).fetchall()
    )
    assert db.audit_events("run-0") == [
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "event_type": "audit.unavailable",
            "provenance": "unavailable",
            "identity": {"run_id": "run-0"},
            "data": {"reason": "run_retention_limit"},
        }
    ]
    db.close()


def test_concurrent_appends_get_one_dense_durable_sequence(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))

    def write(worker: int) -> None:
        for turn in range(50):
            event = gateway_audit_event(
                _frame("message.start", {}),
                _context(run_id="concurrent", turn=worker * 50 + turn),
                sub_id=f"sub-{worker}-{turn}",
            )
            assert event is not None
            db.audit_append(
                event,
                now=float(worker * 50 + turn),
                max_events=1000,
                max_runs=10,
                retention_seconds=10_000,
            )

    workers = [threading.Thread(target=write, args=(index,)) for index in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    events = db.audit_events("concurrent")
    assert [event["seq"] for event in events] == list(range(1, 201))
    db.close()



def test_clock_regression_does_not_evict_the_run_just_appended(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    for run_id, now in (("first", 100.0), ("second", 10.0)):
        event = gateway_audit_event(
            _frame("message.start", {}), _context(run_id=run_id), sub_id=run_id
        )
        assert event is not None
        db.audit_append(
            event, now=now, max_events=10, max_runs=1, retention_seconds=1000
        )
    assert db.audit_events("second")[-1]["event_type"] == "leaf.started"
    assert db.audit_events("first")[0]["data"]["reason"] == "run_retention_limit"
    db.close()


def test_temporal_retention_sweeps_inactive_runs(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    for run_id, now in (("expired", 1.0), ("current", 100.0)):
        event = gateway_audit_event(
            _frame("message.start", {}), _context(run_id=run_id), sub_id=run_id
        )
        assert event is not None
        db.audit_append(
            event, now=now, max_events=10, max_runs=10, retention_seconds=10
        )
    assert db.audit_events("expired")[0]["data"]["reason"] == "time_retention"
    assert db.audit_events("current")[-1]["event_type"] == "leaf.started"
    db.close()

def test_process_kill_leaves_atomic_dense_prefix_and_restart_continues(tmp_path: Path) -> None:
    import subprocess
    import sys

    path = tmp_path / "crash.db"
    ready = tmp_path / "ready"
    script = r'''
import sys, time
from lohra.state import SessionDB
from lohra.workflow.audit import AUDIT_SCHEMA_VERSION

db = SessionDB(sys.argv[1])
for index in range(10_000):
    db.audit_append(
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "event_type": "crash.probe",
            "provenance": "observed",
            "identity": {"run_id": "crash-run", "segment_id": "dead-segment"},
            "data": {"index": index},
        },
        now=float(index),
        max_events=20_000,
        max_runs=10,
        retention_seconds=100_000,
    )
    if index == 19:
        open(sys.argv[2], "w").close()
    time.sleep(0.001)
'''
    child = subprocess.Popen([sys.executable, "-c", script, str(path), str(ready)])
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "child never reached the committed crash boundary"
    child.kill()
    child.wait(timeout=5)

    db = SessionDB(str(path))
    with db._lock:
        assert db._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    before = db.audit_events("crash-run")
    assert len(before) >= 20
    assert [event["seq"] for event in before] == list(range(1, len(before) + 1))

    next_event = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_type": "segment.started",
        "provenance": "observed",
        "identity": {"run_id": "crash-run", "segment_id": "new-segment"},
        "data": {"recovered_process": True},
    }
    next_seq = db.audit_append(
        next_event,
        now=20_000.0,
        max_events=20_000,
        max_runs=10,
        retention_seconds=100_000,
    )
    assert next_seq == len(before) + 1
    db.close()


def test_leaf_content_size_survives_repeated_sanitization_and_the_ledger(
    tmp_path: Path,
) -> None:
    """The leaf response size is the only quantitative signal the ledger keeps.

    It passes the sanitizer three times (producer, ``SessionDB`` defense in
    depth, reader), so a non-idempotent pass would replace the honest character
    count with the cardinality of the marker the pass itself just built.
    """
    frame = _frame("message.complete", {"text": "x" * 4096, "status": "complete"})
    produced = gateway_audit_event(frame, _context(), sub_id="sub-1")
    assert produced is not None
    expected = {"state": "observed", "unit": "characters", "value": 4096}
    assert produced["data"]["content"]["size"] == expected

    once = sanitize_audit_event(produced)
    assert once["data"]["content"] == {"state": "excluded_by_policy", "size": expected}
    assert sanitize_audit_event(once) == once, "sanitization must be a fixed point"

    db = SessionDB(str(tmp_path / "state.db"))
    trail = AuditTrail(db, queue_limit=8)
    trail.record_gateway(frame, _context(), sub_id="sub-1")
    assert trail.flush(timeout=2)
    trail.shutdown()
    page = db.audit_query("run-1")
    stored = [
        event for event in page["events"] if event["event_type"] == "leaf.completed"
    ]
    assert stored and stored[0]["data"]["content"]["size"] == expected
    assert "x" * 4096 not in json.dumps(page)
    db.close()


def test_unhashable_state_is_sanitized_instead_of_raising() -> None:
    """The sanitizer's job is hardening untrusted input, so it must not crash.

    ``value.get("state") in _SAFE_STATES`` hashes whatever the payload put
    there; a dict or list raises ``TypeError`` out of the producer thread,
    before any ordinal is allocated — a silent loss with no gap marker.
    """
    for hostile in ({"nested": "dict"}, ["list"], {1, 2}):
        event = {
            "schema_version": 1,
            "event_type": "node.started",
            "provenance": "observed",
            "identity": {"run_id": "run-1"},
            "data": {"state": hostile, "nested": {"state": hostile}},
        }
        safe = sanitize_audit_event(event)
        assert safe["event_type"] == "node.started"
        assert safe["data"]["state"] != hostile


def test_a_sanitizer_failure_becomes_a_declared_gap_not_a_silent_loss(
    tmp_path: Path,
) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    trail = AuditTrail(db, queue_limit=8)
    import lohra.workflow.audit as audit_module

    original = audit_module._bounded
    calls: list[int] = []

    def exploding(event: dict[str, Any], limit: int) -> dict[str, Any]:
        # Gap markers are built through the same helper; only fail the event.
        if event.get("event_type") == "node.started":
            calls.append(1)
            raise TypeError("unhashable type: 'dict'")
        return original(event, limit)

    audit_module._bounded = exploding
    try:
        accepted = trail.record(
            {
                "schema_version": 1,
                "event_type": "node.started",
                "provenance": "observed",
                "identity": {"run_id": "run-1"},
                "data": {},
            }
        )
        assert accepted is False and calls
        assert trail.flush(timeout=2)
    finally:
        audit_module._bounded = original
        trail.shutdown()

    events = db.audit_events("run-1")
    assert [event["event_type"] for event in events] == ["audit.gap"]
    assert events[0]["data"]["reason"] == "corrupt_payload"
    db.close()


def test_a_permanently_dead_sink_stops_the_writer_at_shutdown() -> None:
    """The marker retry must observe ``_stop``.

    Its exit condition requires ``not markers_pending``, but ``_marker_inflight``
    stays True while the sink refuses, so the daemon thread spun forever after
    ``shutdown()`` — burning CPU and logging a warning per attempt for the rest
    of the process's life (disk full, read-only DB, SQLITE_CORRUPT).
    """

    class BrokenSink:
        def __init__(self) -> None:
            self.calls = 0

        def audit_append(self, event: dict[str, Any], **kwargs: Any) -> int:
            self.calls += 1
            raise RuntimeError("disk full")

    sink = BrokenSink()
    trail = AuditTrail(sink, queue_limit=8)
    trail.record(
        {
            "schema_version": 1,
            "event_type": "node.started",
            "provenance": "observed",
            "identity": {"run_id": "run-1"},
            "data": {},
        }
    )
    # Honest: a sink that never accepts anything did not drain cleanly.
    assert trail.shutdown(timeout=1.0) is False
    assert not trail._thread.is_alive(), "writer must not outlive shutdown"
    settled = sink.calls
    time.sleep(0.3)
    assert sink.calls == settled, "no attempts may continue after shutdown"


def test_marker_retry_backs_off_instead_of_hammering_a_failing_sink() -> None:
    class FlakySink:
        def __init__(self) -> None:
            self.calls = 0

        def audit_append(self, event: dict[str, Any], **kwargs: Any) -> int:
            self.calls += 1
            raise RuntimeError("locked")

    sink = FlakySink()
    trail = AuditTrail(sink, queue_limit=8)
    try:
        trail.record_gap("run-1", "sink_failure", count=1)
        time.sleep(1.0)
        # Without backoff this fixed 0.05s sleep yields ~20 attempts/second.
        assert sink.calls < 12, f"retry is hammering the sink ({sink.calls} attempts)"
    finally:
        trail.shutdown(timeout=1.0)


def test_leaf_identity_is_named_when_stamped_and_absent_when_not() -> None:
    """``model``/``provider`` are configuration identity, bounded like any other
    identifier — and a producer that stamps nothing adds no keys, so every event
    that is not a workflow leaf stays exactly as small as it was."""
    from lohra.workflow.audit import _IDENTITY_STRING_LIMIT

    bare = gateway_audit_event(
        _frame("message.complete", {"status": "complete", "text": "hi"}),
        _context(),
        sub_id="sub-1",
    )
    assert "model" not in bare["data"] and "provider" not in bare["data"]

    long_name = "m" * (_IDENTITY_STRING_LIMIT + 40)
    stamped = gateway_audit_event(
        _frame(
            "message.complete",
            {
                "status": "complete",
                "text": "hi",
                "_audit_model": long_name,
                "_audit_provider": "openai",
            },
        ),
        _context(),
        sub_id="sub-1",
    )
    once = sanitize_audit_event(stamped)
    twice = sanitize_audit_event(once)
    assert once["data"]["model"] == long_name[:_IDENTITY_STRING_LIMIT]
    assert twice["data"] == once["data"]  # bounded AND idempotent across passes
    assert once["data"]["provider"] == "openai"
