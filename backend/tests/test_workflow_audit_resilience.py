from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from lohra.state import SessionDB
from lohra.workflow.audit import (
    AUDIT_SCHEMA_VERSION,
    AuditTrail,
    _bounded,
    gateway_audit_event,
)
from tests.test_workflow_audit import _BlockingDB, _context, _frame


class _FlakyDB:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.events: list[dict[str, Any]] = []

    def audit_append(self, event: dict[str, Any], **_: Any) -> None:
        if self.failures:
            self.failures -= 1
            raise OSError("synthetic sink outage")
        self.events.append(event)


def test_sink_recovery_persists_explicit_gap_without_failing_producer() -> None:
    db = _FlakyDB(failures=2)
    trail = AuditTrail(db)
    trail.record_gateway(_frame("message.start", {}), _context(), sub_id="sub")
    assert trail.flush(timeout=2)
    trail.shutdown()

    assert db.events == [
        {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "event_type": "audit.gap",
            "provenance": "dropped",
            "identity": {"run_id": "run-1"},
            "data": {"reason": "sink_failure", "dropped_count": 1},
        }
    ]


def test_unknown_crash_tail_is_declared_without_inventing_a_count(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    trail = AuditTrail(db)
    trail.record_gap("run-1", "process_crash", count=None)
    assert trail.flush(timeout=2)
    trail.shutdown()

    event = db.audit_events("run-1")[0]
    assert event["event_type"] == "audit.gap"
    assert event["data"] == {
        "reason": "process_crash",
        "dropped_count": None,
        "count_state": "unavailable",
    }
    db.close()


def test_snapshot_ring_and_append_strategies_have_distinct_evidence(tmp_path: Path) -> None:
    from collections import deque

    path = tmp_path / "strategies.db"
    db = SessionDB(str(path))
    ring: deque[int] = deque(maxlen=3)
    for index in range(5):
        db.run_state_put(
            "comparison",
            {
                "name": "comparison",
                "status": "running",
                "progress_json": json.dumps({"last_action": index}),
            },
            now=float(index),
        )
        ring.append(index)
        event = gateway_audit_event(
            _frame("message.start", {}),
            _context(run_id="comparison", turn=index),
            sub_id=f"sub-{index}",
        )
        assert event is not None
        db.audit_append(
            event,
            now=float(index),
            max_events=10,
            max_runs=10,
            retention_seconds=100,
        )
    assert json.loads(db.run_state_get("comparison")["progress_json"]) == {
        "last_action": 4
    }
    assert list(ring) == [2, 3, 4]
    db.close()

    reopened = SessionDB(str(path))
    assert [event["identity"]["turn"] for event in reopened.audit_events("comparison")] == [
        0,
        1,
        2,
        3,
        4,
    ]
    reopened.close()


def test_wide_64_leaf_workflow_has_complete_bounded_audit(tmp_path: Path) -> None:
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient, _text_response

    db = SessionDB(str(tmp_path / "state.db"))

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response("ok")]),
        )

    service = WorkflowService(
        base_child_factory=factory,
        db=db,
        home=tmp_path,
        run_concurrency=8,
    )
    spec = {
        "meta": {"name": "wide-audit"},
        "nodes": [
            {
                "id": "wide",
                "type": "parallel",
                "branches": [f"branch {index}" for index in range(64)],
            }
        ],
    }
    started = service.start(spec)
    run_id = started["run_id"]
    assert service.status(run_id, wait=True)["status"] == "complete"
    service.shutdown()

    events = db.audit_events(run_id)
    kinds = [event["event_type"] for event in events]
    assert len(events) == 134
    assert kinds.count("leaf.started") == 64
    assert kinds.count("leaf.completed") == 64
    assert "audit.gap" not in kinds
    db.close()


def test_opaque_identifier_objects_are_never_stringified() -> None:
    class NeverStringify:
        def __str__(self) -> str:
            raise AssertionError("opaque identifier was stringified")

    event = gateway_audit_event(
        _frame(
            "tool.complete",
            {
                "tool_call_id": NeverStringify(),
                "name": NeverStringify(),
                "status": NeverStringify(),
                "result": "secret",
            },
        ),
        _context(),
        sub_id="sub",
    )
    assert event is not None
    assert event["data"]["tool_id"] is None
    assert event["data"]["tool_name"] is None
    assert event["data"]["status"] is None


def test_drop_accounting_is_bounded_across_unlimited_run_ids() -> None:
    db = _BlockingDB()
    trail = AuditTrail(db, queue_limit=1, max_drop_buckets=3)
    trail.record_gateway(_frame("message.start", {}), _context(), sub_id="writer")
    assert db.entered.wait(1)
    trail.record_gateway(_frame("message.start", {}), _context(), sub_id="queued")
    for index in range(20):
        trail.record_gateway(
            _frame("message.start", {}),
            _context(run_id=f"run-{index}"),
            sub_id=f"dropped-{index}",
        )

    db.release.set()
    assert trail.flush(timeout=2)
    trail.shutdown()
    gaps = [event for event in db.events if event["event_type"] == "audit.gap"]
    assert len(gaps) == 3
    aggregate = next(event for event in gaps if event["identity"]["run_id"] == "$audit")
    assert aggregate["data"]["reason"] == "drop_bucket_overflow"
    assert aggregate["data"]["run_attribution"] == "unavailable"
    assert aggregate["data"]["dropped_count"] == 18


