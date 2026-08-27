"""Tests for the cron scheduler tick (pure, clock + runner injected)."""

import pytest

from lohra.cron.scheduler import tick
from lohra.cron.store import CronStore


@pytest.fixture
def store(tmp_path):
    return CronStore(tmp_path)


def test_tick_runs_due_jobs_and_marks_them(store):
    job = store.add(name="x", prompt="p", type="interval", value=5)  # due (never run)
    ran = []
    result = tick(store, ran.append, now=1000.0)
    assert ran == [job]
    assert [r[0] for r in result] == [job["id"]]
    # marked run so it won't immediately fire again
    assert store.get(job["id"])["last_run_at"] == 1000.0


def test_tick_skips_not_due_jobs(store):
    store.add(name="x", prompt="p", type="interval", value=60, )  # just-added, but...
    # mark it as run now so the 60-min interval is not yet elapsed
    job = store.list()[0]
    store.mark_run(job["id"], when=1000.0)
    ran = []
    tick(store, ran.append, now=1000.0 + 30 * 60)  # only 30 min later
    assert ran == []


def test_tick_skips_disabled_jobs(store):
    job = store.add(name="x", prompt="p", type="interval", value=5)
    store.set_enabled(job["id"], False)
    ran = []
    tick(store, ran.append, now=10_000.0)
    assert ran == []


def test_tick_isolates_a_failing_job(store):
    a = store.add(name="a", prompt="p", type="interval", value=5)
    b = store.add(name="b", prompt="p", type="interval", value=5)

    def runner(job):
        if job["id"] == a["id"]:
            raise RuntimeError("boom")

    result = tick(store, runner, now=1000.0)
    by_id = dict(result)
    assert by_id[a["id"]] is False  # failed
    assert by_id[b["id"]] is True
    # both marked run regardless, so a broken job doesn't retry-storm
    assert store.get(a["id"])["last_run_at"] == 1000.0
    assert store.get(b["id"])["last_run_at"] == 1000.0


def test_tick_skips_a_malformed_job_without_aborting(store):
    # a hand-corrupted job (unknown type) must not blow up the whole tick
    good = store.add(name="ok", prompt="p", type="interval", value=5)
    bad = store.add(name="bad", prompt="p", type="interval", value=5)
    # corrupt the bad one's type directly in the file
    store._mutate(bad["id"], lambda j: {**j, "type": "weekly"})
    ran = []
    result = tick(store, ran.append, now=1000.0)
    assert good in ran  # the good job still ran
    assert dict(result)[bad["id"]] is False  # the bad one was skipped (logged)


def test_once_job_runs_exactly_once_across_ticks(store):
    store.add(name="x", prompt="p", type="once", value=500.0)
    ran = []
    tick(store, ran.append, now=600.0)  # due
    tick(store, ran.append, now=700.0)  # already ran -> not due
    assert len(ran) == 1
