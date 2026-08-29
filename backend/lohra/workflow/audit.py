"""Bounded, metadata-only workflow audit trail (OBS-03).

Gateway frames are an observation source, not a storage schema: this module
allow-lists metadata, excludes content/private replay state, and sends bounded
events through a non-blocking queue to SQLite.  A slow or failed audit sink may
lose audit detail, but it must never change workflow execution; every observed
loss is represented by a durable ``audit.gap`` when the sink recovers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from dataclasses import asdict
from itertools import islice
from typing import Any, Callable

from lohra.workflow.causality import CausalContext

logger = logging.getLogger(__name__)

AUDIT_SCHEMA_VERSION = 1
DEFAULT_QUEUE_LIMIT = 256
DEFAULT_MAX_EVENT_BYTES = 2048
DEFAULT_MAX_EVENTS_PER_RUN = 2048
DEFAULT_MAX_RUNS = 64
DEFAULT_RETENTION_SECONDS = 30 * 86400
DEFAULT_MAX_DROP_BUCKETS = 256

_PRIVATE_KEYS = frozenset(
    {
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "provider_data",
        "encrypted_content",
    }

)

_SAFE_STATES = frozenset(
    {
        "excluded_by_policy", "excluded_private_state", "not_observed",
        "not_yet_available", "observed", "redacted", "truncated",
        "unavailable",
    }
)
_SENSITIVE_FIELDS = _PRIVATE_KEYS | frozenset(
    {
        "prompt", "response", "text", "content", "args", "arguments",
        "result", "output", "command", "url", "error", "message", "cause",
    }
)
_SAFE_DATA_FIELDS = frozenset(
    {
        "arguments", "before_seq", "bytes", "cause", "characters",
        "count_state", "dropped_count", "limit_bytes", "original_bytes",
        "original_event_type", "private_state", "reason", "recovered_process",
        "result", "resume", "run_attribution", "size", "source", "state",
        "status", "tool_id", "tool_name", "tool_name_state",
        "top_level_items", "unit", "value",
    }
)


_EVENT_TYPES = frozenset(
    {
        "audit.gap", "audit.truncated", "audit.unavailable",
        "cache.missed", "cache.replayed", "cache.stored", "cache.unavailable",
        "leaf.completed", "leaf.failed", "leaf.started",
        "node.completed", "node.failed", "node.paused", "node.started",
        "segment.completed", "segment.started",
        "tool.completed", "tool.started", "workflow.fault",
    }
)
_PROVENANCE = frozenset({"dropped", "observed", "replayed", "synthetic", "truncated", "unavailable"})
_SAFE_STRING_VALUES = {
    "count_state": frozenset({"unavailable"}),
    "original_event_type": _EVENT_TYPES,
    "private_state": frozenset({"excluded_private_state", "not_observed"}),
    "reason": frozenset({
        "corrupt_payload", "drop_bucket_overflow", "process_crash",
        "queue_overflow", "retention_limit", "sink_failure",
        "tombstone_compaction", "unavailable",
    }),
    "run_attribution": frozenset({"unavailable"}),
    "source": frozenset({"gateway", "harness"}),
    "state": _SAFE_STATES | frozenset({"complete", "null", "pending", "running"}),
    "status": frozenset({
        "cancelled", "complete", "degraded", "error", "failed", "paused",
        "success", "unavailable",
    }),
    "tool_name_state": frozenset({"known_tool", "unknown_tool"}),
    "unit": frozenset({"bytes", "characters", "items", "top_level_items"}),
}
_OPAQUE_IDENTIFIER_FIELDS = frozenset({"tool_id"})
_SAFE_TOOL_NAMES = frozenset(
    {
        "collect_session", "cronjob", "delegate_task", "image_gen", "list_models",
        "memory", "read_file", "run_workflow", "session_search", "skill_manage",
        "skill_view", "spawn_session", "steer_session", "terminal", "vision_analyze",
        "web_fetch", "web_search", "workflow_cancel", "workflow_list",
        "workflow_pause", "workflow_status", "workflow_templates", "write_file",
    }
)


def _bounded_text(value: Any, limit: int) -> str | None:
    """Accept scalar identifiers only; never invoke an opaque object's repr/str."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (bool, int, float)):
        return str(value)[:limit]
    return None