def test_generic_record_boundary_sanitizes_sensitive_and_opaque_values(tmp_path: Path) -> None:
    canary = "CANARY-generic-boundary"

    class OpaqueKey:
        def __str__(self) -> str:
            raise AssertionError("opaque key was stringified")

        def __repr__(self) -> str:
            raise AssertionError("opaque key was repr'd")

    class OpaqueValue:
        def __str__(self) -> str:
            raise AssertionError("opaque value was stringified")

        def __repr__(self) -> str:
            raise AssertionError("opaque value was repr'd")

    db = SessionDB(str(tmp_path / "state.db"))
    trail = AuditTrail(db)
    trail.record(
        {
            "event_type": "test.boundary",
            "provenance": "observed",
            "identity": {"run_id": "run-safe"},
            "data": {
                "prompt": canary,
                "result": {"token": canary},
                OpaqueKey(): OpaqueValue(),
            },
        }
    )
    assert trail.flush(timeout=2)
    trail.shutdown()
    for artifact in tmp_path.glob("state.db*"):
        assert canary.encode() not in artifact.read_bytes(), artifact.name
    event = db.audit_events("run-safe")[0]
    assert event["data"]["prompt"]["state"] == "excluded_by_policy"
    assert event["data"]["result"]["state"] == "excluded_by_policy"
    assert event["data"]["field_2_unavailable"]["state"] == "excluded_by_policy"
    db.close()


def test_explicit_crash_gap_survives_full_queue_in_causal_order() -> None:
    db = _BlockingDB()
    trail = AuditTrail(db, queue_limit=1)
    assert trail.record_gateway(_frame("message.start", {}), _context(), sub_id="first")
    assert db.entered.wait(1)
    assert trail.record_gateway(_frame("message.start", {}), _context(), sub_id="queued")
    assert trail.record_gap("run-1", "process_crash", count=None)

    db.release.set()
    assert trail.flush(timeout=2)
    assert trail.shutdown()
    assert [event["event_type"] for event in db.events] == [
        "leaf.started",
        "leaf.started",
        "audit.gap",
    ]
    gap = db.events[-1]
    assert gap["data"] == {
        "reason": "process_crash",
        "dropped_count": None,
        "count_state": "unavailable",
    }


def test_terminal_gateway_status_is_not_reported_as_success() -> None:
    complete = gateway_audit_event(
        _frame("message.complete", {"status": "complete"}), _context(), sub_id="sub"
    )
    failed = gateway_audit_event(
        _frame("message.complete", {"status": "error"}), _context(), sub_id="sub"
    )
    interrupted = gateway_audit_event(
        _frame("message.complete", {"status": "interrupted"}), _context(), sub_id="sub"
    )
    assert complete is not None and complete["event_type"] == "leaf.completed"
    assert failed is not None and failed["event_type"] == "leaf.failed"
    assert interrupted is not None and interrupted["event_type"] == "leaf.failed"


def test_unknown_model_tool_name_is_excluded() -> None:
    canary = "CANARY-unknown-tool-content"
    event = gateway_audit_event(
        _frame("tool.start", {"name": canary, "args": {}}),
        _context(),
        sub_id="sub",
    )
    assert event is not None
    assert canary not in json.dumps(event)
    assert event["data"]["tool_name"] is None
    assert event["data"]["tool_name_state"] == "unknown_tool"


def test_strict_event_byte_bound_including_adversarial_unicode() -> None:
    event = {
        "event_type": "node." + "😀" * 200,
        "provenance": "😀" * 100,
        "identity": {
            "run_id": "😀" * 128,
            "segment_id": "😀" * 128,
            "node_path": ["😀" * 64] * 8,
            "cell_id": "😀" * 128,
            "role": "😀" * 64,
            "sub_id": "😀" * 128,
        },
        "data": {"status": "😀" * 500},
    }
    for limit in (512, 2048):
        bounded = _bounded(event, limit)
        encoded = json.dumps(bounded, ensure_ascii=True, separators=(",", ":")).encode()
        assert len(encoded) <= limit
        assert bounded["event_type"] == "audit.truncated"


def test_time_retention_removes_a_sequence_prefix_not_a_middle_hole(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "retention.db"))

    def append(now: float, event_type: str, retention: float = 1000) -> None:
        db.audit_append(
            {
                "schema_version": 1,
                "event_type": event_type,
                "provenance": "observed",
                "identity": {"run_id": "run-retention"},
                "data": {},
            },
            now=now,
            max_events=100,
            max_runs=10,
            retention_seconds=retention,
        )

    append(100, "node.started")
    append(0, "node.completed")
    append(50, "cache.missed")
    append(200, "cache.stored", retention=160)  # cutoff=40: seq=2 is old, seq=1 is not
    events = db.audit_events("run-retention")
    assert events[0]["event_type"] == "audit.gap"
    assert events[0]["data"]["dropped_count"] == 2
    assert events[0]["data"]["before_seq"] == 3
    assert [(event["seq"], event["event_type"]) for event in events[1:]] == [
        (3, "cache.missed"),
        (4, "cache.stored"),
    ]
    db.close()


