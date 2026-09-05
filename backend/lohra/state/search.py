"""The `session_search` tool — intercepted, session-bound to the SessionDB (§1).

Four zero-LLM-cost ways to revisit past sessions:
- discovery: full-text (FTS5/BM25) search across all messages
- browse: recent sessions
- read: a whole session by id

(SCROLL — a ±window around a hit — reduces to read for now.)
"""

from __future__ import annotations

from typing import Any

from lohra.state.db import SessionDB
from lohra.tools.registry import registry, tool_error, tool_result

_SCHEMA = {
    "description": (
        "Search your past sessions at zero token cost. mode='discovery' full-text "
        "searches all messages (FTS5 syntax: AND default, OR, NOT, \"phrases\", "
        "prefix*); mode='browse' lists recent sessions; mode='read' returns a whole "
        "session by id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["discovery", "browse", "read"]},
            "query": {"type": "string", "description": "Search query (discovery)"},
            "session_id": {"type": "string", "description": "Session to read (read)"},
            "limit": {"type": "integer", "description": "Max results (discovery)"},
        },
        "required": ["mode"],
    },
}


class SessionSearchTool:
    """Executes session_search against one session's SessionDB."""

    def __init__(self, db: SessionDB) -> None:
        self.db = db

    def handle(self, args: dict[str, Any]) -> str:
        mode = args.get("mode")
        if mode == "discovery":
            query = args.get("query")
            if not query:
                return tool_error("'discovery' requires 'query'")
            return tool_result(hits=self.db.search(query, limit=int(args.get("limit", 10))))
        if mode == "browse":
            return tool_result(sessions=self.db.list_sessions())
        if mode == "read":
            session_id = args.get("session_id")
            if not session_id:
                return tool_error("'read' requires 'session_id'")
            return tool_result(messages=self.db.load_messages(session_id))
        return tool_error(f"unknown mode {mode!r} (use discovery/browse/read)")


def register_session_search_schema() -> None:
    registry.register(
        "session_search", "search", _SCHEMA, _intercepted, override=True, emoji="🔎",
        author_time_only=True,
    )


def _intercepted(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("session_search must be intercepted with a session SessionDB")