def _safe_metadata(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Allow-list bounded metadata without traversing opaque content values."""
    marker = isinstance(value, dict) and value.get("state") in _SAFE_STATES
    if key is not None and key not in _SAFE_DATA_FIELDS:
        return {"state": "excluded_by_policy", "size": _observed_size(value)}
    if key in _SENSITIVE_FIELDS and not marker:
        return {"state": "excluded_by_policy", "size": _observed_size(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key in _OPAQUE_IDENTIFIER_FIELDS:
            return {"state": "observed", "characters": min(len(value), 256)}
        if key == "tool_name":
            if value in _SAFE_TOOL_NAMES:
                return value
            return {"state": "observed", "characters": min(len(value), 256)}
        allowed = _SAFE_STRING_VALUES.get(key or "")
        if allowed is not None and value in allowed:
            return value
        return {"state": "excluded_by_policy", "characters": min(len(value), 256)}
    if isinstance(value, bytes):
        return {"state": "unavailable", "bytes": len(value)}
    if depth >= 4:
        return {"state": "truncated", "side": "depth"}
    if isinstance(value, dict):
        # Already-redacted producer markers are metadata, not the sensitive value.
        if value.get("state") in _SAFE_STATES:
            return {
                safe_key: _safe_metadata(item, key=safe_key, depth=depth + 1)
                for raw_key, item in islice(value.items(), 16)
                if isinstance(raw_key, str)
                for safe_key in (raw_key[:64],)
                if safe_key in _SAFE_DATA_FIELDS
            }
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(islice(value.items(), 16)):
            safe_key = raw_key[:64] if isinstance(raw_key, str) else f"field_{index}_unavailable"
            result[safe_key] = _safe_metadata(item, key=safe_key, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item, depth=depth + 1) for item in value[:32]]
    return {"state": "unavailable", "type": type(value).__name__[:64]}


def _observed_size(value: Any) -> dict[str, Any]:
    """Constant-work size metadata; never serialize or traverse user content."""
    if value is None:
        return {"state": "unavailable"}
    if isinstance(value, str):
        return {"state": "observed", "unit": "characters", "value": len(value)}
    if isinstance(value, bytes):
        return {"state": "observed", "unit": "bytes", "value": len(value)}
    if isinstance(value, (dict, list, tuple)):
        return {
            "state": "observed",
            "unit": "top_level_items",
            "value": len(value),
        }
    return {"state": "unavailable"}


def _identity(context: CausalContext, sub_id: str | None) -> dict[str, Any]:
    value = asdict(context)
    value["node_path"] = list(context.node_path)
    value["branch_path"] = list(context.branch_path)
    value["sub_id"] = sub_id
    return value


def _event(
    event_type: str,
    context: CausalContext,
    sub_id: str | None,
    data: dict[str, Any],
    *,
    provenance: str = "observed",
) -> dict[str, Any]:
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_type": event_type,
        "provenance": provenance,
        "identity": _identity(context, sub_id),
        "data": data,
    }


def causal_audit_event(
    event_type: str,
    context: CausalContext,
    *,
    data: dict[str, Any] | None = None,
    provenance: str = "observed",
    sub_id: str | None = None,
) -> dict[str, Any]:
    """Build an allow-listed audit event for workflow-owned observations."""
    return _event(event_type, context, sub_id, dict(data or {}), provenance=provenance)


def gateway_audit_event(
    frame: dict[str, Any], context: CausalContext, *, sub_id: str
) -> dict[str, Any] | None:
    """Translate one gateway frame into the stable metadata-first vocabulary.

    Deltas and private provider/reasoning state are excluded by policy.  Tool
    arguments/results and final assistant content are represented only by their
    byte size plus an explicit state; values and digests are never retained.
    """
    params = frame.get("params") if isinstance(frame, dict) else None
    if not isinstance(params, dict):
        return None
    kind = params.get("type")
    payload = params.get("payload")
    payload = payload if isinstance(payload, dict) else {}

    if kind == "message.delta":
        return None
    if kind == "message.start":
        return _event(
            "leaf.started",
            context,
            sub_id,
            {"content": {"state": "excluded_by_policy"}},
        )
    if kind == "message.complete":
        status = _bounded_text(payload.get("status"), 32) or "unavailable"
        return _event(
            "leaf.completed" if status == "complete" else "leaf.failed",
            context,
            sub_id,
            {
                "status": status,
                "content": {
                    "state": "excluded_by_policy",
                    "size": _observed_size(payload.get("text")),
                },
            },
        )
    if kind in {"tool.start", "tool.complete"}:
        complete = kind == "tool.complete"
        args = payload.get("args", payload.get("args_text"))
        result = payload.get("result")
        private_seen = any(key in payload for key in _PRIVATE_KEYS)
        return _event(
            "tool.completed" if complete else "tool.started",
            context,
            sub_id,
            {
                "tool_id": _bounded_text(
                    payload.get("tool_call_id", payload.get("tool_id")), 128
                ),
                "tool_name": (
                    _bounded_text(payload.get("name"), 128)
                    if payload.get("_audit_tool_name_known") is True
                    else None
                ),
                "tool_name_state": (
                    "known_tool" if payload.get("_audit_tool_name_known") is True
                    else "unknown_tool"
                ),
                "status": _bounded_text(payload.get("status"), 32),
                "arguments": {"state": "redacted", "size": _observed_size(args)},
                "result": {
                    "state": "redacted" if complete else "not_yet_available",
                    "size": _observed_size(result) if complete else {"state": "unavailable"},
                },
                "private_state": (
                    "excluded_private_state" if private_seen else "not_observed"
                ),
            },
        )
    if kind == "error":
        return _event(
            "leaf.failed",
            context,
            sub_id,
            {"cause": {"state": "redacted", "size": _observed_size(payload)}},
        )
    return None


def _gap_event(run_id: str, reason: str, count: int | None) -> dict[str, Any]:
    data: dict[str, Any] = {"reason": reason, "dropped_count": count}
    if count is None:
        data["count_state"] = "unavailable"
    if run_id == "$audit":
        data["run_attribution"] = "unavailable"
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_type": "audit.gap",
        "provenance": "dropped",
        "identity": {"run_id": run_id},
        "data": data,
    }


def _bounded(event: dict[str, Any], limit: int) -> dict[str, Any]:
    identity = event.get("identity") if isinstance(event.get("identity"), dict) else {}
    node_path = identity.get("node_path")
    branch_path = identity.get("branch_path")
    safe_identity: dict[str, Any] = {
        "run_id": _bounded_text(identity.get("run_id"), 128) or "$unavailable"
    }
    for key, limit_text in (
        ("segment_id", 128), ("role", 64), ("sub_id", 128)
    ):
        if key in identity:
            safe_identity[key] = _bounded_text(identity.get(key), limit_text)
    if "node_path" in identity:
        safe_identity["node_path"] = [
            text for part in (node_path if isinstance(node_path, (list, tuple)) else ())[-8:]
            if (text := _bounded_text(part, 64)) is not None
        ]
    if "branch_path" in identity:
        safe_identity["branch_path"] = [
            item for item in (branch_path if isinstance(branch_path, (list, tuple)) else ())[-8:]
            if isinstance(item, int)
        ]
    for key in ("item_index", "stage_index", "attempt", "turn"):
        if key in identity:
            value = identity.get(key)
            safe_identity[key] = value if isinstance(value, int) else None
    # Provider cache keys hash resolved prompts.  Never persist them: derive the
    # audit identity only from structural metadata, excluding retry/turn so a
    # logical cell remains stable across corrections and resumes.
    if "cell_id" in identity:
        structural = {
            key: value for key, value in safe_identity.items()
            if key not in {"attempt", "turn", "segment_id", "sub_id"}
        }
        safe_identity["cell_id"] = "audit:" + hashlib.sha256(
            json.dumps(structural, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest()[:24]
    event_type = _bounded_text(event.get("event_type"), 64)
    safe_event_type = event_type if event_type in _EVENT_TYPES else "audit.unavailable"
    provenance = _bounded_text(event.get("provenance"), 32)
    safe_provenance = provenance if provenance in _PROVENANCE else "unavailable"
    safe = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_type": safe_event_type,
        "provenance": safe_provenance,
        "identity": safe_identity,
        "data": _safe_metadata(event.get("data") if isinstance(event.get("data"), dict) else {}),
    }
    encoded = json.dumps(safe, ensure_ascii=True, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size <= limit:
        return safe
    compact = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_type": "audit.truncated",
        "provenance": "truncated",
        "identity": {"run_id": safe_identity["run_id"]},
        "data": {
            "state": "truncated",
            "original_bytes": size,
            "limit_bytes": limit,
            "original_event_type": safe["event_type"],
        },
    }
    compact_size = len(
        json.dumps(compact, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    )
    if compact_size <= limit:
        return compact
    # Identity fields are untrusted and may expand under JSON escaping.  The
    # final ASCII-only marker is deliberately tiny enough for the 512-byte floor.
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_type": "audit.truncated",
        "provenance": "truncated",
        "identity": {"run_id": "$unavailable"},
        "data": {"state": "truncated", "original_bytes": size, "limit_bytes": limit},
    }



class AuditTrail:
    """Non-blocking producer plus one daemon SQLite writer.

    Producer ordinals merge accepted events and bounded loss markers into one
    causal stream.  The producer never waits for SQLite; a failed sink leaves
    its marker pending and ``flush``/``shutdown`` report that honestly.
    """

    def __init__(
        self,
        db: Any,
        *,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        max_events_per_run: int = DEFAULT_MAX_EVENTS_PER_RUN,
        max_runs: int = DEFAULT_MAX_RUNS,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        max_drop_buckets: int = DEFAULT_MAX_DROP_BUCKETS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._queue: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue(
            maxsize=max(1, queue_limit)
        )
        self._max_event_bytes = max(512, max_event_bytes)
        self._max_events = max(1, max_events_per_run)
        self._max_runs = max(1, max_runs)
        self._max_drop_buckets = max(2, max_drop_buckets)
        self._retention = max(1.0, retention_seconds)
        self._clock = clock
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._accepting = True
        self._next_order = 1
        # key -> [first producer order, run_id, reason, count].  ``count=None``
        # is an explicit unknowable boundary and is never converted to 1.
        self._markers: dict[tuple[Any, ...], list[Any]] = {}
        self._marker_inflight = False
        self._thread = threading.Thread(target=self._run, name="workflow-audit", daemon=True)
        self._thread.start()

    def _order_locked(self) -> int:
        order = self._next_order
        self._next_order += 1
        return order

    def _add_marker_locked(
        self, run_id: str, reason: str, count: int | None, order: int, *, unique: bool
    ) -> None:
        key: tuple[Any, ...] = ("declared", run_id, reason, order) if unique else (run_id, reason)
        if key not in self._markers and len(self._markers) >= self._max_drop_buckets - 1:
            key = ("$audit", "drop_bucket_overflow")
            run_id, reason = "$audit", "drop_bucket_overflow"
            count = None if count is None else count
        marker = self._markers.get(key)
        if marker is None:
            self._markers[key] = [order, run_id, reason, count]
            return
        marker[0] = min(int(marker[0]), order)
        if marker[3] is None or count is None:
            marker[3] = None
        else:
            marker[3] = int(marker[3]) + count

    def record_gap(self, run_id: str, reason: str, *, count: int | None = None) -> bool:
        """Declare a loss boundary without competing for the ordinary event queue."""
        safe_run = _bounded_text(run_id, 128) or "$unavailable"
        candidate_reason = _bounded_text(reason, 64)
        safe_reason = (
            candidate_reason
            if candidate_reason in _SAFE_STRING_VALUES["reason"]
            else "unavailable"
        )
        with self._state_lock:
            if not self._accepting:
                return False
            order = self._order_locked()
            self._add_marker_locked(safe_run, safe_reason, count, order, unique=True)
        return True

    def record_gateway(
        self, frame: dict[str, Any], context: Any, *, sub_id: str
    ) -> bool:
        if not isinstance(context, CausalContext):
            return False
        event = gateway_audit_event(frame, context, sub_id=sub_id)
        return event is not None and self.record(event)

    def record(self, event: dict[str, Any]) -> bool:
        bounded = _bounded(event, self._max_event_bytes)
        identity = bounded.get("identity")
        run_id = identity.get("run_id") if isinstance(identity, dict) else None
        with self._state_lock:
            if not self._accepting:
                return False
            order = self._order_locked()
            try:
                self._queue.put_nowait((order, bounded))
            except queue.Full:
                if isinstance(run_id, str) and run_id:
                    self._add_marker_locked(
                        run_id, "queue_overflow", 1, order, unique=False
                    )
                return False
        return True

    def _next_marker(self) -> tuple[tuple[Any, ...], list[Any]] | None:
        with self._state_lock:
            if not self._markers:
                return None
            key = min(self._markers, key=lambda item: int(self._markers[item][0]))
            marker = self._markers.pop(key)
            self._marker_inflight = True
            return key, marker

    def _peek_marker_order(self) -> int | None:
        with self._state_lock:
            if not self._markers:
                return None
            return min(int(marker[0]) for marker in self._markers.values())

    def _finish_marker(self) -> None:
        with self._state_lock:
            self._marker_inflight = False

    def _note_sink_failure(self, event: dict[str, Any], order: int) -> None:
        identity = event.get("identity")
        run_id = identity.get("run_id") if isinstance(identity, dict) else None
        if not isinstance(run_id, str) or not run_id:
            return
        with self._state_lock:
            self._add_marker_locked(run_id, "sink_failure", 1, order, unique=False)

    def _append(self, event: dict[str, Any]) -> bool:
        try:
            self._db.audit_append(
                event,
                now=self._clock(),
                max_events=self._max_events,
                max_runs=self._max_runs,
                retention_seconds=self._retention,
            )
            return True
        except Exception as exc:
            # Exception prose may quote a rejected value; log the class only.
            logger.warning("workflow audit append failed (%s)", type(exc).__name__)
            return False

    def _run(self) -> None:
        pending: tuple[int, dict[str, Any]] | None = None
        marker_pending: tuple[tuple[Any, ...], list[Any]] | None = None
        while True:
            with self._state_lock:
                markers_pending = bool(self._markers) or self._marker_inflight
            if self._stop.is_set() and self._queue.unfinished_tasks == 0 and not markers_pending:
                return

            if marker_pending is not None:
                marker = marker_pending[1]
                gap = _bounded(
                    _gap_event(str(marker[1]), str(marker[2]), marker[3]),
                    self._max_event_bytes,
                )
                if self._append(gap):
                    marker_pending = None
                    self._finish_marker()
                else:
                    time.sleep(0.05)
                continue

            if pending is None:
                try:
                    pending = self._queue.get(timeout=0.05)
                except queue.Empty:
                    marker_pending = self._next_marker()
                    continue

            marker_order = self._peek_marker_order()
            if marker_order is not None and marker_order < pending[0]:
                marker_pending = self._next_marker()
                continue

            order, event = pending
            pending = None
            try:
                if not self._append(event):
                    self._note_sink_failure(event, order)
            finally:
                self._queue.task_done()

    def flush(self, *, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._state_lock:
                markers_pending = bool(self._markers) or self._marker_inflight
            if self._queue.unfinished_tasks == 0 and not markers_pending:
                return True
            time.sleep(0.005)
        return False

    def shutdown(self, *, timeout: float = 1.0) -> bool:
        with self._state_lock:
            self._accepting = False
        self._stop.set()
        clean = self.flush(timeout=timeout)
        self._thread.join(timeout=max(0.0, timeout))
        return clean and not self._thread.is_alive()