def test_audit_sqlite_wait_does_not_hold_general_session_lock(tmp_path: Path) -> None:
    path = tmp_path / "convoy.db"
    db = SessionDB(str(path))
    blocker = sqlite3.connect(path)
    blocker.execute("PRAGMA busy_timeout=1000")
    blocker.execute("BEGIN IMMEDIATE")
    trail = AuditTrail(db)
    assert trail.record_gateway(_frame("message.start", {}), _context(), sub_id="sub")
    time.sleep(0.02)  # writer is now waiting on its dedicated short-timeout connection

    started = time.monotonic()
    assert db.run_state_get("missing") is None
    elapsed = time.monotonic() - started
    assert elapsed < 0.2

    blocker.rollback()
    blocker.close()
    assert trail.flush(timeout=2)
    assert trail.shutdown()
    db.close()


def test_record_is_explicitly_refused_after_shutdown(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "closed.db"))
    trail = AuditTrail(db)
    assert trail.shutdown()
    assert not trail.record(
        {
            "event_type": "late",
            "provenance": "observed",
            "identity": {"run_id": "late-run"},
            "data": {},
        }
    )
    assert db.audit_events("late-run") == []
    db.close()


def _multiprocess_audit_writer(path: str, worker: int, count: int) -> None:
    db = SessionDB(path)
    for index in range(count):
        for attempt in range(20):
            try:
                db.audit_append(
                    {
                        "schema_version": 1,
                        "event_type": "mp.event",
                        "provenance": "observed",
                        "identity": {"run_id": "mp-run"},
                        "data": {"worker": worker, "index": index},
                    },
                    now=time.time(),
                    max_events=1000,
                    max_runs=10,
                    retention_seconds=3600,
                )
                break
            except sqlite3.OperationalError:
                if attempt == 19:
                    raise
                time.sleep(0.01)
    db.close()


def test_sqlite_sequence_is_dense_across_process_connections(tmp_path: Path) -> None:
    path = str(tmp_path / "multiprocess.db")
    SessionDB(path).close()  # migrate before process contention
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_multiprocess_audit_writer, args=(path, worker, 25))
        for worker in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    db = SessionDB(path)
    events = db.audit_events("mp-run")
    assert len(events) == 100
    assert [event["seq"] for event in events] == list(range(1, 101))
    db.close()


def test_service_rejects_launch_after_shutdown(tmp_path: Path) -> None:
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient, _text_response

    db = SessionDB(str(tmp_path / "service-close.db"))

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response("ok")]),
        )

    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    service.shutdown()
    result = service.start(
        {"meta": {"name": "late"}, "nodes": [{"id": "n", "type": "agent", "prompt": "x"}]}
    )
    assert result == {"error": "workflow service is shutting down"}
    db.close()


class _RecoverableUnavailableDB:
    def __init__(self) -> None:
        self.available = threading.Event()
        self.events: list[dict[str, Any]] = []

    def audit_append(self, event: dict[str, Any], **_kwargs: Any) -> int:
        if not self.available.is_set():
            raise sqlite3.OperationalError("locked")
        self.events.append(event)
        return len(self.events)


def test_shutdown_reports_permanently_unavailable_sink_then_recovers() -> None:
    db = _RecoverableUnavailableDB()
    trail = AuditTrail(db)
    assert trail.record_gateway(_frame("message.start", {}), _context(), sub_id="sub")
    assert not trail.shutdown(timeout=0.05)
    assert not trail.record_gateway(_frame("message.start", {}), _context(), sub_id="late")
    db.available.set()
    assert trail.shutdown(timeout=2)
    assert [event["event_type"] for event in db.events] == ["audit.gap"]
    assert db.events[0]["data"]["reason"] == "sink_failure"


def _async_audit_crash_writer(path: str, ready: Any) -> None:
    db = SessionDB(path)

    class SlowSink:
        def audit_append(self, event: dict[str, Any], **kwargs: Any) -> int:
            time.sleep(0.02)
            return db.audit_append(event, **kwargs)

    trail = AuditTrail(SlowSink(), queue_limit=256)
    for index in range(200):
        trail.record(
            {
                "event_type": "leaf.started",
                "provenance": "observed",
                "identity": {"run_id": "async-crash", "turn": index},
                "data": {},
            }
        )
    ready.set()
    time.sleep(30)


def test_sigkill_of_async_pipeline_keeps_dense_prefix_and_restart_gap(tmp_path: Path) -> None:
    path = str(tmp_path / "async-crash.db")
    SessionDB(path).close()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(target=_async_audit_crash_writer, args=(path, ready))
    child.start()
    assert ready.wait(5)
    time.sleep(0.06)  # allow a small committed prefix while most events remain queued
    child.kill()
    child.join(5)
    assert child.exitcode is not None and child.exitcode != 0

    db = SessionDB(path)
    assert db._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    before = db.audit_events("async-crash")
    assert before
    assert [event["seq"] for event in before] == list(range(1, len(before) + 1))

    trail = AuditTrail(db)
    assert trail.record_gap("async-crash", "process_crash", count=None)
    assert trail.flush(timeout=2)
    assert trail.shutdown()
    after = db.audit_events("async-crash")
    assert [event["seq"] for event in after] == list(range(1, len(after) + 1))
    assert after[-1]["event_type"] == "audit.gap"
    assert after[-1]["data"]["reason"] == "process_crash"
    assert after[-1]["data"]["dropped_count"] is None
    db.close()


