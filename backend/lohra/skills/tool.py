"""The `skill_view` and `skill_manage` tools — intercepted, session-bound (§4, §6).

Schemas live in the registry; execution binds to the session's SkillStore via
the intercept dispatcher. Handlers return JSON envelopes and never raise.
"""

from __future__ import annotations

from typing import Any

from lohra.skills.store import SkillError, SkillStore
from lohra.tools.registry import registry, tool_error, tool_result

_VIEW_SCHEMA = {
    "description": "Load the full body of a skill by name (progressive disclosure).",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Skill name to load"}},
        "required": ["name"],
    },
}

_MANAGE_GUIDANCE = (
    "Skills are procedural memory. create one when a task was complex (5+ steps), "
    "you overcame non-obvious errors, or a workflow is worth reusing. Before "
    "skilling an error workaround, classify it: agency = your own bad choice "
    "(e.g. picking a model slug that doesn't exist) — fix the choice, don't "
    "skill it; environment = the surrounding system misbehaving (e.g. a "
    "provider quota or timeout) — that's worth a skill. No evidence of "
    "environment means agency. update one that's stale or wrong (edits it in "
    "place — a project skill is edited in the project). delete removes a "
    "skill (home skills only). For a project-specific skill, create with "
    "scope='project'. Bodies: concise, reusable instructions."
)

_MANAGE_SCHEMA = {
    "description": _MANAGE_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "delete"]},
            "name": {"type": "string", "description": "Skill name (lowercase, hyphens, ≤64)"},
            "description": {"type": "string", "description": "One-line description (create/update)"},
            "body": {"type": "string", "description": "Markdown instructions (create/update)"},
            "scope": {
                "type": "string",
                "enum": ["home", "project"],
                "description": "Where create writes (default home; 'project' = the project's skills)",
            },
        },
        "required": ["action", "name"],
    },
}


class SkillTool:
    """Executes skill_view / skill_manage against one session's SkillStore."""

    def __init__(self, store: SkillStore) -> None:
        self.store = store

    def view(self, args: dict[str, Any]) -> str:
        name = args.get("name")
        if not name:
            return tool_error("'skill_view' requires 'name'")
        skill = self.store.get(name)
        if skill is None:
            return tool_error(f"no skill named {name!r}")
        return tool_result(name=skill.name, version=skill.version, body=skill.body)

    def manage(self, args: dict[str, Any]) -> str:
        action = args.get("action")
        name = args.get("name")
        if not name:
            return tool_error("'skill_manage' requires 'name'")
        try:
            if action == "create":
                description = args.get("description") or ""
                body = args.get("body") or ""
                if not body:
                    return tool_error("'create' requires 'body'")
                scope = args.get("scope") or "home"
                skill = self.store.create(name, description, body, scope=scope)
                return tool_result(action="create", name=skill.name, scope=scope)
            if action == "update":
                if args.get("description") is None and args.get("body") is None:
                    return tool_error("'update' requires 'description' or 'body'")
                skill = self.store.update(
                    name, description=args.get("description"), body=args.get("body")
                )
                return tool_result(action="update", name=skill.name)
            if action == "delete":
                removed = self.store.delete(name)
                if not removed:
                    return tool_error(f"no skill named {name!r}")
                return tool_result(action="delete", name=name)
            return tool_error(f"unknown action {action!r} (use create/update/delete)")
        except SkillError as exc:
            return tool_error(str(exc))
        except OSError as exc:  # a planted symlink-to-file etc. must not break never-raise
            return tool_error(f"skill filesystem error: {exc}")


def register_skill_tool_schemas() -> None:
    """Register skill_view + skill_manage schemas (execution is intercepted)."""
    registry.register(
        "skill_view", "skills", _VIEW_SCHEMA, _intercepted, override=True, emoji="📖",
        author_time_only=True,
    )
    registry.register(
        "skill_manage", "skills", _MANAGE_SCHEMA, _intercepted, override=True, emoji="🛠️",
        author_time_only=True,
    )


def _intercepted(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("skill tools must be intercepted with a session SkillStore")
