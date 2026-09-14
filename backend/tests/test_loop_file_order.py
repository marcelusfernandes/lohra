"""#97: emitted file-call order, even when the scheduler starts later jobs first."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

import lohra.agent.loop as loop
import lohra.agent.tool_batches as tool_batches
from lohra.agent.types import ToolCall
from lohra.tools import fs  # noqa: F401 - register the real file handlers
from lohra.tools.registry import registry
from lohra.tools.sandbox_denials import denial_of
from lohra.workflow.sandbox import WorkflowPolicy, sandbox_dispatch
from tests.test_loop import _make_agent, _text_response, _tool_call_response


class LaterJobFirstPool(ThreadPoolExecutor):
    """A legal, deterministic worker schedule; no sleep or probabilistic race."""

    def __init__(self, max_workers, **kwargs):
        super().__init__(max_workers=max_workers, **kwargs)
        self._delay_first = max_workers > 1
        self._submitted = 0
        self._later_done = Event()

    def submit(self, fn, *args, **kwargs):
        index = self._submitted
        self._submitted += 1
        def execute():
            if index == 0 and self._delay_first:
                assert self._later_done.wait(5), "later job never completed"
            try:
                return fn(*args, **kwargs)
            finally:
                if index > 0:
                    self._later_done.set()
        return super().submit(execute)


def test_same_file_calls_follow_emitted_order_under_reversed_worker_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    path = tmp_path / "shared.txt"
    completed = []
    def dispatch(name, args):
        result = registry.dispatch(name, args)
        completed.append(args["content"])
        return result
    calls = [("first", "write_file", {"path": str(path), "content": "A"}),
             ("second", "write_file", {"path": str(path), "content": "B"})]
    agent = _make_agent([_tool_call_response(calls), _text_response("done")], tool_dispatch=dispatch)

    result = loop.run_conversation(agent, "write A then B")

    assert (completed, path.read_text()) == (["A", "B"], "B")
    messages = [m for m in result["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in messages] == ["first", "second"]
    assert all(json.loads(m["content"])["ok"] for m in messages)
    assert result["completed"] is True and result["error"] is None


def _run(calls, dispatch=registry.dispatch):
    agent = _make_agent([_tool_call_response(calls), _text_response("done")], tool_dispatch=dispatch)
    result = loop.run_conversation(agent, "execute the file calls")
    return result, [m for m in result["messages"] if m["role"] == "tool"]


@pytest.mark.parametrize("alias", ["relative", "dotdot", "file_symlink", "parent_symlink", "symlink_dotdot", "missing_suffix"])
def test_file_aliases_are_ordered_without_rewriting_dispatch_arguments(tmp_path, monkeypatch, alias):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    target = tmp_path / "shared.txt"
    target.write_text("seed")
    if alias == "relative":
        alias_path = "shared.txt"
    elif alias == "dotdot":
        (tmp_path / "sub").mkdir()
        alias_path = str(tmp_path / "sub" / ".." / "shared.txt")
    elif alias == "file_symlink":
        (tmp_path / "alias.txt").symlink_to(target)
        alias_path = str(tmp_path / "alias.txt")
    elif alias == "parent_symlink":
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        alias_path = str(tmp_path / "alias" / "shared.txt")
    elif alias == "symlink_dotdot":
        (tmp_path / "real" / "deep").mkdir(parents=True)
        (tmp_path / "alias").symlink_to(tmp_path / "real" / "deep", target_is_directory=True)
        target = tmp_path / "real" / "shared.txt"
        alias_path = str(tmp_path / "alias" / ".." / "shared.txt")
    else:
        target = tmp_path / "future" / "shared.txt"
        (tmp_path / "alias").symlink_to(tmp_path / "future", target_is_directory=True)
        alias_path = str(tmp_path / "alias" / "shared.txt")
    calls = [("a", "write_file", {"path": str(target), "content": "A"}),
             ("b", "write_file", {"path": alias_path, "content": "B"})]
    observed = []
    def dispatch(name, args):
        observed.append((name, dict(args)))
        return registry.dispatch(name, args)
    result, messages = _run(calls, dispatch)
    assert observed == [(name, args) for _, name, args in calls]
    assert target.read_text() == "B"
    assert [m["tool_call_id"] for m in messages] == ["a", "b"]
    assert all(json.loads(m["content"])["ok"] for m in messages)
    assert result["completed"]


@pytest.mark.parametrize("kind", ["different_files", "literal_tilde", "unrelated_tools"])
def test_different_resources_really_overlap(tmp_path, monkeypatch, kind):
    monkeypatch.chdir(tmp_path)
    a_entered, b_entered = Event(), Event()
    if kind == "literal_tilde":
        # File handlers do not expand '~'; neither may resource planning.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        a_path, b_path = "~/file.txt", str(tmp_path / "home" / "file.txt")
    else:
        a_path, b_path = str(tmp_path / "a.txt"), str(tmp_path / "b.txt")
    name = "independent_tool" if kind == "unrelated_tools" else "write_file"
    if kind == "unrelated_tools":
        a_path = b_path = "/same-path-field"
        class NoPathProbe:
            def __init__(self, *_):
                pytest.fail("unknown tool's path must never be resolved")
        monkeypatch.setattr(tool_batches, "Path", NoPathProbe)
    calls = [("a", name, {"path": a_path, "content": "A"}),
             ("b", name, {"path": b_path, "content": "B"})]
    def dispatch(name, args):
        own, peer = (a_entered, b_entered) if args["content"] == "A" else (b_entered, a_entered)
        own.set()
        assert peer.wait(5), "independent resource could not start concurrently"
        return '{"ok":true}' if kind == "unrelated_tools" else registry.dispatch(name, args)
    result, messages = _run(calls, dispatch)
    assert all(json.loads(m["content"])["ok"] for m in messages)
    assert result["completed"] and [m["tool_call_id"] for m in messages] == ["a", "b"]
    if kind != "unrelated_tools":
        assert Path(a_path).read_text() == "A" and Path(b_path).read_text() == "B"


def test_overwrite_read_append_read_observe_emitted_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    path = str(tmp_path / "shared.txt")
    calls = [("overwrite", "write_file", {"path": path, "content": "A"}),
             ("read-a", "read_file", {"path": path}),
             ("append", "write_file", {"path": path, "content": "B", "mode": "append"}),
             ("read-ab", "read_file", {"path": path})]
    result, messages = _run(calls)
    assert [m["tool_call_id"] for m in messages] == [row[0] for row in calls]
    assert json.loads(messages[1]["content"])["data"] == "A"
    assert json.loads(messages[3]["content"])["data"] == "AB"
    assert Path(path).read_text() == "AB" and result["completed"]


def test_interleaved_resource_groups_keep_original_result_indices(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    x, y = str(tmp_path / "x.txt"), str(tmp_path / "y.txt")
    calls = [("x-a", "write_file", {"path": x, "content": "A"}),
             ("y-c", "write_file", {"path": y, "content": "C"}),
             ("x-b", "write_file", {"path": x, "content": "B", "mode": "append"}),
             ("read-y", "read_file", {"path": y}),
             ("read-x", "read_file", {"path": x})]
    _, messages = _run(calls)
    assert [m["tool_call_id"] for m in messages] == [row[0] for row in calls]
    assert json.loads(messages[3]["content"])["data"] == "C"
    assert json.loads(messages[4]["content"])["data"] == "AB"


@pytest.mark.parametrize("arguments", [None, "{not-json", '{"path":null}', '{"path":17,"content":"x"}'])
def test_invalid_file_arguments_still_reach_the_original_error_handler(tmp_path, arguments):
    target = tmp_path / "valid.txt"
    messages = loop._execute_tool_calls((
        ToolCall(id="invalid", name="write_file", arguments=arguments),
        ToolCall(id="valid", name="write_file", arguments=json.dumps({"path": str(target), "content": "ok"})),
    ), registry.dispatch)
    assert "error" in json.loads(messages[0]["content"])
    assert json.loads(messages[1]["content"])["ok"] and target.read_text() == "ok"


@pytest.mark.parametrize("custom_error", [False, True])
def test_a_tool_error_does_not_skip_later_calls_on_the_same_file(tmp_path, monkeypatch, custom_error):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    path = str(tmp_path / "file.txt")
    calls = [("failed-read", "read_file", {"path": path}),
             ("write", "write_file", {"path": path, "content": "after error"}),
             ("read", "read_file", {"path": path})]
    dispatched = []
    def dispatch(name, args):
        dispatched.append(name)
        if custom_error and len(dispatched) == 1:
            raise RuntimeError("synthetic dispatch failure")
        return registry.dispatch(name, args)
    result, messages = _run(calls, dispatch)
    assert "error" in json.loads(messages[0]["content"])
    assert json.loads(messages[1]["content"])["ok"]
    assert json.loads(messages[2]["content"])["data"] == "after error"
    assert dispatched == ["read_file", "write_file", "read_file"] and result["completed"]


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_identity_failure_is_silent_and_conservatively_orders_file_calls(tmp_path, monkeypatch, error_type):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    class UnresolvedPath:
        def __init__(self, path):
            self.path = path
        def resolve(self, **kwargs):
            if self.path.endswith("a.txt"):
                raise error_type("private-identity-CANARY")
            return Path(self.path).resolve(**kwargs)
    monkeypatch.setattr(tool_batches, "Path", UnresolvedPath)
    calls = [("a", "write_file", {"path": str(tmp_path / "a.txt"), "content": "A"}),
             ("b", "write_file", {"path": str(tmp_path / "b.txt"), "content": "B"})]
    observed = []
    def dispatch(name, args):
        observed.append(args["content"])
        return registry.dispatch(name, args)
    result, messages = _run(calls, dispatch)
    assert observed == ["A", "B"]  # even different files share the uncertain queue
    assert all(json.loads(m["content"])["ok"] for m in messages)
    assert "CANARY" not in json.dumps(result, default=str)


@pytest.mark.parametrize("tainted", [False, True])
def test_identity_failure_cannot_change_sandbox_denials_or_leak_error(tmp_path, monkeypatch, tainted):
    class UnresolvedPath:
        def __init__(self, path):
            pass
        def resolve(self, **kwargs):
            raise PermissionError("private-identity-CANARY")
    monkeypatch.setattr(tool_batches, "Path", UnresolvedPath)
    dispatched = []
    def base(name, args):
        dispatched.append((name, args))
        return registry.dispatch(name, args)
    working = tmp_path / "work"
    calls = [("outside", "write_file", {"path": str(tmp_path / "outside.txt"), "content": "X"}),
             ("inside", "write_file", {"path": str(working / "inside.txt"), "content": "Y"})]
    dispatch = sandbox_dispatch(base, working_root=working, policy=WorkflowPolicy(), tainted=tainted)
    result, messages = _run(calls, dispatch)
    assert denial_of(messages[0]["content"]).reason == ("tainted_fs" if tainted else "fs_outside_scope")
    assert not (tmp_path / "outside.txt").exists()
    assert dispatched == ([] if tainted else [(calls[1][1], calls[1][2])])
    if tainted:
        assert denial_of(messages[1]["content"]).reason == "tainted_fs"
    else:
        assert (working / "inside.txt").read_text() == "Y"
    assert "CANARY" not in json.dumps(result, default=str)