def test_forged_redaction_marker_cannot_bypass_metadata_allowlist(tmp_path: Path) -> None:
    canary = "CANARY-forged-marker"
    db = SessionDB(str(tmp_path / "forged.db"))
    trail = AuditTrail(db)
    assert trail.record(
        {
            "event_type": "test.forged",
            "provenance": "observed",
            "identity": {"run_id": "forged-run"},
            "data": {
                "result": {
                    "state": "observed",
                    "secret_alias": canary,
                    "size": {"state": "observed", "unit": "characters", "value": 20},
                },
                "innocent_alias": canary,
            },
        }
    )
    assert trail.flush(timeout=2)
    assert trail.shutdown()
    for artifact in tmp_path.glob("forged.db*"):
        assert canary.encode() not in artifact.read_bytes(), artifact.name
    event = db.audit_events("forged-run")[0]
    assert "secret_alias" not in event["data"]["result"]
    assert event["data"]["result"]["size"]["value"] == 20
    assert event["data"]["innocent_alias"]["state"] == "excluded_by_policy"
    db.close()


def test_evicted_run_resurrection_preserves_sequence_and_declares_prefix_gap(
    tmp_path: Path,
) -> None:
    db = SessionDB(str(tmp_path / "resurrection.db"))

    def append(run_id: str, now: float, max_runs: int = 1) -> int:
        return db.audit_append(
            {
                "schema_version": 1,
                "event_type": "probe",
                "provenance": "observed",
                "identity": {"run_id": run_id},
                "data": {},
            },
            now=now,
            max_events=10,
            max_runs=max_runs,
            retention_seconds=1000,
        )

    assert append("resumed", 1) == 1
    assert append("other", 2) == 1
    assert db.audit_events("resumed")[0]["data"]["reason"] == "run_retention_limit"

    assert append("resumed", 3) == 2
    events = db.audit_events("resumed")
    assert events[0]["event_type"] == "audit.gap"
    assert events[0]["data"] == {
        "reason": "retention_limit",
        "dropped_count": 1,
        "before_seq": 2,
    }
    assert events[1]["seq"] == 2
    db.close()


def test_audit_schema_migration_is_additive(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE legacy_sentinel (value TEXT NOT NULL)")
    legacy.execute("INSERT INTO legacy_sentinel VALUES ('preserved')")
    legacy.commit()
    legacy.close()

    db = SessionDB(str(path))
    assert db._connection.execute("SELECT value FROM legacy_sentinel").fetchone()[0] == "preserved"
    tables = {
        row[0]
        for row in db._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'workflow_audit_%'"
        ).fetchall()
    }
    assert tables == {
        "workflow_audit_events",
        "workflow_audit_order",
        "workflow_audit_state",
        "workflow_audit_tombstones",
    }
    db.close()


def test_cancel_is_visible_without_inventing_unrun_node_events(tmp_path: Path) -> None:
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_workflow_pipeline import ScriptedClient

    started = threading.Event()
    release = threading.Event()

    def responder(_prompt: str) -> str:
        started.set()
        release.wait(3)
        return "done"

    db = SessionDB(str(tmp_path / "cancel.db"))

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    spec = {
        "meta": {"name": "cancel-audit"},
        "nodes": [
            {"id": "first", "type": "agent", "prompt": "wait"},
            {"id": "never", "type": "agent", "prompt": "must not run"},
        ],
    }
    try:
        run_id = service.start(spec)["run_id"]
        assert started.wait(2)
        assert service.cancel(run_id)["ok"] is True
        release.set()
        assert service.status(run_id, wait=True, timeout=5)["status"] == "cancelled"
    finally:
        release.set()
        service.shutdown()

    events = db.audit_events(run_id)
    terminal = [event for event in events if event["event_type"] == "segment.completed"]
    assert terminal[-1]["data"]["status"] == "cancelled"
    assert not any(
        event["identity"].get("node_path") == ["never"] for event in events
    )
    db.close()


def test_direct_sink_boundary_rejects_safe_field_aliases_and_cache_digests(tmp_path: Path) -> None:
    secret = "CANARY-aliased-content"
    db = SessionDB(str(tmp_path / "boundary.db"))
    db.audit_append(
        {
            "schema_version": 1,
            "event_type": secret,
            "provenance": secret,
            "identity": {"run_id": "run-boundary", "cell_id": secret},
            "data": {
                "status": secret,
                "reason": secret,
                "source": secret,
                "tool_name": secret,
                "tool_id": secret,
            },
        },
        now=1,
        max_events=10,
        max_runs=10,
        retention_seconds=100,
    )
    events = db.audit_events("run-boundary")
    encoded = json.dumps(events, sort_keys=True)
    assert secret not in encoded
    assert events[0]["event_type"] == "audit.unavailable"
    assert events[0]["provenance"] == "unavailable"
    assert events[0]["identity"]["cell_id"].startswith("audit:")
    db.close()


def test_tombstone_compaction_never_fabricates_a_gap_on_a_pristine_run(
    tmp_path: Path,
) -> None:
    """A run the ledger has never seen starts dense at seq 1, always.

    The compaction horizon cannot be attributed to a run id: "evicted and then
    compacted" and "brand new" are the same observation.  Claiming a gap for
    both makes every run after the horizon report a phantom prefix, which is
    exactly the discriminator OBS-04 exists to provide.  The residual (a
    resurrected-after-compaction run reads as fresh) is named in the spec.
    """
    db = SessionDB(str(tmp_path / "tombstone.db"))

    def append(run_id: str) -> None:
        db.audit_append(
            {
                "schema_version": 1,
                "event_type": "node.started",
                "provenance": "observed",
                "identity": {"run_id": run_id},
                "data": {"state": "running"},
            },
            now=time.time(),
            max_events=10,
            max_runs=1,
            retention_seconds=1000,
        )

    append("r1")
    append("r2")
    append("r3")  # compacts r1's tombstone to keep retention metadata bounded
    compacted = db._audit_connection.execute(
        "SELECT 1 FROM workflow_audit_tombstones WHERE run_id = '$compacted'"
    ).fetchone()
    assert compacted is not None, "the horizon marker must still be recorded"

    append("pristine")
    events = db.audit_events("pristine")
    assert [event["event_type"] for event in events] == ["node.started"]
    assert events[0]["seq"] == 1
    page = db.audit_query("pristine")
    assert page["integrity"]["event_markers"]["gaps"] == 0
    assert page["integrity"]["notices"] == []
    db.close()


