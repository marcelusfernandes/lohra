"""Native publication ordering across connections/processes, without providers."""

import errno
import multiprocessing
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

from lohra.state import SessionDB
from lohra.state import publication
from tests.test_workflow_cancel_transition import isolated_environment  # noqa: F401
from tests.test_workflow_operability import _service
from tests.test_workflow_publication_generation import SPEC
from tests.test_workflow_quota import TimerFactory


def _held_process(path, run_id, pipe):
    db = SessionDB(path)
    try:
        with db.publication_guard(run_id) as access:
            pipe.send(access)
            pipe.recv()
    finally:
        db.close()
        pipe.close()


def _replay_process(path, run_id, pipe):
    db = SessionDB(path)
    notices = []
    svc = _service(db, Path(path).parent, lambda _: "two", timers=TimerFactory(),
                   on_run_done=lambda *args: notices.append(args))
    spec = {**SPEC, "meta": {**SPEC["meta"], "version": 2}, "nodes": [
        {"id": "a", "type": "agent", "prompt": "second synthetic leaf"},
    ]}
    try:
        reply = svc.start(spec, resume_run_id=run_id, owner="synthetic-owner")
        if "error" not in reply:
            svc._runs[run_id].future.result(10)
        pipe.send((reply, notices))
    finally:
        svc.shutdown()
        db.close()
        pipe.close()


def _receive(pipe):
    assert pipe.poll(10), "synthetic process did not respond"
    return pipe.recv()


def _child(ctx, target, *args):
    parent, child = ctx.Pipe()
    process = ctx.Process(target=target, args=(*args, child))
    process.start()
    child.close()
    return process, parent


def test_service_replay_in_another_process_is_refused_only_during_publication(tmp_path):
    path = str(tmp_path / "state.db")
    db = SessionDB(path)
    ctx = multiprocessing.get_context("spawn")
    svc = _service(db, tmp_path, lambda _: "one", timers=TimerFactory())
    processes, pipes = [], []
    try:
        rid = svc.start(SPEC, owner="synthetic-owner")["run_id"]
        svc._runs[rid].future.result(10)
        with db.publication_guard(rid) as access:
            assert access == "acquired"
            process, pipe = _child(ctx, _replay_process, path, rid)
            processes.append(process)
            pipes.append(pipe)
            reply, notices = _receive(pipe)
            assert "publication or transition in progress" in reply["error"]
            assert notices == [] and db.run_fence_of(rid) == 1
            process.join(10)
            assert process.exitcode == 0
        process, pipe = _child(ctx, _replay_process, path, rid)
        processes.append(process)
        pipes.append(pipe)
        reply, notices = _receive(pipe)
        assert reply.get("status") == "started" and len(notices) == 1
        assert db.run_fence_of(rid) == 2
        assert svc.get_template("publication-generation")["meta"]["version"] == 2
        process.join(10)
        assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
        for pipe in pipes:
            pipe.close()
        svc.shutdown()
        db.close()


def test_process_death_releases_guard_and_ttl_recovery_still_works(tmp_path):
    path = str(tmp_path / "state.db")
    db = SessionDB(path)
    ctx = multiprocessing.get_context("spawn")
    process, pipe = None, None
    try:
        assert db.acquire_run_lease("r", "lost", now=1, ttl_seconds=1) == 1
        process, pipe = _child(ctx, _held_process, path, "r")
        assert _receive(pipe) == "acquired"
        assert db.acquire_run_state("r", "next", now=100, ttl_seconds=1).kind == "publication_busy"
        assert db.acquire_run_state("other", "next", now=100, ttl_seconds=1).accepted
        process.terminate()  # real OS release; no cleanup/finally runs in this process
        process.join(10)
        assert not process.is_alive()
        result = db.acquire_run_state("r", "next", now=100, ttl_seconds=1)
        assert result.accepted and result.fence == 2
    finally:
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(10)
        if pipe is not None:
            pipe.close()
        db.close()


def test_database_aliases_and_memory_identity_are_scoped(tmp_path):
    db = SessionDB("state.db")  # fixture cwd is tmp_path
    (tmp_path / "alias.db").symlink_to(tmp_path / "state.db")
    alias = SessionDB(tmp_path / "alias.db")
    separate_file = SessionDB(tmp_path / "separate.db")
    memory = SessionDB(":memory:")
    separate_memory = SessionDB(":memory:")
    try:
        with db.publication_guard("r"):
            assert alias.acquire_run_state("r", "other", now=1, ttl_seconds=1).kind == "publication_busy"
            assert alias.acquire_run_state("other", "other", now=1, ttl_seconds=1).accepted
            assert separate_file.acquire_run_state("r", "other", now=1, ttl_seconds=1).accepted
        with memory.publication_guard("r"):
            assert memory.acquire_run_state("r", "other", now=1, ttl_seconds=1).kind == "publication_busy"
            assert separate_memory.acquire_run_state("r", "other", now=1, ttl_seconds=1).accepted
        directory = tmp_path / ".lohra-publication-locks"
        assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
        assert all(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0 for path in directory.iterdir())
    finally:
        for database in (db, alias, separate_file, memory, separate_memory):
            database.close()


def test_existing_case_alias_shares_the_database_guard(tmp_path):
    path = tmp_path / "Case.db"
    db = SessionDB(path)
    alias_path = tmp_path / "case.db"
    alias = None
    try:
        if not alias_path.exists() or not path.samefile(alias_path):
            pytest.skip("this volume distinguishes case aliases")
        alias = SessionDB(alias_path)
        with db.publication_guard("r"):
            assert alias.acquire_run_state("r", "other", now=1, ttl_seconds=1).kind == "publication_busy"
    finally:
        if alias is not None:
            alias.close()
        db.close()


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_guard_releases_on_every_exception(tmp_path, error):
    db = SessionDB(tmp_path / "state.db")
    try:
        with pytest.raises(error), db.publication_guard("r") as access:
            assert access == "acquired"
            raise error("synthetic publication failure")
        assert db.acquire_run_state("r", "next", now=1, ttl_seconds=1).accepted
    finally:
        db.close()


def test_guard_storage_failure_is_not_a_lease_busy_result(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")

    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "synthetic denied lock-file open")

    try:
        monkeypatch.setattr(publication.os, "open", denied)
        result = db.acquire_run_state("r", "next", now=1, ttl_seconds=1)
        assert result.kind == "storage_error" and db.run_fence_of("r") is None
    finally:
        db.close()


def test_windows_lock_call_is_simulated_separately(monkeypatch, tmp_path):
    calls = []
    fake = SimpleNamespace(LK_NBLCK=123, locking=lambda *args: calls.append(args))
    with (tmp_path / "lock").open("w+b") as handle:
        handle.write(b"\0")
        handle.flush()
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        with monkeypatch.context() as patch:
            patch.setattr(publication.os, "name", "nt")
            publication._try_lock(handle.fileno())
        assert calls == [(handle.fileno(), 123, 1)]
        assert os.lseek(handle.fileno(), 0, os.SEEK_CUR) == 0
