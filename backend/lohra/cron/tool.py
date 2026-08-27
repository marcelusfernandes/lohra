"""The ``cronjob`` tool — intercepted, session-bound (spec §6, §9).

Schema lives in the registry so the model sees it; execution is bound to the
session's CronStore via the intercept dispatcher. Lets the agent schedule itself:
add/list/remove/pause/resume jobs that later run as forked agents.
"""

from __future__ import annotations

from typing import Any

from lohra.cron.store import CronError, CronStore
from lohra.tools.registry import registry, tool_error, tool_result

CRON_GUIDANCE = (
    "Schedule prompts to run later as autonomous agent turns. Use for recurring "
    "or one-off background work the user asked to automate (a daily summary, a "
    "periodic check). 'interval' value = minutes; 'once' value = an epoch "
    "timestamp; 'cron' value = a 5-field expression (min hour day month weekday, "
    "weekday 0=Sunday). Each run is isolated — write a fully self-contained prompt."
)

_SCHEMA = {
    "description": CRON_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "remove", "pause", "resume"]},
            "name": {"type": "string", "description": "Job name (for 'add')"},
            "prompt": {"type": "string", "description": "The instruction each run executes (for 'add')"},
            "schedule_type": {"type": "string", "enum": ["once", "interval", "cron"]},
            "value": {"description": "minutes (interval) | epoch (once) | cron expr (cron)"},
            "job_id": {"type": "string", "description": "Target job (remove/pause/resume)"},
        },
        "required": ["action"],
    },
}


def _summary(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "type": job.get("type"),
        "value": job.get("value"),
        "enabled": job.get("enabled"),
        "last_run_at": job.get("last_run_at"),
    }


class CronTool:
    """Executes cron actions against one session's CronStore."""

    def __init__(self, store: CronStore) -> None:
        self.store = store

    def handle(self, args: dict[str, Any]) -> str:
        action = args.get("action")
        try:
            if action == "add":
                return self._add(args)
            if action == "list":
                return tool_result(jobs=[_summary(j) for j in self.store.list()])
            if action in ("remove", "pause", "resume"):
                return self._target(action, args)
            return tool_error(f"unknown action {action!r} (use add/list/remove/pause/resume)")
        except CronError as exc:
            return tool_error(str(exc))

    def _add(self, args: dict[str, Any]) -> str:
        schedule_type = args.get("schedule_type")
        if not schedule_type:
            return tool_error("'add' requires 'schedule_type' (once/interval/cron)")
        job = self.store.add(
            name=args.get("name") or "",
            prompt=args.get("prompt") or "",
            type=schedule_type,
            value=args.get("value"),
        )
        return tool_result(job_id=job["id"], name=job["name"])

    def _target(self, action: str, args: dict[str, Any]) -> str:
        job_id = args.get("job_id")
        if not job_id:
            return tool_error(f"'{action}' requires 'job_id'")
        if action == "remove":
            ok = self.store.remove(job_id)
        else:
            ok = self.store.set_enabled(job_id, action == "resume")
        if not ok:
            return tool_error(f"no job with id {job_id!r}")
        return tool_result(job_id=job_id, action=action)


def register_cron_tool_schema() -> None:
    """Register the cronjob schema so the model sees it (execution is intercepted)."""
    registry.register("cronjob", "cronjob", _SCHEMA, _intercepted_handler, override=True, emoji="⏰")


def _intercepted_handler(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("the cronjob tool must be intercepted with a session CronStore")