def test_uncompacted_tombstone_resurrection_is_an_explicit_unknown_prefix(
    tmp_path: Path,
) -> None:
    """An evicted run whose own tombstone survived still declares its prefix."""
    db = SessionDB(str(tmp_path / "resurrect.db"))

    def append(run_id: str) -> None:
        db.audit_append(
            {
                "schema_version": 1,
                "event_type": "node.started",
                "provenance": "observed",
                "identity": {"run_id": run_id},
                "data": {"state": "running"},
            },
            now=time.time(),
            max_events=10,
            max_runs=1,
            retention_seconds=1000,
        )

    append("r1")
    append("r2")  # evicts r1, keeping its tombstone (next_seq = 2)
    append("r1")
    events = db.audit_events("r1")
    assert events[0]["event_type"] == "audit.gap"
    assert events[0]["data"] == {
        "reason": "retention_limit",
        "dropped_count": 1,
        "before_seq": 2,
    }
    assert events[1]["seq"] == 2
    db.close()


def test_checkpoint_pause_is_not_a_node_failure_and_closes_segment(tmp_path: Path) -> None:
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient

    db = SessionDB(str(tmp_path / "checkpoint.db"))

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([]),
        )

    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    spec = {
        "meta": {"name": "checkpoint-audit"},
        "nodes": [{"id": "approve", "type": "checkpoint", "prompt": "Proceed?"}],
    }
    try:
        run_id = service.start(spec)["run_id"]
        assert service.status(run_id, wait=True)["status"] == "paused"
    finally:
        service.shutdown()
    events = db.audit_events(run_id)
    assert any(event["event_type"] == "node.paused" for event in events)
    assert not any(event["event_type"] == "node.failed" for event in events)
    assert db.run_state_get(run_id)["audit_segment_id"] is None
    db.close()


def test_resume_declares_unclosed_terminal_audit_segment(tmp_path: Path) -> None:
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient

    db = SessionDB(str(tmp_path / "unclosed.db"))

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([]),
        )

    spec = {
        "meta": {"name": "unclosed-audit"},
        "nodes": [{"id": "approve", "type": "checkpoint", "prompt": "Proceed?"}],
    }
    first = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    run_id = first.start(spec)["run_id"]
    assert first.status(run_id, wait=True)["status"] == "paused"
    first.shutdown()
    with db._lock:
        db._connection.execute(
            "UPDATE workflow_run_state SET audit_segment_id = 'lost-tail' WHERE run_id = ?",
            (run_id,),
        )
        db._connection.commit()
    second = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        assert second.start(
            resume_run_id=run_id, checkpoint_answers={"approve": "go"}
        )["run_id"] == run_id
        assert second.status(run_id, wait=True)["status"] == "complete"
    finally:
        second.shutdown()
    gaps = [event for event in db.audit_events(run_id) if event["event_type"] == "audit.gap"]
    # A lost `segment.completed` append is NOT evidence of a crash: this process
    # shut down cleanly. `process_crash` is reserved for a dead process (§11.2),
    # so the honest label for an unobservable cause is `unavailable`.
    assert gaps[-1]["data"] == {
        "reason": "unavailable",
        "dropped_count": None,
        "count_state": "unavailable",
    }
    started = [
        event
        for event in db.audit_events(run_id)
        if event["event_type"] == "segment.started"
    ]
    assert [event["data"]["recovered_process"] for event in started] == [False, False]
    db.close()


def test_a_dead_process_is_still_reported_as_a_process_crash(tmp_path: Path) -> None:
    """The discriminator's other half: a `running` line nobody holds a lease on."""
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient

    db = SessionDB(str(tmp_path / "crashed.db"))

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([]),
        )

    spec = {
        "meta": {"name": "crashed-audit"},
        "nodes": [{"id": "approve", "type": "checkpoint", "prompt": "Proceed?"}],
    }
    first = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    run_id = first.start(spec)["run_id"]
    assert first.status(run_id, wait=True)["status"] == "paused"
    first.shutdown()
    # What SIGKILL leaves behind: a `running` row whose lease nobody holds.
    with db._lock:
        db._connection.execute(
            "UPDATE workflow_run_state SET status = 'running' WHERE run_id = ?",
            (run_id,),
        )
        db._connection.commit()
    second = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        assert second.start(
            resume_run_id=run_id, checkpoint_answers={"approve": "go"}
        )["run_id"] == run_id
        assert second.status(run_id, wait=True)["status"] == "complete"
    finally:
        second.shutdown()
    gaps = [event for event in db.audit_events(run_id) if event["event_type"] == "audit.gap"]
    assert gaps[-1]["data"] == {
        "reason": "process_crash",
        "dropped_count": None,
        "count_state": "unavailable",
    }
    started = [
        event
        for event in db.audit_events(run_id)
        if event["event_type"] == "segment.started"
    ]
    assert started[-1]["data"]["recovered_process"] is True
    db.close()


