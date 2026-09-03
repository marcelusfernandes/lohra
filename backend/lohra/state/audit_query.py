"""Bounded read model for the durable workflow audit ledger (OBS-04).

One SQLite snapshot is loaded at a time.  OBS-03 bounds that snapshot to 2,048
rows / 4 MiB of event JSON, so exact integrity disclosures do not require an
unbounded scan.  The returned event page and notice list are bounded separately.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

MAX_QUERY_EVENTS = 100
MAX_QUERY_NOTICES = 20
# Re-routes are rare by construction — one per ``route_fault`` pause somebody
# answered — so a run's whole set fits beside a single page. Bounded anyway,
# like every other list this reader returns.
MAX_QUERY_REROUTES = 20
_MARKER_TYPES = frozenset({"audit.gap", "audit.truncated", "audit.unavailable"})
_REROUTED = "node.rerouted"
_REROUTE_FIELDS = ("node_id", "from", "to", "channel")
_FIELD_STATES = frozenset(
    {"redacted", "truncated", "unavailable", "excluded_by_policy", "excluded_private_state"}
)


def _unavailable(run_id: str, seq: int | None, reason: str, created_at: float | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": 1,
        "event_type": "audit.unavailable",
        "provenance": "unavailable",
        "identity": {"run_id": run_id},
        "data": {"reason": reason},
    }
    if seq is not None:
        event["seq"] = seq
    if created_at is not None:
        event["created_at"] = created_at
    return event


def _decode(run_id: str, row: Any) -> dict[str, Any]:
    seq, payload, created_at = int(row[0]), row[1], float(row[2])
    try:
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError("audit payload is not an object")
        # Repeat the writer's allow-list for legacy/tampered but valid JSON.
        # The public reader never trusts persistence to imply sanitization.
        from lohra.workflow.audit import sanitize_audit_event

        safe = sanitize_audit_event(event)
        return {**safe, "seq": seq, "created_at": created_at}
    except (TypeError, ValueError, json.JSONDecodeError):
        return _unavailable(run_id, seq, "corrupt_payload", created_at)


def _count_field_states(value: Any, counts: Counter[str]) -> None:
    if isinstance(value, dict):
        state = value.get("state")
        if isinstance(state, str) and state in _FIELD_STATES:
            counts[state] += 1
        for item in value.values():
            _count_field_states(item, counts)
    elif isinstance(value, list):
        for item in value:
            _count_field_states(item, counts)


def _reroutes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ``node.rerouted`` in the snapshot, flattened to its four facts.

    Read off the already-sanitized events, so the summary can never say more
    than the ledger does; a legacy row missing a field simply has none.
    """
    summary: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != _REROUTED:
            continue
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        summary.append(
            {"seq": int(event["seq"])}
            | {name: data[name] for name in _REROUTE_FIELDS if name in data}
        )
    return summary


def _matches(
    event: dict[str, Any], *, node_id: str | None, event_type: str | None,
    sub_id: str | None, segment_id: str | None, attempt: int | None,
) -> bool:
    identity = event.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    node_path = identity.get("node_path")
    actual_node = node_path[-1] if isinstance(node_path, list) and node_path else None
    return not (
        (node_id is not None and actual_node != node_id)
        or (event_type is not None and event.get("event_type") != event_type)
        or (sub_id is not None and identity.get("sub_id") != sub_id)
        or (segment_id is not None and identity.get("segment_id") != segment_id)
        or (attempt is not None and identity.get("attempt") != attempt)
    )


