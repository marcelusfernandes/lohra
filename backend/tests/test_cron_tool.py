"""Tests for the cronjob tool — intercepted, session-bound (spec §6, §9)."""

import json

import pytest

from lohra.cron.store import CronStore
from lohra.cron.tool import CronTool


@pytest.fixture
def tool(tmp_path):
    return CronTool(CronStore(tmp_path))


def _call(tool, **args):
    return json.loads(tool.handle(args))


def test_add_creates_a_job(tool):
    out = _call(tool, action="add", name="daily", prompt="summarize my day",
                schedule_type="interval", value=1440)
    assert out["ok"] is True
    assert out["job_id"]
    assert tool.store.get(out["job_id"])["name"] == "daily"


def test_list_returns_jobs(tool):
    tool.store.add(name="a", prompt="p", type="interval", value=5)
    out = _call(tool, action="list")
    assert out["ok"] is True
    assert len(out["jobs"]) == 1
    assert out["jobs"][0]["name"] == "a"


def test_remove(tool):
    job = tool.store.add(name="a", prompt="p", type="interval", value=5)
    out = _call(tool, action="remove", job_id=job["id"])
    assert out["ok"] is True
    assert tool.store.get(job["id"]) is None


def test_remove_unknown_errors(tool):
    out = _call(tool, action="remove", job_id="nope")
    assert "error" in out


def test_pause_and_resume(tool):
    job = tool.store.add(name="a", prompt="p", type="interval", value=5)
    assert _call(tool, action="pause", job_id=job["id"])["ok"] is True
    assert tool.store.get(job["id"])["enabled"] is False
    assert _call(tool, action="resume", job_id=job["id"])["ok"] is True
    assert tool.store.get(job["id"])["enabled"] is True


def test_add_missing_fields_errors(tool):
    out = _call(tool, action="add", name="x")  # no prompt/schedule
    assert "error" in out


def test_add_invalid_cron_errors(tool):
    out = _call(tool, action="add", name="x", prompt="p", schedule_type="cron", value="bad")
    assert "error" in out


def test_unknown_action_errors(tool):
    out = _call(tool, action="frobnicate")
    assert "error" in out


def test_target_action_requires_job_id(tool):
    assert "error" in _call(tool, action="pause")
    assert "error" in _call(tool, action="remove")


def test_register_schema_and_intercepted_fallback():
    from lohra.cron.tool import register_cron_tool_schema
    from lohra.tools import registry

    register_cron_tool_schema()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "cronjob" in names
    out = json.loads(registry.dispatch("cronjob", {"action": "list"}))
    assert "error" in out  # must be intercepted with a session store