def test_run_retention_evicts_dead_runs_before_a_live_one(tmp_path: Path) -> None:
    """Eviction order must know about liveness, not just append recency.

    A run paused on a ``checkpoint`` (WF-29) is designed to wait for a human
    BETWEEN processes — potentially for hours. It emits no audit events while it
    waits, so a purely LRU-by-append cap evicts its ENTIRE trail (eviction is a
    whole-run DELETE, not a prefix) in favour of runs that are merely newer. The
    cap stays hard: liveness reorders who is evicted, it never exempts anyone.
    """
    db = SessionDB(str(tmp_path / "live.db"))
    with db._lock:
        for run_id, status in (("live-run", "paused"), ("dead-run", "complete")):
            db._connection.execute(
                "INSERT INTO workflow_run_state (run_id, status, updated_at) VALUES (?, ?, ?)",
                (run_id, status, 1000.0),
            )
        db._connection.commit()

    def append(run_id: str) -> None:
        db.audit_append(
            {
                "schema_version": 1,
                "event_type": "node.started",
                "provenance": "observed",
                "identity": {"run_id": run_id},
                "data": {"state": "running"},
            },
            now=time.time(),
            max_events=10,
            max_runs=3,
            retention_seconds=1000,
        )

    append("live-run")
    append("dead-run")
    # Enough to force eviction, few enough that no tombstone is compacted away.
    for index in range(3):
        append(f"noise-{index}")

    live = db.audit_events("live-run")
    assert [event["event_type"] for event in live] == ["node.started"]
    dead = db.audit_events("dead-run")
    assert dead[0]["event_type"] == "audit.unavailable"
    # The cap is still hard: at most `max_runs` runs keep a retained trail.
    with db._lock:
        retained = db._audit_connection.execute(
            "SELECT COUNT(*) FROM workflow_audit_state"
        ).fetchone()[0]
    assert retained <= 3
    db.close()


def test_the_appending_run_is_never_self_evicted_even_under_live_pressure(
    tmp_path: Path,
) -> None:
    db = SessionDB(str(tmp_path / "self.db"))
    with db._lock:
        for index in range(4):
            db._connection.execute(
                "INSERT INTO workflow_run_state (run_id, status, updated_at) VALUES (?, ?, ?)",
                (f"held-{index}", "running", 1000.0),
            )
        db._connection.commit()

    def append(run_id: str) -> None:
        db.audit_append(
            {
                "schema_version": 1,
                "event_type": "node.started",
                "provenance": "observed",
                "identity": {"run_id": run_id},
                "data": {"state": "running"},
            },
            now=time.time(),
            max_events=10,
            max_runs=2,
            retention_seconds=1000,
        )

    for index in range(4):
        append(f"held-{index}")
    append("newcomer")  # not live anywhere: the whole cap is held by live runs
    events = db.audit_events("newcomer")
    assert [event["event_type"] for event in events] == ["node.started"]
    db.close()


def _checkpoint_service(db: SessionDB, tmp_path: Path) -> Any:
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([]),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=tmp_path)


_CHECKPOINT_SPEC = {
    "meta": {"name": "segment-ordering"},
    "nodes": [{"id": "approve", "type": "checkpoint", "prompt": "Proceed?"}],
}


def test_the_terminal_line_is_never_durable_before_the_segment_closes(
    tmp_path: Path,
) -> None:
    """Ordering, not timing: the closing append lands while the run still owns
    its lease and its line still reads ``running``.

    The marker on the terminal line is the discriminator for "the closing
    ``segment.completed`` append never landed".  Publishing that line — and
    handing the lease to the next resume — while the append is merely QUEUED
    turns a race into a permanent, false ``audit.gap`` on a run in which
    nothing was ever lost.

    The observation is recorded from the writer thread and asserted on the main
    one on purpose: an ``assert`` raised inside the sink is swallowed by
    ``AuditTrail._append`` and would come back as a ``sink_failure`` marker.
    """
    db = SessionDB(str(tmp_path / "ordering.db"))
    service = _checkpoint_service(db, tmp_path)
    observed: list[tuple[Any, bool, bool]] = []
    box: dict[str, str] = {}
    original = db.audit_append

    def spy(event: dict[str, Any], **kwargs: Any) -> int:
        run_id = box.get("run_id")
        if event.get("event_type") == "segment.completed" and run_id:
            row = db.run_state_get(run_id)
            observed.append(
                (
                    row["status"],
                    row["audit_segment_id"] is not None,
                    service._store.lease_expiry(run_id) is not None,
                )
            )
        return original(event, **kwargs)

    db.audit_append = spy  # type: ignore[method-assign]
    try:
        box["run_id"] = service.start(_CHECKPOINT_SPEC)["run_id"]
        assert service.status(box["run_id"], wait=True)["status"] == "paused"
    finally:
        service.shutdown()
    assert observed == [("running", True, True)]
    # ...and once it landed, the line the next resume reads carries no marker.
    assert db.run_state_get(box["run_id"])["audit_segment_id"] is None
    db.close()


