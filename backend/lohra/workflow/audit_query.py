"""Agent-facing, provider-free query adapter for OBS-04."""

from __future__ import annotations

from typing import Any

from lohra.state import SessionDB
from lohra.tools.registry import registry, tool_error, tool_result

AUDIT_QUERY_SCHEMA = {
    "description": (
        "Read the durable metadata-only audit trail for one workflow run. This is "
        "a local SQLite query: it creates no provider client and spends no model "
        "tokens. Events are chronological and paginated by durable seq. Reuse the "
        "returned snapshot_seq for stable pagination; omit it with after_seq to "
        "follow a live tail. Filters affect event rows, never integrity notices."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "Workflow run id."},
            "node_id": {"type": "string", "description": "Exact final node id."},
            "event_type": {"type": "string", "description": "Exact audit event type."},
            "sub_id": {"type": "string", "description": "Exact leaf sub-session id."},
            "segment_id": {"type": "string", "description": "Exact run segment id."},
            "attempt": {"type": "integer", "minimum": 0},
            "after_seq": {
                "type": "integer", "minimum": 0,
                "description": "Exclusive durable cursor (default 0).",
            },
            "snapshot_seq": {
                "type": "integer", "minimum": 0,
                "description": "High-water mark returned by page one for a stable scan.",
            },
            "limit": {
                "type": "integer", "minimum": 1,
                "description": "Rows to return (default 50, clamped to 100).",
            },
        },
        "required": ["run_id"],
    },
}


class WorkflowAuditTool:
    """A read-only DB adapter; deliberately has no WorkflowService/client."""

    def __init__(self, db: SessionDB) -> None:
        self._db = db

    def handle(self, args: dict[str, Any]) -> str:
        run_id = args.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            return tool_error("workflow_audit needs a non-empty 'run_id'")
        integer_fields = ("attempt", "after_seq", "snapshot_seq", "limit")
        for name in integer_fields:
            value = args.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                return tool_error(f"'{name}' must be an integer")
        for name in ("attempt", "after_seq", "snapshot_seq"):
            value = args.get(name)
            if value is not None and value < 0:
                return tool_error(f"'{name}' must be >= 0")
        if args.get("limit") is not None and args["limit"] < 1:
            return tool_error("'limit' must be >= 1")
        for name in ("node_id", "event_type", "sub_id", "segment_id"):
            value = args.get(name)
            if value is not None and not isinstance(value, str):
                return tool_error(f"'{name}' must be a string")
        filters = {
            name: args[name] for name in (
                "node_id", "event_type", "sub_id", "segment_id", "attempt",
                "after_seq", "snapshot_seq", "limit",
            ) if name in args
        }
        return tool_result(**self._db.audit_query(run_id, **filters))


def _intercepted(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("workflow_audit must be intercepted with a SessionDB")


def register_workflow_audit_schema() -> None:
    registry.register(
        "workflow_audit", "workflow", AUDIT_QUERY_SCHEMA, _intercepted,
        override=True, emoji="🔎",
    )
