"""#126: deferred template/notice effects retain their acquisition identity."""

from copy import deepcopy
import threading

import pytest

from lohra.state import SessionDB
from lohra.workflow import library
from tests.test_workflow_cancel_transition import isolated_environment  # noqa: F401
from tests.test_workflow_operability import _service
from tests.test_workflow_quota import TimerFactory


SPEC = {
    "meta": {"name": "publication-generation", "version": 1},
    "nodes": [{"id": "a", "type": "agent", "prompt": "first synthetic leaf"}],
}


@pytest.mark.parametrize("interleave", ["current", "metadata", "successor"])
def test_deferred_publication_cannot_regress_a_successor(tmp_path, monkeypatch, interleave):
    ready, release = threading.Event(), threading.Event()
    notices, observed = [], []
    db = SessionDB(tmp_path / "state.db")
    other_db = SessionDB(tmp_path / "state.db")
    old = _service(db, tmp_path, lambda _: "one", timers=TimerFactory(),
                   on_run_done=lambda *args: notices.append(("old", args)))
    new = _service(other_db, tmp_path, lambda _: "two", timers=TimerFactory(),
                   on_run_done=lambda *args: notices.append(("new", args)))
    publish = old._publish_outcome

    def held(state, record):
        ready.set()
        assert release.wait(10)
        observed.append((state.fence, db.run_fence_of(state.run_id)))
        return publish(state, record)

    monkeypatch.setattr(old, "_publish_outcome", held)
    try:
        rid = old.start(SPEC, owner="synthetic-owner")["run_id"]
        assert ready.wait(10)
        if interleave == "metadata":
            state = old._runs[rid]
            before = state.revision
            assert old._persist_state(state)
            assert state.revision > before and state.fence == 1
        if interleave == "successor":
            assert old._store.lease_expiry(rid) is None
            changed = deepcopy(SPEC)
            changed["meta"]["version"] = 2
            changed["nodes"][0]["prompt"] = "second synthetic leaf"
            assert new.start(changed, resume_run_id=rid, owner="synthetic-owner").get("status") == "started"
            new._runs[rid].future.result(10)
            assert new.get_template("publication-generation")["meta"]["version"] == 2
        release.set()
        old._runs[rid].future.result(10)
        expected = 2 if interleave == "successor" else 1
        assert observed == [(1, expected)]
        assert new.get_template("publication-generation")["meta"]["version"] == expected
        assert new._store.load(rid).spec["meta"]["version"] == expected
        assert [who for who, _ in notices] == (["new"] if interleave == "successor" else ["old"])
        assert notices[0][1][2] == "complete"
    finally:
        release.set()
        old.shutdown()
        new.shutdown()
        db.close()
        other_db.close()


@pytest.mark.parametrize("effect", ["library", "notice"])
def test_effect_after_ownership_check_blocks_succession_without_state_locks(
    tmp_path, monkeypatch, effect
):
    ready, release, launched = threading.Event(), threading.Event(), threading.Event()
    observations, notices = [], []
    db = SessionDB(tmp_path / "state.db")
    other_db = SessionDB(tmp_path / "state.db")
    old = _service(db, tmp_path, lambda _: "one", timers=TimerFactory(),
                   on_run_done=lambda *args: notices.append("old"))
    new = _service(other_db, tmp_path, lambda _: "two", timers=TimerFactory(),
                   on_run_done=lambda *args: notices.append("new"))

    def hold(rid):
        assert launched.wait(10)
        state = old._runs[rid]
        # Assertions stay outside the fail-isolated callbacks.
        observations.append((
            old._lock.locked(), old._lifecycle_lock.locked(),
            state.state_lock.locked(), old._store._lock.locked(),
            db._lock._is_owned(), db._connection.in_transaction,
            db.acquire_run_state(rid, "reentrant", now=1e20, ttl_seconds=1).kind,
            old.start(SPEC, resume_run_id=rid).get("error"),
        ))
        ready.set()
        assert release.wait(10)

    if effect == "library":
        save = library._save_template

        def held_save(home, name, spec, **kwargs):
            if spec["meta"].get("version") == 1:
                hold(kwargs["run_id"])
            return save(home, name, spec, **kwargs)

        monkeypatch.setattr(library, "_save_template", held_save)
    else:
        notify = old._notify_done

        def held_notify(state):
            hold(state.run_id)
            return notify(state)

        monkeypatch.setattr(old, "_notify_done", held_notify)
    try:
        rid = old.start(SPEC, owner="synthetic-owner")["run_id"]
        launched.set()
        assert ready.wait(10)
        assert observations[0][:-1] == (False, False, False, False, False, False, "publication_busy")
        assert "publication or transition in progress" in observations[0][-1]
        assert old._store.lease_expiry(rid) is None  # stronger than TTL expiration
        changed = deepcopy(SPEC)
        changed["meta"]["version"] = 2
        changed["nodes"][0]["prompt"] = "second synthetic leaf"
        refused = new.start(changed, resume_run_id=rid, owner="synthetic-owner")
        assert "publication or transition in progress" in refused["error"]
        assert other_db.run_fence_of(rid) == 1
        assert other_db.acquire_run_state("unrelated", "other", now=1e20, ttl_seconds=1).accepted
        release.set()
        old._runs[rid].future.result(10)
        assert new.start(changed, resume_run_id=rid, owner="synthetic-owner").get("status") == "started"
        new._runs[rid].future.result(10)
        assert notices == ["old", "new"]
        assert new.get_template("publication-generation")["meta"]["version"] == 2
    finally:
        launched.set()
        release.set()
        old.shutdown()
        new.shutdown()
        db.close()
        other_db.close()


def test_broken_effects_release_the_guard_and_preserve_notice_isolation(tmp_path, monkeypatch):
    notices = []
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, lambda _: "one", timers=TimerFactory(),
                   on_run_done=lambda *args: notices.append(args))

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic feedback failure")

    monkeypatch.setattr(library, "record_outcome", fail)
    try:
        rid = svc.start(SPEC, owner="synthetic-owner")["run_id"]
        svc._runs[rid].future.result(10)
        assert len(notices) == 1
        with db.publication_guard(rid) as access:
            assert access == "acquired"
        monkeypatch.setattr(svc, "_on_run_done", fail)
        assert svc.start(SPEC, resume_run_id=rid).get("status") == "started"
        svc._runs[rid].future.result(10)
        with db.publication_guard(rid) as access:
            assert access == "acquired"
    finally:
        svc.shutdown()
        db.close()
