"""Tests for CronStore — jobs.json persistence (spec §6)."""

import json

import pytest

from lohra.cron.store import CronError, CronStore


@pytest.fixture
def store(tmp_path):
    return CronStore(tmp_path)


def test_empty_store_lists_nothing(store):
    assert store.list() == []


def test_add_assigns_id_and_defaults(store):
    job = store.add(name="daily", prompt="summarize", type="interval", value=60)
    assert job["id"]
    assert job["enabled"] is True
    assert job["last_run_at"] is None
    assert job["created_at"] > 0
    assert store.list() == [job]


def test_add_persists_to_jobs_json(store, tmp_path):
    store.add(name="x", prompt="p", type="interval", value=5)
    data = json.loads((tmp_path / "cron" / "jobs.json").read_text())
    assert len(data["jobs"]) == 1


def test_get_and_remove(store):
    job = store.add(name="x", prompt="p", type="interval", value=5)
    assert store.get(job["id"])["name"] == "x"
    assert store.remove(job["id"]) is True
    assert store.get(job["id"]) is None
    assert store.remove(job["id"]) is False  # already gone


def test_set_enabled_pauses_and_resumes(store):
    job = store.add(name="x", prompt="p", type="interval", value=5)
    assert store.set_enabled(job["id"], False) is True
    assert store.get(job["id"])["enabled"] is False
    store.set_enabled(job["id"], True)
    assert store.get(job["id"])["enabled"] is True


def test_set_enabled_unknown_returns_false(store):
    assert store.set_enabled("nope", False) is False


def test_mark_run_updates_last_run_at(store):
    job = store.add(name="x", prompt="p", type="interval", value=5)
    store.mark_run(job["id"], when=1234.5)
    assert store.get(job["id"])["last_run_at"] == 1234.5


def test_add_validates_type(store):
    with pytest.raises(CronError):
        store.add(name="x", prompt="p", type="weekly", value=1)


def test_add_validates_interval_value(store):
    with pytest.raises(CronError):
        store.add(name="x", prompt="p", type="interval", value=0)


def test_add_validates_cron_expression(store):
    with pytest.raises(CronError):
        store.add(name="x", prompt="p", type="cron", value="not a cron")
    # a valid one is accepted
    assert store.add(name="ok", prompt="p", type="cron", value="0 9 * * 1")


def test_add_once_requires_numeric_value(store):
    with pytest.raises(CronError):
        store.add(name="x", prompt="p", type="once", value="tomorrow")


def test_add_requires_name_and_prompt(store):
    with pytest.raises(CronError):
        store.add(name="", prompt="p", type="interval", value=5)
    with pytest.raises(CronError):
        store.add(name="x", prompt="", type="interval", value=5)


def test_corrupt_jobs_file_is_treated_as_empty(store, tmp_path):
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text("{not json")
    assert store.list() == []  # degrades, doesn't crash
