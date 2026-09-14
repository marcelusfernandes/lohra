"""#138: only an accepted Service submission may execute or announce a run."""

from contextlib import closing, contextmanager
import json
import os
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.sandbox import FsRoot, WorkflowPolicy
from lohra.workflow.service import WorkflowService


SPEC = {"meta": {"name": "submission-synthetic", "version": 1}, "nodes": [
    {"id": "a", "type": "agent", "prompt": "Read synthetic.txt and summarize."},
]}
TOOL = {"name": "read_file", "description": "Synthetic reader; no filesystem operation.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                         "required": ["path"]}}


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    keep = {"HOME", "CODEX_HOME", "PATH", "TMPDIR", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    for name in tuple(os.environ):
        if name not in keep:
            monkeypatch.delenv(name)
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("LOHRA_AUDIT", "off")
    monkeypatch.chdir(tmp_path)


class InertTimer:
    def __init__(self, delay, callback):
        self.callback = callback

    def start(self):
        pass

    def cancel(self):
        pass


@contextmanager
def service(tmp_path, monkeypatch):
    calls, tools, notices, events, prepared, executed, closes = [], [], [], [], [], [], []

    class Client(ModelClient):
        def __init__(self):
            self.round = 0

        def create(self, **kwargs):
            calls.append(kwargs)
            self.round += 1
            content = ([{"type": "tool_use", "name": "read_file", "id": "synthetic",
                         "input": {"path": str(tmp_path / "synthetic.txt")}}] if self.round == 1
                       else [{"type": "text", "text": "synthetic complete"}])
            return {"content": content, "stop_reason": "tool_use" if self.round == 1 else "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

        def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
            return self.create(**kwargs)

        def close(self):
            closes.append(self)

    def dispatch(name, args):
        tools.append((name, args))
        return json.dumps({"output": "synthetic read; no file opened"})

    db = SessionDB(tmp_path / "state.db")
    svc = WorkflowService(
        base_child_factory=lambda: Agent(
            model="synthetic", provider=get_provider_profile("anthropic"), client=Client(),
            tool_dispatch=dispatch, tool_definitions=(TOOL,),
        ),
        db=db, home=tmp_path, max_runs=2, clock=lambda: 1000.0,
        lease_timer_factory=InertTimer, on_run_done=lambda *args: notices.append(args),
        on_event=lambda *args: events.append(args),
        policy=WorkflowPolicy(fs_allow=(FsRoot(tmp_path, writable=False),)),
    )
    save = svc._save_state

    def observed_save(state, **kwargs):
        if kwargs.get("mode") == "launch" and state not in prepared:
            prepared.append(state)
            run = state.engine.run

            def observed_engine(*args, **kwargs):
                executed.append(state.run_id)
                return run(*args, **kwargs)

            monkeypatch.setattr(state.engine, "run", observed_engine)
        return save(state, **kwargs)

    monkeypatch.setattr(svc, "_save_state", observed_save)
    try:
        yield SimpleNamespace(svc=svc, db=db, calls=calls, tools=tools, notices=notices,
                              events=events, prepared=prepared, executed=executed, closes=closes)
    finally:
        svc.shutdown()
        db.close()


@contextmanager
def occupied_worker(svc):
    entered, release = Event(), Event()

    def occupy():
        entered.set()
        assert release.wait(10)

    # Its first-ever task, so submit must attempt worker expansion after enqueue.
    future = svc._pool.submit(occupy)
    assert entered.wait(5)
    try:
        yield release
    finally:
        release.set()
        future.result(10)


class SyntheticAbort(BaseException):
    pass


@pytest.mark.parametrize("mode", ["closed", "partial_after", "partial_before", "abort", "success"])
def test_submit_acceptance_controls_all_service_execution(tmp_path, monkeypatch, mode):
    with service(tmp_path, monkeypatch) as r:
        svc = r.svc
        attempted = Event()
        run = svc._run

        def observed_run(*args, **kwargs):
            try:
                return run(*args, **kwargs)
            finally:
                attempted.set()

        monkeypatch.setattr(svc, "_run", observed_run)
        abandon = svc._abandon_launch

        def before_cleanup(*args, **kwargs):
            if mode == "partial_before":
                # Observe callable drainage, not client execution: after the fix
                # an unauthorized callable returns without invoking the engine.
                assert attempted.wait(5)
            return abandon(*args, **kwargs)

        monkeypatch.setattr(svc, "_abandon_launch", before_cleanup)
        error = SyntheticAbort if mode == "abort" else RuntimeError
        if mode == "success":
            reply = svc.start(SPEC, owner="synthetic-owner")
            assert reply["status"] == "started"
        elif mode == "closed":
            svc._pool.shutdown(wait=True)
            with pytest.raises(RuntimeError, match="cannot schedule"):
                svc.start(SPEC, owner="synthetic-owner")
        else:
            with occupied_worker(svc) as release:
                native_start = Thread.start
                failures = []

                def fail_start(thread, *args, **kwargs):
                    if thread.name.startswith("wf-run"):
                        failures.append(thread.name)
                        if mode == "partial_before":
                            release.set()
                        raise error("synthetic wf-run thread creation failure")
                    return native_start(thread, *args, **kwargs)

                with monkeypatch.context() as patch:
                    patch.setattr(Thread, "start", fail_start)
                    with pytest.raises(error, match="synthetic wf-run"):
                        svc.start(SPEC, owner="synthetic-owner")
                assert len(failures) == 1
        # Public API drains all queued work, no private queue surgery or sleep.
        svc._pool.shutdown(wait=True)
        assert len(r.prepared) == 1
        state = r.prepared[0]
        row = r.db.run_state_get(state.run_id)
        ledger = r.db.run_spend_get(state.run_id)
        assert svc._store.lease_expiry(state.run_id) is None
        if mode == "success":
            assert state.future is not None and r.executed == [state.run_id]
            assert len(r.calls) == 2 and len(r.tools) == 1 and len(r.notices) == 1
            assert row["status"] == "complete"
            assert ledger["tokens_in"] == ledger["tokens_out"] == 2
        else:
            assert r.executed == r.calls == r.tools == r.notices == r.events == []
            assert state.future is None and svc._runs == {}
            assert row["status"] == "failed"
            assert ledger["tokens_in"] == ledger["tokens_out"] == 0
            with closing(sqlite3.connect(tmp_path / "state.db")) as reader:
                assert reader.execute(
                    "SELECT count(*) FROM workflow_node_cache WHERE run_id = ?", (state.run_id,)
                ).fetchone()[0] == 0
        assert r.closes == []


@pytest.mark.parametrize("new_budget", [None, 10000])
def test_refused_replay_preserves_prior_metadata_and_spend(tmp_path, monkeypatch, new_budget):
    with service(tmp_path, monkeypatch) as r:
        rid = r.svc.start(SPEC, args={"input": "prior"}, owner="prior-owner")["run_id"]
        assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
        prior = r.db.run_state_get(rid)
        spend = r.db.run_spend_get(rid)
        r.svc._pool.shutdown(wait=True)
        changed = {**SPEC, "meta": {"name": "unaccepted replacement", "version": 2}}
        with pytest.raises(RuntimeError, match="cannot schedule"):
            r.svc.start(changed, args={"input": "replacement"}, owner="replacement-owner",
                        resume_run_id=rid, token_budget=new_budget)
        row = r.db.run_state_get(rid)
        for key in ("name", "owner", "status", "spec_json", "args_json", "token_budget", "tainted"):
            assert row[key] == prior[key], key
        assert r.db.run_spend_get(rid) == spend
        assert len(r.calls) == 2 and len(r.notices) == 1
        assert r.svc._store.lease_expiry(rid) is None


def test_budget_preparation_failure_releases_only_its_new_lease(tmp_path, monkeypatch):
    with service(tmp_path, monkeypatch) as r:
        rid = r.svc.start(SPEC)["run_id"]
        assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
        prior = r.db.run_state_get(rid)

        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic budget read failure")

        monkeypatch.setattr(r.svc, "_effective_budget", unavailable)
        with pytest.raises(sqlite3.OperationalError, match="synthetic budget read"):
            r.svc.start(resume_run_id=rid)
        assert r.svc._store.lease_expiry(rid) is None
        after = r.db.run_state_get(rid)
        assert after["fence"] > prior["fence"]  # a refused preparation cannot rewind ownership
        assert {k: v for k, v in after.items() if k != "fence"} == {
            k: v for k, v in prior.items() if k != "fence"
        }
        assert len(r.calls) == 2 and len(r.notices) == 1


@pytest.mark.parametrize("expire", [False, True])
def test_same_run_preparations_have_one_winner_before_first_write(tmp_path, monkeypatch, expire):
    with service(tmp_path, monkeypatch) as r:
        rid = r.svc.start(SPEC)["run_id"]
        assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
        entered, release = Event(), Event()
        now = [1000.0]
        monkeypatch.setattr(r.svc._store, "_clock", lambda: now[0])
        save = r.svc._save_state
        replies, failures = [], []

        def held_save(state, **kwargs):
            if kwargs.get("mode") == "launch" and not entered.is_set():
                entered.set()
                assert release.wait(10)
            return save(state, **kwargs)

        monkeypatch.setattr(r.svc, "_save_state", held_save)

        def first_start():
            try:
                replies.append(r.svc.start(resume_run_id=rid))
            except BaseException as exc:
                failures.append(exc)

        first = Thread(target=first_start)
        first.start()
        try:
            assert entered.wait(5)
            if expire:
                now[0] = 2001.0  # deterministic expiration while the first preparation is held
            loser = r.svc.start(resume_run_id=rid)
            assert "error" in loser
        finally:
            release.set()
            first.join(10)
        assert not first.is_alive() and failures == []
        assert len(replies) == 1 and replies[0].get("status") == "started"
        assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"


@pytest.mark.parametrize("failure", ["raise", "refuse"])
def test_failed_metadata_cleanup_keeps_original_error_and_releases_resources(
    tmp_path, monkeypatch, failure,
):
    from lohra.state.runstate import StateWrite

    with service(tmp_path, monkeypatch) as r:
        save = r.svc._store.save_snapshot
        observations = []

        def failed_cleanup(snapshot, **kwargs):
            if snapshot.launch_failure is not None:
                observations.append((r.svc._lifecycle_lock.locked(), r.svc._lock.locked()))
                if failure == "raise":
                    raise sqlite3.OperationalError("synthetic restoration unavailable")
                return StateWrite("storage_error")
            return save(snapshot, **kwargs)

        monkeypatch.setattr(r.svc._store, "save_snapshot", failed_cleanup)
        r.svc._pool.shutdown(wait=True)
        with pytest.raises(RuntimeError, match="cannot schedule"):
            r.svc.start(SPEC)
        state = r.prepared[0]
        assert observations == [(False, False)]
        assert r.svc._runs == {}
        assert r.svc._store.lease_expiry(state.run_id) is None
        assert state.core._pool._shutdown
        assert r.executed == r.calls == r.tools == r.notices == r.events == []
        assert r.closes == []
        # Storage failure is not reported as a successful durable restoration.
        assert r.db.run_state_get(state.run_id)["status"] == "running"


def test_plan_and_reentrant_consumers_see_the_real_accepted_future(tmp_path, monkeypatch):
    with service(tmp_path, monkeypatch) as r:
        observations, nested, cancellations = [], [], []

        def sink(rid, kind, payload):
            state = r.svc._get(rid)
            observations.append((kind, state.future is not None,
                                 r.svc._lifecycle_lock.locked(), r.svc._lock.locked()))
            if kind == "plan" and not nested:
                # Avoid a real deadlock on the old implementation: report the
                # held-lock observation and assert after callback dispatch.
                nested.append(None)
                if not r.svc._lifecycle_lock.locked():
                    assert "error" in r.svc.start(resume_run_id=rid)
                    nested[0] = r.svc.start(SPEC)
                    cancellations.append(r.svc.cancel(rid))

        r.svc._events.set_sink(sink)
        rid = r.svc.start(SPEC)["run_id"]
        assert r.svc.status(rid, wait=True, timeout=10)["status"] == "cancelled"
        assert nested[0]["status"] == "started"
        assert r.svc.status(nested[0]["run_id"], wait=True, timeout=10)["status"] == "complete"
        assert cancellations == [{"ok": True, "run_id": rid}]
        assert observations and all(row[1:] == (True, False, False) for row in observations)
        assert len(r.calls) == 2 and len(r.tools) == 1  # only the second run
        assert r.notices[0][1] == nested[0]["run_id"]


@pytest.mark.parametrize("boundary", ["budget", "sqlite", "cleanup"])
def test_shutdown_waits_for_preparation_without_holding_its_gate(tmp_path, monkeypatch, boundary):
    with service(tmp_path, monkeypatch) as r:
        entered, release, shutdown_waiting = Event(), Event(), Event()
        observations, replies, errors = [], [], []
        attr = {"budget": "_effective_budget", "sqlite": "_save_state",
                "cleanup": "_abandon_launch"}[boundary]
        original = getattr(r.svc, attr)

        def held(*args, **kwargs):
            observations.append((r.svc._lifecycle_lock.locked(), r.svc._lock.locked()))
            entered.set()
            assert release.wait(10)
            return original(*args, **kwargs)

        monkeypatch.setattr(r.svc, attr, held)
        wait = r.svc._launches._condition.wait

        def observed_wait(*args, **kwargs):
            shutdown_waiting.set()
            return wait(*args, **kwargs)

        monkeypatch.setattr(r.svc._launches._condition, "wait", observed_wait)
        if boundary == "cleanup":
            r.svc._pool.shutdown(wait=True)

        def start():
            try:
                replies.append(r.svc.start(SPEC))
            except BaseException as exc:
                errors.append(exc)

        starter, closer = Thread(target=start), Thread(target=r.svc.shutdown)
        starter.start()
        try:
            assert entered.wait(5)
            closer.start()
            assert shutdown_waiting.wait(5)
            assert "shutting down" in r.svc.start(SPEC)["error"]
            assert r.svc._lifecycle_lock.acquire(timeout=1)
            r.svc._lifecycle_lock.release()
            assert closer.is_alive()
        finally:
            release.set()
            starter.join(10)
            if closer.ident is not None:
                closer.join(10)
        assert not starter.is_alive() and not closer.is_alive()
        assert replies == [] and len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert observations and all(item == (False, False) for item in observations)
        assert r.svc._runs == {} and r.svc._store.lease_expiry(r.prepared[0].run_id) is None
        assert r.executed == r.calls == r.tools == r.notices == r.events == []


def test_shutdown_from_synchronous_preparation_is_an_explicit_refusal(tmp_path, monkeypatch):
    with service(tmp_path, monkeypatch) as r:
        effective = r.svc._effective_budget
        refusals = []

        def reenter(*args, **kwargs):
            try:
                r.svc.shutdown()
            except RuntimeError as exc:
                refusals.append(str(exc))
            return effective(*args, **kwargs)

        monkeypatch.setattr(r.svc, "_effective_budget", reenter)
        rid = r.svc.start(SPEC)["run_id"]
        assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
        assert refusals == ["cannot shut down from inside workflow launch preparation"]


@pytest.mark.parametrize("prior_status", [None, "complete", "paused", "running"])
def test_durable_refusal_keeps_prior_markers_without_inventing_audit(tmp_path, monkeypatch, prior_status):
    from dataclasses import replace

    monkeypatch.setenv("LOHRA_AUDIT", "on")
    with service(tmp_path, monkeypatch) as r:
        rid = None
        old_events = []
        prior = None
        if prior_status is not None:
            rid = r.svc.start(SPEC, owner="prior-owner")["run_id"]
            assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
            assert r.svc._audit.flush()
            prior = replace(r.svc._store.load(rid), status=prior_status,
                            audit_segment_id="prior-unclosed" if prior_status != "complete" else None)
            assert r.svc._store.save_snapshot(
                prior, fence=prior.fence, mode="launch", expected_revision=prior.revision,
            ).accepted
            r.svc._runs.clear()  # a fresh process cannot use the prior local projection
            old_events = r.db.audit_events(rid)
        r.svc._pool.shutdown(wait=True)
        with pytest.raises(RuntimeError, match="cannot schedule"):
            r.svc.start(SPEC, resume_run_id=rid, owner="CANARY-unaccepted-owner")
        rid = r.prepared[-1].run_id
        assert r.svc._audit.flush()
        assert r.db.audit_events(rid) == old_events
        with closing(SessionDB(tmp_path / "state.db")) as reader:
            from lohra.workflow.runstate_store import RunStateStore
            store = RunStateStore(reader, clock=lambda: 1000.0, timer_factory=InertTimer)
            try:
                restored = store.load(rid)
                assert restored.launch_failure == "submission_refused"
                expected = prior_status if prior_status in {"complete", "paused"} else "failed"
                assert restored.status == expected
                assert restored.audit_segment_id == (prior.audit_segment_id if prior else None)
                if prior:
                    assert restored.owner == "prior-owner" and restored.args == prior.args
                assert r.svc.status(rid)["launch_failure"] == "submission_refused"
            finally:
                store.shutdown()
        assert r.svc._store.lease_expiry(rid) is None


@pytest.mark.parametrize("drain_before_retry", [False, True])
def test_old_refused_callable_cannot_borrow_a_replay_acceptance(tmp_path, monkeypatch, drain_before_retry):
    with service(tmp_path, monkeypatch) as r:
        rid = r.svc.start(SPEC)["run_id"]
        assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
        # A new pool gives the partial-submit setup one known occupied worker;
        # all executions still use real public ThreadPoolExecutor methods.
        from concurrent.futures import ThreadPoolExecutor
        r.svc._pool.shutdown(wait=True)
        r.svc._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wf-run")
        attempted = Event()
        run = r.svc._run

        def observed(*args, **kwargs):
            try:
                return run(*args, **kwargs)
            finally:
                attempted.set()

        monkeypatch.setattr(r.svc, "_run", observed)
        with occupied_worker(r.svc) as release:
            native_start = Thread.start

            def fail_start(thread, *args, **kwargs):
                if thread.name.startswith("wf-run"):
                    raise RuntimeError("synthetic partial submit")
                return native_start(thread, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(Thread, "start", fail_start)
                with pytest.raises(RuntimeError, match="synthetic partial"):
                    r.svc.start(resume_run_id=rid)
            refused = r.prepared[-1]
            if drain_before_retry:
                release.set()
                assert attempted.wait(5)
            accepted = r.svc.start(resume_run_id=rid)
            assert accepted["status"] == "started"
            assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
        assert refused.future is None
        assert r.svc._get(rid) is not refused and r.svc._get(rid).future is not None
        assert r.executed == [rid, rid] and len(r.calls) == 2  # one paid run, one cached replay
        assert len(r.notices) == 2
        assert "launch_failure" not in r.svc.status(rid)
        assert r.db.run_spend_get(rid)["tokens_in"] == 2


@pytest.mark.parametrize("winner", ["cancel", "takeover"])
def test_refusal_cleanup_cannot_restore_over_a_winning_transition(tmp_path, monkeypatch, winner):
    with service(tmp_path, monkeypatch) as old:
        rid = old.svc.start(SPEC, owner="old-owner")["run_id"]
        assert old.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
        entered, release = Event(), Event()
        errors = []
        abandon = old.svc._abandon_launch

        def held(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return abandon(*args, **kwargs)

        monkeypatch.setattr(old.svc, "_abandon_launch", held)
        old.svc._pool.shutdown(wait=True)

        def start():
            try:
                old.svc.start(resume_run_id=rid, token_budget=10000)
            except BaseException as exc:
                errors.append(exc)

        starter = Thread(target=start)
        starter.start()
        try:
            assert entered.wait(5)
            if winner == "cancel":
                assert old.svc.cancel(rid)["ok"]
                row = old.db.run_state_get(rid)
            else:
                with service(tmp_path, monkeypatch) as new:
                    monkeypatch.setattr(new.svc._store, "_clock", lambda: 2001.0)
                    reply = new.svc.start(resume_run_id=rid, owner="new-owner", token_budget=20000)
                    assert reply["status"] == "started"
                    assert new.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
                    row = new.db.run_state_get(rid)
                    spend = new.db.run_spend_get(rid)
                    release.set()
                    starter.join(10)
                    assert new.db.run_state_get(rid) == row
                    assert new.db.run_spend_get(rid) == spend
                    assert len(new.notices) == 1
        finally:
            release.set()
            starter.join(10)
        assert not starter.is_alive() and len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert old.db.run_state_get(rid) == row
        assert row["status"] == ("cancelled" if winner == "cancel" else "complete")
        assert len(old.calls) == 2 and len(old.notices) == 1
        assert old.svc._runs == {}


def test_post_acceptance_response_error_does_not_abandon_the_tracked_run(tmp_path, monkeypatch):
    from lohra.workflow.operator_budget import AppliedBudget

    with service(tmp_path, monkeypatch) as r:
        entered, release = Event(), Event()
        run = r.svc._run

        def held(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return run(*args, **kwargs)

        def interrupted(self):
            raise SyntheticAbort("synthetic caller interruption after acceptance")

        monkeypatch.setattr(r.svc, "_run", held)
        monkeypatch.setattr(AppliedBudget, "as_dict", interrupted)
        try:
            with pytest.raises(SyntheticAbort, match="after acceptance"):
                r.svc.start(SPEC)
            assert entered.wait(5)
            state = r.prepared[-1]
            assert r.svc._get(state.run_id) is state and state.future is not None
        finally:
            release.set()
        assert r.svc.status(state.run_id, wait=True, timeout=10)["status"] == "complete"
        assert len(r.calls) == 2 and len(r.notices) == 1


def test_submit_acceptance_linearizes_before_a_concurrent_shutdown(tmp_path, monkeypatch):
    with service(tmp_path, monkeypatch) as r:
        submitted, release, closing_entered = Event(), Event(), Event()
        replies, errors, seen = [], [], []
        submit, close = r.svc._pool.submit, r.svc._launches.close

        def held_submit(*args, **kwargs):
            future = submit(*args, **kwargs)
            submitted.set()
            assert release.wait(10)
            return future

        def observed_close():
            closing_entered.set()
            return close()

        def sink(rid, kind, payload):
            seen.append((kind, r.svc._get(rid).future is not None))

        def start():
            try:
                replies.append(r.svc.start(SPEC))
            except BaseException as exc:
                errors.append(exc)

        monkeypatch.setattr(r.svc._pool, "submit", held_submit)
        monkeypatch.setattr(r.svc._launches, "close", observed_close)
        r.svc._events.set_sink(sink)
        starter, closer = Thread(target=start), Thread(target=r.svc.shutdown)
        starter.start()
        try:
            assert submitted.wait(5)
            closer.start()
            assert closing_entered.wait(5)
        finally:
            release.set()
            starter.join(10)
            if closer.ident is not None:
                closer.join(10)
        assert not starter.is_alive() and not closer.is_alive() and errors == []
        assert len(replies) == 1 and replies[0]["status"] == "started"
        state = r.prepared[0]
        assert state.future is not None and state.future.done()
        assert seen and all(tracked for kind, tracked in seen)
        assert r.svc._store.lease_expiry(state.run_id) is None
        assert "shutting down" in r.svc.start(SPEC)["error"]


def test_core_cleanup_error_does_not_hide_the_submit_error_or_skip_release(tmp_path, monkeypatch):
    with service(tmp_path, monkeypatch) as r:
        save, observations = r.svc._save_state, []

        def replace_cleanup(state, **kwargs):
            if kwargs.get("mode") == "launch":
                shutdown = state.core.shutdown

                def fail_cleanup():
                    observations.append((r.svc._lifecycle_lock.locked(), r.svc._lock.locked()))
                    shutdown()
                    raise SyntheticAbort("synthetic core cleanup failure")

                monkeypatch.setattr(state.core, "shutdown", fail_cleanup)
            return save(state, **kwargs)

        monkeypatch.setattr(r.svc, "_save_state", replace_cleanup)
        r.svc._pool.shutdown(wait=True)
        with pytest.raises(RuntimeError, match="cannot schedule"):
            r.svc.start(SPEC)
        assert observations == [(False, False)]
        assert r.svc._runs == {} and r.svc._store.lease_expiry(r.prepared[0].run_id) is None
        assert r.svc.status(r.prepared[0].run_id)["launch_failure"] == "submission_refused"
