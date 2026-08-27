"""CronStore — persistence for scheduled jobs in ``HOME/cron/jobs.json`` (spec §6).

Validates jobs at the boundary (fail fast), writes atomically (temp + rename),
and degrades gracefully on a corrupt file (treated as empty). Returns new dicts;
never mutates a job in place outside the lock.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from lohra.cron.schedule import cron_matches

_JOB_TYPES = ("once", "interval", "cron")


class CronError(ValueError):
    """A bad job definition — surfaced to the caller (tool/CLI/REST)."""


def _validate(name: str, prompt: str, job_type: str, value: Any) -> None:
    if not name or not name.strip():
        raise CronError("a job needs a non-empty 'name'")
    if not prompt or not prompt.strip():
        raise CronError("a job needs a non-empty 'prompt'")
    if job_type not in _JOB_TYPES:
        raise CronError(f"unknown job type {job_type!r} (use once/interval/cron)")
    if job_type == "interval":
        if not isinstance(value, (int, float)) or value <= 0:
            raise CronError("'interval' value must be minutes > 0")
    elif job_type == "once":
        if not isinstance(value, (int, float)):
            raise CronError("'once' value must be a run-at epoch timestamp")
    elif job_type == "cron":
        from datetime import datetime

        try:
            cron_matches(str(value), datetime.fromtimestamp(0))
        except ValueError as exc:
            raise CronError(f"invalid cron expression: {exc}") from exc


class CronStore:
    """Thread-safe store for cron jobs backed by a JSON file."""

    def __init__(self, home: Path) -> None:
        self._path = Path(home) / "cron" / "jobs.json"
        self._lock = threading.RLock()

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return []  # corrupt -> empty (don't crash the scheduler)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return jobs if isinstance(jobs, list) else []

    def _write(self, jobs: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"jobs": jobs}, indent=2))
        os.replace(tmp, self._path)  # atomic

    def list(self) -> list[dict]:
        with self._lock:
            return self._read()

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            return next((j for j in self._read() if j.get("id") == job_id), None)

    def add(self, *, name: str, prompt: str, type: str, value: Any) -> dict:
        _validate(name, prompt, type, value)
        job = {
            "id": uuid4().hex,
            "name": name,
            "prompt": prompt,
            "type": type,
            "value": value,
            "enabled": True,
            "created_at": time.time(),
            "last_run_at": None,
        }
        with self._lock:
            jobs = self._read()
            jobs.append(job)
            self._write(jobs)
        return job

    def remove(self, job_id: str) -> bool:
        with self._lock:
            jobs = self._read()
            remaining = [j for j in jobs if j.get("id") != job_id]
            if len(remaining) == len(jobs):
                return False
            self._write(remaining)
            return True

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        return self._mutate(job_id, lambda j: {**j, "enabled": enabled})

    def mark_run(self, job_id: str, *, when: float) -> bool:
        return self._mutate(job_id, lambda j: {**j, "last_run_at": when})

    def _mutate(self, job_id: str, change) -> bool:
        with self._lock:
            jobs = self._read()
            found = False
            out = []
            for job in jobs:
                if job.get("id") == job_id:
                    out.append(change(job))
                    found = True
                else:
                    out.append(job)
            if found:
                self._write(out)
            return found
