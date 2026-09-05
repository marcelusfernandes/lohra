"""The `memory` tool — intercepted, session-bound (spec §2, §6).

Schema lives in the registry so the model sees the tool; execution is bound to
the session's MemoryStore via the intercept dispatcher. Handlers return JSON
envelopes and never raise into the dispatcher.
"""

from __future__ import annotations

from typing import Any

from lohra.memory.store import MemoryError, MemoryStore
from lohra.tools.registry import registry, tool_error, tool_result

MEMORY_GUIDANCE = (
    "Save durable facts that should persist across sessions. Save proactively when "
    "the user corrects you, shares a preference or habit, or you learn a convention. "
    "Classify failure causes before saving: agency = the author's own bad choice "
    "(e.g. picking a model slug that doesn't exist); environment = the surrounding "
    "system misbehaving (e.g. a provider quota or timeout). No evidence of "
    "environment means agency. Do NOT save task progress, completed-work logs, or "
    "temporary TODOs — procedures belong in skills, not memory. Write declarative "
    "facts ('User prefers tabs'), not instructions to yourself."
)

_SCHEMA = {
    "description": MEMORY_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove"]},
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "memory = agent notes (default); user = user profile",
            },
            "text": {"type": "string", "description": "Entry text for 'add'"},
            "old_text": {"type": "string", "description": "Unique substring to find (replace/remove)"},
            "new_text": {"type": "string", "description": "Replacement entry text (replace)"},
        },
        "required": ["action"],
    },
}


class MemoryTool:
    """Executes memory actions against one session's MemoryStore."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def handle(self, args: dict[str, Any]) -> str:
        action = args.get("action")
        target = args.get("target", "memory")
        memory_file = self.store.file_for(target)
        try:
            if action == "add":
                text = args.get("text")
                if not text:
                    return tool_error("'add' requires 'text'")
                memory_file.add(text)
            elif action == "replace":
                old_text, new_text = args.get("old_text"), args.get("new_text")
                if not old_text or new_text is None:
                    return tool_error("'replace' requires 'old_text' and 'new_text'")
                memory_file.replace(old_text, new_text)
            elif action == "remove":
                old_text = args.get("old_text")
                if not old_text:
                    return tool_error("'remove' requires 'old_text'")
                memory_file.remove(old_text)
            else:
                return tool_error(f"unknown action {action!r} (use add/replace/remove)")
        except MemoryError as exc:
            return tool_error(str(exc))
        return tool_result(target=target, entry_count=len(memory_file.entries()))


def register_memory_tool_schema() -> None:
    """Register the memory schema so the model sees it (execution is intercepted)."""
    registry.register(
        "memory",
        "memory",
        _SCHEMA,
        _intercepted_handler,
        override=True,
        emoji="🧠",
        author_time_only=True,
    )


def _intercepted_handler(_args: dict[str, Any], **_kwargs: Any) -> str:
    # Reached only if not intercepted — a wiring bug. Fail loudly but safely.
    return tool_error("the memory tool must be intercepted with a session MemoryStore")
