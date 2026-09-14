"""#136: real executor refusal cannot authorize an untracked child."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    keep = {"HOME", "CODEX_HOME", "PATH", "TMPDIR", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    for name in tuple(os.environ):
        if name not in keep:
            monkeypatch.delenv(name)
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("LOHRA_AUDIT", "off")
    monkeypatch.chdir(tmp_path)


@contextmanager
def rig(tmp_path, *, cap=10):
    calls, tools, hooks, events, refunds = [], [], [], [], []

    class Client(ModelClient):
        def __init__(self):
            self.round = 0

        def create(self, **kwargs):
            calls.append(kwargs)
            self.round += 1
            content = ([{"type": "tool_use", "name": "read_file", "id": "synthetic",
                         "input": {"path": "synthetic.txt"}}] if self.round == 1 else
                       [{"type": "text", "text": "complete"}])
            return {"content": content, "stop_reason": "tool_use" if self.round == 1 else "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

        def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
            return self.create(**kwargs)

        def close(self):
            pass

    class CountedBudget(Budget):
        def refund(self, amount):
            refunds.append(amount)
            return super().refund(amount)

    def dispatch(name, args):
        tools.append((name, args))
        return json.dumps({"output": "synthetic read; no file opened"})

    db = SessionDB(tmp_path / "state.db")
    core = OrchestrationCore(
        db,
        lambda: Agent(model="synthetic", provider=get_provider_profile("anthropic"),
                      client=Client(), tool_dispatch=dispatch, tool_definitions=({
                          "name": "read_file", "description": "Synthetic read",
                          "input_schema": {"type": "object", "properties": {
                              "path": {"type": "string"}}, "required": ["path"]},
                      },)),
        max_concurrent=2, max_children=cap,
        event_sink=lambda *args: events.append(args),
    )
    budget = CountedBudget(lifetime=1)
    engine = WorkflowEngine(core, budget=budget)
    try:
        yield SimpleNamespace(db=db, core=core, engine=engine, budget=budget,
                              calls=calls, tools=tools, hooks=hooks, events=events, refunds=refunds)
    finally:
        core.shutdown()
        db.close()


def spawn(r, callback, prompt="synthetic prompt"):
    if callback:
        return r.engine.spawn_leaf_with_done(prompt, r.hooks.append)
    return r.engine.spawn_leaf(prompt)


@contextmanager
def occupied_worker(core):
    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(10)

    # This is its first-ever task: no prior idle semaphore can avoid worker 2.
    future = core._pool.submit(block)
    assert entered.wait(5)
    try:
        yield release
    finally:
        release.set()
        future.result(10)


@contextmanager
def failed_thread_start(monkeypatch, error=RuntimeError):
    original = Thread.start
    failures = []

    def fail(thread, *args, **kwargs):
        if thread.name.startswith("orch"):
            failures.append(thread.name)
            raise error("synthetic thread creation failure")
        return original(thread, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Thread, "start", fail)
        yield failures


@pytest.mark.parametrize("callback", [False, True])
@pytest.mark.parametrize("mode", ["partial", "closed", "success"])
def test_engine_spawn_acceptance_matches_execution(tmp_path, monkeypatch, callback, mode):
    with rig(tmp_path) as r:
        created = []
        create = r.db.create_session

        def observed_create(sid, **kwargs):
            create(sid, **kwargs)
            created.append(sid)

        monkeypatch.setattr(r.db, "create_session", observed_create)
        if mode == "partial":
            with occupied_worker(r.core):
                with failed_thread_start(monkeypatch) as failures:
                    with pytest.raises(RuntimeError, match="synthetic thread"):
                        spawn(r, callback)
                assert len(failures) == 1
        elif mode == "closed":
            r.core._pool.shutdown(wait=True)
            with pytest.raises(RuntimeError, match="cannot schedule"):
                spawn(r, callback)
        else:
            sid = spawn(r, callback)
            assert r.core._children[sid].future is not None
            assert r.engine.spawned == (sid,) and r.budget.lifetime_remaining == 0
            assert r.core.causal_snapshot(sid)["causal_context"] is not None
        # No queue removal or negative timed wait: drain the real executor.
        r.core._pool.shutdown(wait=True)
        assert len(created) == 1
        row = r.db.get_session(created[0])
        if mode == "success":
            assert len(r.calls) == 2 and len(r.tools) == 1
            assert r.hooks == ([sid] if callback else [])
            assert r.core.collect(sid, wait=True)["tokens_in"] == 2
            assert row["end_reason"] is None and r.refunds == []
        else:
            assert r.calls == r.tools == r.hooks == r.events == []
            assert r.core._children == {} and r.core._active == 0
            assert r.engine.spawned == () and r.budget.lifetime_remaining == 1
            assert r.refunds == [1]
            assert row["end_reason"] == "spawn_rejected" and row["ended_at"] is not None
            assert row["message_count"] == 0


@pytest.mark.parametrize("cap,accepted", [(1, False), (2, False), (1, True)])
def test_only_an_accepted_spawn_can_evict_a_previous_child(tmp_path, cap, accepted):
    with rig(tmp_path, cap=cap) as r:
        r.db.create_session("parent", title="must survive")
        first = r.core.spawn("first", parent_id="parent", causal_context={"turn": 1}, on_done=r.hooks.append)
        r.core.collect(first, wait=True, timeout=10)
        before = deepcopy(r.core.collect(first)), r.core.causal_snapshot(first), r.db.get_session(first)
        prior = r.core._children[first]
        if accepted:
            second = r.core.spawn("second", parent_id="parent", on_done=r.hooks.append)
            r.core.collect(second, wait=True, timeout=10)
            assert "error" in r.core.collect(first)
            assert r.hooks == [first, second] and len(r.calls) == 4
        else:
            r.core._pool.shutdown(wait=True)
            with pytest.raises(RuntimeError):
                r.core.spawn("refused", parent_id="parent", on_done=r.hooks.append)
            assert r.core._children == {first: prior}
            assert (r.core.collect(first), r.core.causal_snapshot(first), r.db.get_session(first)) == before
            assert r.hooks == [first] and len(r.calls) == 2
        assert r.db.get_session("parent")["title"] == "must survive"


@pytest.mark.parametrize("callback", [False, True])
@pytest.mark.parametrize("retry_before_drain", [False, True])
def test_valid_retry_cannot_authorize_a_refused_callable(tmp_path, monkeypatch, callback, retry_before_drain):
    with rig(tmp_path) as r:
        rejected_done = Event()
        run = r.core._run

        def observed_run(sid, prompt, *args):
            try:
                return run(sid, prompt, *args)
            finally:
                if prompt == "refused":
                    rejected_done.set()

        monkeypatch.setattr(r.core, "_run", observed_run)
        with occupied_worker(r.core) as release:
            with failed_thread_start(monkeypatch):
                with pytest.raises(RuntimeError):
                    spawn(r, callback, "refused")
            assert r.refunds == [1] and r.budget.lifetime_remaining == 1
            if not retry_before_drain:
                release.set()
                assert rejected_done.wait(10)
                assert r.calls == r.tools == r.hooks == []
            sid = spawn(r, callback, "accepted retry")
            assert r.core.collect(sid, wait=True, timeout=10)["status"] == "complete"
        r.core._pool.shutdown(wait=True)
        assert rejected_done.is_set()
        assert len(r.calls) == 2 and len(r.tools) == 1
        assert all("refused" not in json.dumps(call) for call in r.calls)
        assert r.engine.spawned == (sid,) and r.budget.lifetime_remaining == 0
        assert r.refunds == [1] and r.hooks == ([sid] if callback else [])


@pytest.mark.parametrize("callback", [False, True])
@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_base_exception_after_enqueue_still_revokes_the_attempt(tmp_path, monkeypatch, callback, error):
    with rig(tmp_path) as r:
        with occupied_worker(r.core):
            with failed_thread_start(monkeypatch, error):
                with pytest.raises(error, match="synthetic thread"):
                    spawn(r, callback)
        r.core._pool.shutdown(wait=True)
        assert r.core._children == {}
        assert r.calls == r.tools == r.hooks == r.events == []
        assert r.engine.spawned == () and r.refunds == [1]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_rejected_row_cleanup_is_outside_lock_and_cannot_hide_submit_failure(
    tmp_path, monkeypatch, caplog, cleanup_fails
):
    with rig(tmp_path) as r:
        cleanup = []
        end = r.db.end_session

        def observed_end(sid, reason):
            cleanup.append((sid, reason, r.core._lock.locked()))
            if cleanup_fails:
                raise sqlite3.OperationalError("synthetic storage failure")
            return end(sid, reason)

        monkeypatch.setattr(r.db, "end_session", observed_end)
        r.core._pool.shutdown(wait=True)
        with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
            spawn(r, True)
        assert len(cleanup) == 1 and cleanup[0][1:] == ("spawn_rejected", False)
        assert r.core._children == {} and r.refunds == [1]
        assert r.calls == r.tools == r.hooks == []
        row = r.db.get_session(cleanup[0][0])
        assert row["end_reason"] == (None if cleanup_fails else "spawn_rejected")
        if cleanup_fails:
            assert "could not mark rejected preparation" in caplog.text


@pytest.mark.parametrize("seam", ["factory", "configure", "database"])
def test_preparation_failure_cannot_publish_or_change_an_existing_session(tmp_path, monkeypatch, seam):
    with rig(tmp_path) as r:
        r.db.create_session("existing", title="keep")
        before = r.db.get_session("existing")

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic preparation failure")

        configure = fail if seam == "configure" else None
        if seam == "factory":
            monkeypatch.setattr(r.core, "_child_factory", fail)
        elif seam == "database":
            monkeypatch.setattr(r.db, "create_session", fail)
        with pytest.raises(RuntimeError, match="synthetic preparation failure"):
            r.engine.spawn_leaf("refused", configure=configure)
        r.core._pool.shutdown(wait=True)
        assert r.core._children == {} and r.engine.spawned == ()
        assert r.calls == r.tools == r.hooks == r.events == []
        assert r.refunds == [1] and r.db.get_session("existing") == before


def test_fast_completion_observes_publication_and_can_chain_another_child(tmp_path):
    with rig(tmp_path) as r:
        observations, children = [], []

        def done(sid):
            observations.append((r.core._lock.locked(), r.core._children[sid].future is not None,
                                 r.core.causal_snapshot(sid)["causal_context"]))
            children.append(r.core.spawn("chained"))  # no wait inside the callback

        first = r.core.spawn("first", on_done=done, causal_context={"turn": "first"})
        r.core.collect(first, wait=True, timeout=10)
        assert observations == [(False, True, {"turn": "first"})]
        assert len(children) == 1
        assert r.core.collect(children[0], wait=True, timeout=10)["status"] == "complete"
        assert len(r.calls) == 4 and len(r.tools) == 2


@pytest.mark.parametrize("operation", ["cancel", "shutdown"])
def test_accepted_queued_child_keeps_normal_cancel_and_shutdown_hooks(tmp_path, operation):
    with rig(tmp_path) as r:
        r.core._pool.shutdown(wait=True)
        r.core._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orch")
        with occupied_worker(r.core):
            sid = spawn(r, True)
            if operation == "cancel":
                assert r.core.cancel(sid) == {"ok": True, "cancelled": "queued"}
            else:
                r.core.shutdown(wait=False)
            assert r.core.collect(sid)["status"] == "cancelled"
            assert r.hooks == [sid]
        r.core._pool.shutdown(wait=True)
        assert r.hooks == [sid] and r.calls == r.tools == []
