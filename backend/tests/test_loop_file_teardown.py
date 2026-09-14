"""Per-file queues must not join a running tool or start their tail on teardown."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        def submit(self, fn, *args, **kwargs):
            def execute():
                try:
                    return fn(*args, **kwargs)
                finally:
                    worker_finished.set()
            super().submit(execute)
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


def _named_call(name, path):
    return ToolCall(id=name, name="write_file", arguments=json.dumps({
        "path": str(path), "content": name,
    }))


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_later_worker_failure_unwinds_while_first_resource_tail_is_blocked(tmp_path, monkeypatch, exception):
    entered, release, unwound, finished = Event(), Event(), Event(), Event()
    raised, seen, shutdowns, pools = [], [], [], []
    failure = exception("original failure")
    class ObservedPool(ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pools.append(self)
        def shutdown(self, **kwargs):
            shutdowns.append(kwargs)
            return super().shutdown(**kwargs)
    monkeypatch.setattr(loop, "ThreadPoolExecutor", ObservedPool)
    x, y = tmp_path / "x", tmp_path / "y"
    calls = tuple(_named_call(name, path) for name, path in (
        ("head", x), ("fatal", y), ("blocked", x), ("never-x", x), ("never-y", y),
    ))
    def dispatch(name, args):
        marker = args["content"]
        seen.append(marker)
        if marker == "fatal":
            assert entered.wait(5)
            raise failure
        if marker == "blocked":
            entered.set()
            try:
                assert release.wait(8)
            finally:
                finished.set()
        return registry.dispatch(name, args)
    def run():
        try:
            loop._execute_tool_calls(calls, dispatch)
        except BaseException as exc:
            raised.append(exc)
        finally:
            unwound.set()
    runner = Thread(target=run)
    runner.start()
    try:
        assert entered.wait(5)
        assert unwound.wait(3), "later failure waited for the first resource queue"
        assert not finished.is_set() and raised == [failure]
        assert shutdowns == [{"wait": False, "cancel_futures": True}]
    finally:
        release.set()
        runner.join(5)
        pools[0].shutdown(wait=True)
    assert "never-x" not in seen and "never-y" not in seen
    assert x.read_text() == "blocked" and not y.exists()


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_worker_publishes_stop_before_the_result_consumer_observes_failure(tmp_path, monkeypatch, exception):
    entered, release, normal_finished = Event(), Event(), Event()
    seen = []
    failure = exception("original failure")
    class ObservedPool(ThreadPoolExecutor):
        def submit(self, fn, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            def completed(future):
                if future.exception() is failure:
                    release.set()
                else:
                    normal_finished.set()
            future.add_done_callback(completed)
            return future
    def delayed_consumer(futures):
        # The caller cannot publish stopping until this earlier worker finishes.
        assert normal_finished.wait(5)
        yield from as_completed(futures)
    monkeypatch.setattr(loop, "ThreadPoolExecutor", ObservedPool)
    monkeypatch.setattr(loop, "as_completed", delayed_consumer, raising=False)
    x, y = tmp_path / "x", tmp_path / "y"
    calls = tuple(_named_call(name, path) for name, path in (
        ("head", x), ("fatal", y), ("never-x", x), ("never-y", y),
    ))
    def dispatch(name, args):
        marker = args["content"]
        seen.append(marker)
        if marker == "head":
            entered.set()
            assert release.wait(5)
        if marker == "fatal":
            assert entered.wait(5)
            raise failure
        return registry.dispatch(name, args)
    try:
        with pytest.raises(exception) as caught:
            loop._execute_tool_calls(calls, dispatch)
    finally:
        release.set()
    assert caught.value is failure
    assert seen == ["head", "fatal"]
    assert x.read_text() == "head" and not y.exists()


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