def test_a_slow_closing_append_does_not_invent_a_gap_for_the_next_resume(
    tmp_path: Path,
) -> None:
    """The reviewer's repro: delay only the ``segment.completed`` commit, resume
    at once, and watch a run in which nothing failed acquire a permanent
    ``audit.gap`` of ``unavailable``/``count=null``."""
    db = SessionDB(str(tmp_path / "slow-close.db"))
    service = _checkpoint_service(db, tmp_path)
    release = threading.Event()
    original = db.audit_append

    def slow(event: dict[str, Any], **kwargs: Any) -> int:
        if event.get("event_type") == "segment.completed":
            threading.Timer(0.25, release.set).start()
            release.wait(5)
        return original(event, **kwargs)

    db.audit_append = slow  # type: ignore[method-assign]
    try:
        run_id = service.start(_CHECKPOINT_SPEC)["run_id"]
        assert service.status(run_id, wait=True)["status"] == "paused"
        assert service.start(
            resume_run_id=run_id, checkpoint_answers={"approve": "go"}
        )["run_id"] == run_id
        assert service.status(run_id, wait=True)["status"] == "complete"
    finally:
        service.shutdown()
    assert not [
        event for event in db.audit_events(run_id) if event["event_type"] == "audit.gap"
    ]
    db.close()


