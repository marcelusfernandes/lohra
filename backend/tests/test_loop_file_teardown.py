"""Per-file queues must not join a running tool or start their tail on teardown."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

import pytest

import lohra.agent.loop as loop
from lohra.agent.types import ToolCall
from lohra.tools import fs  # noqa: F401 - register production handlers
from lohra.tools.registry import registry


def _calls(path):
    return tuple(ToolCall(id=value, name="write_file", arguments=json.dumps({
        "path": str(path), "content": value,
    })) for value in ("A", "B"))


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_base_exception_unwinds_without_join_and_cancels_the_active_queue_tail(tmp_path, monkeypatch, exception):
    entered, release, worker_finished, unwound = Event(), Event(), Event(), Event()
    shutdowns, dispatched, raised = [], [], []
    class InterruptedPool(ThreadPoolExecutor):
        def map(self, fn, *iterables, **kwargs):
            def execute(*args):
                try:
                    return fn(*args)
                finally:
                    worker_finished.set()
            super().map(execute, *iterables, **kwargs)
            assert entered.wait(5)
            raise exception()
        def shutdown(self, **kwargs):
            shutdowns.append(kwargs)
            return super().shutdown(**kwargs)
    monkeypatch.setattr(loop, "ThreadPoolExecutor", InterruptedPool)
    def dispatch(name, args):
        dispatched.append(args["content"])
        if args["content"] == "A":
            entered.set()
            assert release.wait(5)
        return registry.dispatch(name, args)
    path = tmp_path / "file.txt"
    def run():
        try:
            loop._execute_tool_calls(_calls(path), dispatch)
        except BaseException as exc:
            raised.append(exc)
        finally:
            unwound.set()
    runner = Thread(target=run)
    runner.start()
    try:
        assert entered.wait(5)
        assert unwound.wait(5), "executor joined the still-blocked tool"
        assert not worker_finished.is_set()
        assert len(raised) == 1 and isinstance(raised[0], exception)
        assert shutdowns == [{"wait": False, "cancel_futures": True}]
    finally:
        release.set()
        runner.join(5)
        assert worker_finished.wait(5)
    assert dispatched == ["A"]  # B was pending inside an already-running job
    assert path.read_text() == "A"


def test_base_exception_from_a_tool_does_not_run_its_following_file_call(tmp_path):
    seen = []
    def dispatch(name, args):
        seen.append(args["content"])
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        loop._execute_tool_calls(_calls(tmp_path / "file"), dispatch)
    assert seen == ["A"]


@pytest.mark.skipif(os.name != "posix", reason="SIGINT subprocess contract is POSIX")
def test_real_sigint_preserves_prompt_teardown_and_skips_queued_file_call(tmp_path):
    script = r'''
import json, os, signal, sys
from pathlib import Path
from threading import Event, Thread
from lohra.agent.loop import _execute_tool_calls
from lohra.agent.types import ToolCall
from lohra.tools import fs
from lohra.tools.registry import registry

target, forbidden = Path(sys.argv[1]), Path(sys.argv[2])
entered, release = Event(), Event()
def dispatch(name, args):
    if args['content'] == 'A':
        entered.set()
        assert release.wait(10)
    else:
        forbidden.write_text('queued call started')
    return registry.dispatch(name, args)
def interrupt():
    assert entered.wait(10)
    os.kill(os.getpid(), signal.SIGINT)
sender = Thread(target=interrupt)
sender.start()
interrupted = False
try:
    _execute_tool_calls(tuple(ToolCall(id=v, name='write_file', arguments=json.dumps({
        'path': str(target), 'content': v,
    })) for v in ('A', 'B')), dispatch)
except KeyboardInterrupt:
    interrupted = True
finally:
    release.set()
    sender.join(5)
print(json.dumps({'interrupted': interrupted}))
'''
    target, forbidden = tmp_path / "file.txt", tmp_path / "queued-ran.txt"
    result = subprocess.run([sys.executable, "-c", script, str(target), str(forbidden)],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"interrupted": True}
    assert target.read_text() == "A" and not forbidden.exists()
