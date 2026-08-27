"""Cron schedule computation — pure, clock-injected (spec §6).

``is_due(job, now)`` decides whether a job should fire at epoch ``now``. Three
schedule types: ``once`` (run_at epoch), ``interval`` (minutes between runs),
``cron`` (a 5-field expression: minute hour day-of-month month day-of-week,
day-of-week 0=Sunday). No external deps — a small cron-field parser does the
matching.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# (low, high) bounds per cron field position. Weekday allows 7 (== Sunday, the
# standard cron alias for 0).
_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def parse_cron_field(field: str, low: int, high: int) -> set[int]:
    """Expand one cron field (``*``, ``a``, ``a-b``, ``a,b``, ``*/n``) to a set."""
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            start, end = int(start_str), int(end_str)
        else:
            start = end = int(part)
        if start < low or end > high or start > end or step < 1:
            raise ValueError(f"cron field out of range: {field!r}")
        values.update(range(start, end + 1, step))
    return values


def cron_matches(expr: str, when: datetime) -> bool:
    """True if ``expr`` (5 fields) fires at ``when`` (cron day-of-week 0=Sunday)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression needs 5 fields, got {len(fields)}: {expr!r}")
    # cron weekday: 0=Sunday..6=Saturday (Python isoweekday: 1=Mon..7=Sun)
    weekday = when.isoweekday() % 7
    for i, (field, (low, high)) in enumerate(zip(fields, _FIELD_BOUNDS)):
        allowed = parse_cron_field(field, low, high)
        if i == 4:  # weekday: accept both 0 and 7 for Sunday
            value = weekday in allowed or (weekday == 0 and 7 in allowed)
        else:
            value = (when.minute, when.hour, when.day, when.month)[i] in allowed
        if not value:
            return False
    return True


def _minute_floor(epoch: float) -> float:
    return epoch - (epoch % 60)


def is_due(job: dict[str, Any], *, now: float) -> bool:
    """Return whether ``job`` should run at epoch ``now``."""
    if not job.get("enabled", True):
        return False
    job_type = job.get("type")
    value = job.get("value")
    last_run = job.get("last_run_at")

    if job_type == "once":
        return last_run is None and now >= float(value)
    if job_type == "interval":
        if last_run is None:
            return True
        return (now - last_run) >= float(value) * 60
    if job_type == "cron":
        if not cron_matches(str(value), datetime.fromtimestamp(now)):
            return False
        # don't fire twice within the same wall-clock minute
        return last_run is None or last_run < _minute_floor(now)
    raise ValueError(f"unknown job type {job_type!r}")