def query(
    connection: Any,
    lock: Any,
    run_id: str,
    *,
    node_id: str | None = None,
    event_type: str | None = None,
    sub_id: str | None = None,
    segment_id: str | None = None,
    attempt: int | None = None,
    after_seq: int = 0,
    snapshot_seq: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return one chronological page plus run-wide integrity disclosures.

    ``snapshot_seq`` freezes multi-page reads.  Omit it to include the newest
    committed tail, which is useful while another process is still writing.
    Filtering affects only ``events``; integrity notices remain run-wide so a
    node filter can never hide that the retained trail has a gap.
    """
    requested_limit = int(limit)
    effective_limit = min(MAX_QUERY_EVENTS, max(1, requested_limit))
    with lock:
        try:
            connection.execute("BEGIN")
            state = connection.execute(
                """SELECT retention_dropped, dropped_before_seq
                   FROM workflow_audit_state WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            tombstone = connection.execute(
                "SELECT reason FROM workflow_audit_tombstones WHERE run_id = ?", (run_id,)
            ).fetchone()
            rows = connection.execute(
                """SELECT seq, payload_json, created_at FROM workflow_audit_events
                   WHERE run_id = ? ORDER BY seq""",
                (run_id,),
            ).fetchall()
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    decoded = [_decode(run_id, row) for row in rows]
    current_highwater = max((int(event["seq"]) for event in decoded), default=0)
    frozen_highwater = (
        current_highwater if snapshot_seq is None else min(current_highwater, max(0, snapshot_seq))
    )

    notices: list[dict[str, Any]] = []
    if state is not None and int(state[0] or 0):
        dropped = int(state[0])
        data: dict[str, Any] = {
            "reason": "tombstone_compaction" if dropped < 0 else "retention_limit",
            "dropped_count": None if dropped < 0 else dropped,
            "before_seq": int(state[1] or 0),
        }
        if dropped < 0:
            data["count_state"] = "unavailable"
        notices.append(
            {"event_type": "audit.gap", "provenance": "dropped", "data": data}
        )
    if state is None and tombstone is not None:
        notices.append(
            {"event_type": "audit.unavailable", "provenance": "unavailable",
             "data": {"reason": str(tombstone[0])}}
        )
    elif state is None and not decoded:
        notices.append(
            {"event_type": "audit.unavailable", "provenance": "unavailable",
             "data": {"reason": "not_recorded"}}
        )

    # Integrity and filters share one stable retained snapshot. A live append
    # after page one cannot make disclosures drift midway through pagination.
    snapshot_events = [event for event in decoded if int(event["seq"]) <= frozen_highwater]
    field_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    for event in snapshot_events:
        _count_field_states(event, field_counts)
        kind = event.get("event_type")
        if kind in _MARKER_TYPES:
            event_counts[str(kind)] += 1
            notices.append(event)
    for notice in notices:
        kind = notice.get("event_type")
        if kind in _MARKER_TYPES and "seq" not in notice:
            event_counts[str(kind)] += 1

    eligible = [
        event for event in snapshot_events
        if after_seq < int(event["seq"])
        and _matches(
            event, node_id=node_id, event_type=event_type, sub_id=sub_id,
            segment_id=segment_id, attempt=attempt,
        )
    ]
    page_events = eligible[:effective_limit]
    has_more = len(eligible) > effective_limit
    next_after = int(page_events[-1]["seq"]) if page_events else after_seq
    filters = {
        key: value for key, value in {
            "node_id": node_id,
            "event_type": event_type,
            "sub_id": sub_id,
            "segment_id": segment_id,
            "attempt": attempt,
        }.items() if value is not None
    }
    availability = "available" if state is not None or decoded else "unavailable"
    returned_notices = notices[:MAX_QUERY_NOTICES]
    reroutes = _reroutes(snapshot_events)
    return {
        "run_id": run_id,
        "availability": availability,
        "filters": filters,
        "events": page_events,
        # Run-wide, exactly like the integrity notices and for the same reason:
        # a node filter or a page boundary must never be able to hide that a
        # route was MOVED mid-run (issue #64). Counting it here is what lets a
        # reader ask "was anything re-routed?" without paging the whole trail.
        "routing": {
            "rerouted": len(reroutes),
            "reroutes": reroutes[:MAX_QUERY_REROUTES],
            "reroutes_truncated": len(reroutes) > MAX_QUERY_REROUTES,
        },
        "page": {
            "after_seq": after_seq,
            "next_after_seq": next_after,
            "snapshot_seq": frozen_highwater,
            "limit_requested": requested_limit,
            "limit_effective": effective_limit,
            "limit_clamped": requested_limit != effective_limit,
            "returned": len(page_events),
            "has_more": has_more,
        },
        "policy": {
            "mode": "metadata_only",
            "raw_payloads": "redacted_or_excluded_at_ingest_and_read",
            "private_reasoning": "excluded_private_state",
            "provider_calls": "none",
            "summary_generated": False,
        },
        "integrity": {
            "scope": "retained_snapshot",
            "event_markers": {
                "gaps": event_counts["audit.gap"],
                "truncated": event_counts["audit.truncated"],
                "unavailable": event_counts["audit.unavailable"],
            },
            "field_markers": {state: field_counts[state] for state in sorted(_FIELD_STATES)},
            "pagination_truncated": has_more,
            "notices": returned_notices,
            "notices_total": len(notices),
            "notices_returned": len(returned_notices),
            "notices_truncated": len(notices) > len(returned_notices),
        },
    }