def test_transient_sqlite_busy_is_retried_not_dropped(tmp_path):
    # CI 3.11 (runner 2-core lento): BUSY transiente no append virava drop +
    # audit.gap em cenários que o contrato promete completos. O writer é
    # assíncrono — retry limitado converte o transiente em sucesso; contenção
    # PERSISTENTE continua degradando visível (o contrato de perda-visível).
    from lohra.state import SessionDB
    from lohra.workflow.audit import AuditTrail

    db = SessionDB(str(tmp_path / "state.db"))
    calls = {"n": 0}
    original = db.audit_append

    def flaky(event, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            import sqlite3

            raise sqlite3.OperationalError("database is locked")
        return original(event, **kwargs)

    db.audit_append = flaky  # type: ignore[method-assign]
    trail = AuditTrail(db)
    try:
        trail.record({"event_type": "segment.started", "identity": {"run_id": "r1"}})
        assert trail.flush(timeout=5)
    finally:
        trail.shutdown(timeout=5)
    events = db.audit_events("r1")
    assert [e["event_type"] for e in events] == ["segment.started"]  # sem gap
    assert calls["n"] >= 3  # re-tentou de verdade
    db.close()


# --- issue #34: contenção SQLite — warnings agregados, drain final, knob ---


def test_contention_window_recovers_after_the_lock_clears(tmp_path, monkeypatch, caplog):
    """Repro determinística: uma 2ª conexão segura o write lock (BEGIN
    IMMEDIATE); o primeiro append esgota o retry (1 warning, perda visível em
    gap); ao soltar o lock, o resto drena sem perda."""
    import logging

    from lohra.workflow import audit as auditmod

    monkeypatch.setenv("LOHRA_AUDIT_BUSY_TIMEOUT_MS", "10")
    monkeypatch.setattr(auditmod, "_BUSY_RETRY_DELAYS", (0.01,))
    path = str(tmp_path / "state.db")
    db = SessionDB(path)
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")

    trail = AuditTrail(db)
    try:
        with caplog.at_level(logging.WARNING, logger="lohra.workflow.audit"):
            for _ in range(5):
                trail.record(
                    {"event_type": "segment.started", "identity": {"run_id": "r1"}}
                )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if any("SQLite contention" in r.message for r in caplog.records):
                    break
                time.sleep(0.02)
            contention = [r for r in caplog.records if "SQLite contention" in r.message]
            assert len(contention) == 1  # a 1ª falha avisa NA HORA, uma vez

            blocker.rollback()  # solta o write lock: o sink volta a escrever
            assert trail.flush(timeout=10)
    finally:
        trail.shutdown(timeout=10)
        blocker.close()

    # perda visível preservada: o que falhou virou gap contado; o resto chegou
    events = db.audit_events("r1")
    gaps = [e for e in events if e["event_type"] == "audit.gap"]
    started = [e for e in events if e["event_type"] == "segment.started"]
    dropped = sum(g["data"]["dropped_count"] for g in gaps)
    assert dropped >= 1 and dropped + len(started) == 5  # nada some sem marker
    db.close()


def test_contention_warnings_are_rate_limited_with_a_count(monkeypatch, caplog):
    """709 falhas num turno = punhado de linhas contadas, não 709: a 1ª falha
    de um período quieto loga na hora; as seguintes só contam dentro do
    intervalo; a saída do sink drena o restante numa linha com a contagem."""
    import logging

    from lohra.workflow import audit as auditmod

    monkeypatch.setattr(auditmod, "_BUSY_RETRY_DELAYS", (0.001,))

    class _LockedForEvents:
        """Gaps entram; qualquer outro append 'está locked' — o padrão real de
        contenção em que os markers acham janela e os eventos não."""

        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def audit_append(self, event: dict[str, Any], **_: Any) -> int:
            if event.get("event_type") == "audit.gap":
                self.events.append(event)
                return len(self.events)
            raise sqlite3.OperationalError("database is locked")

    db = _LockedForEvents()
    trail = AuditTrail(db)
    with caplog.at_level(logging.WARNING, logger="lohra.workflow.audit"):
        for _ in range(5):
            trail.record(
                {"event_type": "segment.started", "identity": {"run_id": "r1"}}
            )
        trail.flush(timeout=10)
        trail.shutdown(timeout=10)

    contention = [r for r in caplog.records if "SQLite contention" in r.message]
    # 2 linhas para 5 falhas: a imediata ("1 append(s)") e o drain da saída
    # com o restante ("4 append(s)") — nunca uma por evento.
    assert len(contention) == 2
    assert "1 append(s)" in contention[0].message
    assert "4 append(s)" in contention[1].message
    # e a perda continua 100% visível nos gaps que os markers gravaram
    assert sum(g["data"]["dropped_count"] for g in db.events) == 5


def test_final_drain_lands_pending_markers_before_abandoning(tmp_path, monkeypatch):
    """O caminho de abandono do shutdown agora tenta um drain final paciente:
    markers pendentes que o loop desistiria são escritos se o banco aceitar."""
    monkeypatch.setattr(AuditTrail, "_run", lambda self: None)  # sem writer thread
    db = SessionDB(str(tmp_path / "state.db"))
    trail = AuditTrail(db)
    trail.record_gap("r1", "queue_overflow", count=3)
    trail.record_gap("r2", "sink_failure", count=1)

    failed = trail._final_marker_drain(None)

    assert failed == 0
    gap_runs = {e["identity"]["run_id"] for e in db.audit_events("r1")} | {
        e["identity"]["run_id"] for e in db.audit_events("r2")
    }
    assert gap_runs == {"r1", "r2"}
    db.close()


def test_final_drain_reports_the_abandoned_count_when_the_sink_is_dead(
    tmp_path, monkeypatch, caplog
):
    import logging

    monkeypatch.setattr(AuditTrail, "_run", lambda self: None)
    db = SessionDB(str(tmp_path / "state.db"))

    def dead(event, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    db.audit_append = dead  # type: ignore[method-assign]
    trail = AuditTrail(db)
    trail.record_gap("r1", "queue_overflow", count=2)
    with caplog.at_level(logging.WARNING, logger="lohra.workflow.audit"):
        failed = trail._final_marker_drain(None)
    assert failed == 1
    abandoned = [r for r in caplog.records if "abandoned" in r.message]
    assert len(abandoned) == 1 and "1" in abandoned[0].message  # com contagem
    db.close()


def test_audit_busy_timeout_env_knob(tmp_path, monkeypatch):
    """O busy_timeout da conexão do audit é do operador: default 250ms (mata a
    maioria dos BUSY sob fan-out sem convoy — conexão/lock próprios), lixo
    degrada pro default, e o repro de contenção usa um valor minúsculo."""
    monkeypatch.setenv("LOHRA_AUDIT_BUSY_TIMEOUT_MS", "700")
    db = SessionDB(str(tmp_path / "a.db"))
    assert db._audit_connection.execute("PRAGMA busy_timeout").fetchone()[0] == 700
    db.close()

    monkeypatch.setenv("LOHRA_AUDIT_BUSY_TIMEOUT_MS", "garbage")
    db = SessionDB(str(tmp_path / "b.db"))
    assert db._audit_connection.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    db.close()

    monkeypatch.delenv("LOHRA_AUDIT_BUSY_TIMEOUT_MS")
    db = SessionDB(str(tmp_path / "c.db"))
    assert db._audit_connection.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    db.close()


def test_shutdown_lands_markers_from_the_caller_thread_as_last_chance(tmp_path, monkeypatch):
    """A contenção que o drain in-thread perde é tipicamente a transação do
    PRÓPRIO processo; na thread que chama shutdown() ela já acabou — a última
    chance roda lá, depois da morte do daemon, e torna o gap durável."""
    from lohra.workflow import audit as auditmod

    monkeypatch.setattr(auditmod, "_BUSY_RETRY_DELAYS", (0.001,))
    db = SessionDB(str(tmp_path / "state.db"))
    original = db.audit_append

    def writer_thread_is_locked(event, **kwargs):
        if threading.current_thread().name == "workflow-audit":
            raise sqlite3.OperationalError("database is locked")
        return original(event, **kwargs)

    db.audit_append = writer_thread_is_locked  # type: ignore[method-assign]
    trail = AuditTrail(db)
    trail.record({"event_type": "segment.started", "identity": {"run_id": "r1"}})
    trail.flush(timeout=5)  # o evento esgota e vira marker que o daemon não grava

    assert trail.shutdown(timeout=5) is True  # a última chance drenou tudo
    gaps = [e for e in db.audit_events("r1") if e["event_type"] == "audit.gap"]
    assert len(gaps) == 1 and gaps[0]["data"]["dropped_count"] == 1
    db.close()


def test_old_tombstones_table_gains_next_seq_on_open(tmp_path):
    """Causa raiz REAL do issue #34 ao vivo: um state.db criado antes do
    next_seq nas tombstones fazia TODO append de audit falhar com
    OperationalError ('no such column') — indistinguível de contenção no
    warning. Abrir o banco migra a coluna (DEFAULT 1 = semântica legada)."""
    path = str(tmp_path / "state.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE workflow_audit_tombstones ("
        "run_id TEXT PRIMARY KEY, reason TEXT NOT NULL, evicted_at REAL NOT NULL)"
    )
    conn.commit()
    conn.close()

    db = SessionDB(path)
    trail = AuditTrail(db)
    trail.record({"event_type": "segment.started", "identity": {"run_id": "r1"}})
    assert trail.flush(timeout=5)
    assert trail.shutdown(timeout=5) is True
    assert [e["event_type"] for e in db.audit_events("r1")] == ["segment.started"]
    db.close()
