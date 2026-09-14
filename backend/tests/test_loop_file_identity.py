"""Filesystem identity, not spelling, determines intra-batch file ordering."""

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import lohra.agent.loop as loop
import lohra.agent.tool_batches as tool_batches
from lohra.agent.types import ToolCall
from lohra.tools.registry import registry
from tests.test_loop_file_order import LaterJobFirstPool


def _call(identifier, path, content=None, mode="overwrite"):
    args = {"path": str(path)}
    if content is not None:
        args.update(content=content, mode=mode)
    return ToolCall(id=identifier, name="read_file" if content is None else "write_file",
                    arguments=json.dumps(args))


@pytest.mark.parametrize("alias_kind", ["case", "parent_case", "hardlink"])
def test_existing_file_identity_orders_reads_writes_and_appends(tmp_path, monkeypatch, alias_kind):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    parent = tmp_path / "Parent"
    parent.mkdir()
    target = parent / "Case.txt"
    target.write_text("seed")
    if alias_kind == "hardlink":
        alias = tmp_path / "hardlink.txt"
        alias.hardlink_to(target)
    else:
        alias = parent / "case.txt" if alias_kind == "case" else tmp_path / "parent" / "Case.txt"
        if not alias.exists() or not alias.samefile(target):
            pytest.skip("this volume treats the tested case spellings as distinct")
    calls = (_call("write", target, "A"), _call("read-a", alias),
             _call("append", alias, "B", "append"), _call("read-ab", target))
    observed = []
    def dispatch(name, args):
        observed.append((name, dict(args)))
        return registry.dispatch(name, args)

    results = loop._execute_tool_calls(calls, dispatch)

    assert observed == [(call.name, json.loads(call.arguments)) for call in calls]
    assert [row["tool_call_id"] for row in results] == [call.id for call in calls]
    assert all(json.loads(row["content"])["ok"] for row in results)
    assert [json.loads(results[i]["content"])["data"] for i in (1, 3)] == ["A", "AB"]
    assert target.read_text() == alias.read_text() == "AB"


@pytest.mark.parametrize("suffixes", [
    ("Case.txt", "case.txt"), ("New/Case.txt", "new/case.txt"), ("É.txt", "e\u0301.txt"),
])
def test_missing_case_collisions_are_conservatively_ordered(tmp_path, monkeypatch, suffixes):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    target, alias = (tmp_path / suffix for suffix in suffixes)
    assert not target.exists() and not alias.exists()
    calls = (_call("a", target, "A"), _call("b", alias, "B"))
    observed = []
    def dispatch(name, args):
        observed.append(args["content"])
        return registry.dispatch(name, args)

    results = loop._execute_tool_calls(calls, dispatch)

    assert observed == ["A", "B"]
    assert all(json.loads(row["content"])["ok"] for row in results)
    # No assumption about this volume: either spelling may create its own file.
    assert target.read_text() == ("B" if target.samefile(alias) else "A")
    assert alias.read_text() == "B"


@pytest.mark.parametrize("missing_leaf", [False, True])
def test_proven_distinct_case_resources_really_overlap(tmp_path, monkeypatch, missing_leaf):
    # Synthetic stat evidence exercises a case-sensitive filesystem on every OS.
    # No real file writes: this host may resolve both names to the same file.
    class DistinctFiles(type(Path())):
        def stat(self, **kwargs):
            if self.name in {"Case", "case"}:
                return SimpleNamespace(st_dev=1, st_ino=1 if self.name == "Case" else 2)
            return super().stat(**kwargs)
    monkeypatch.setattr(tool_batches, "Path", DistinctFiles)
    entered = {"A": Event(), "B": Event()}
    def dispatch(name, args):
        own = args["content"]
        entered[own].set()
        assert entered["B" if own == "A" else "A"].wait(5), "distinct inodes were serialized"
        return '{"ok":true}'

    a, b = tmp_path / "Case", tmp_path / "case"
    if missing_leaf:
        a, b = a / "new.txt", b / "new.txt"
    results = loop._execute_tool_calls((_call("a", a, "A"), _call("b", b, "B")), dispatch)

    assert all(json.loads(row["content"])["ok"] for row in results)


def test_missing_files_under_existing_case_alias_parents_are_ordered(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    parent, alias = tmp_path / "Parent", tmp_path / "parent"
    parent.mkdir()
    if not alias.exists() or not parent.samefile(alias):
        pytest.skip("this volume treats the tested parent spellings as distinct")
    calls = (_call("a", parent / "new.txt", "A"), _call("b", alias / "new.txt", "B"))

    results = loop._execute_tool_calls(calls, registry.dispatch)

    assert all(json.loads(row["content"])["ok"] for row in results)
    assert (parent / "new.txt").read_text() == "B"


def test_stat_failure_is_silent_and_preserves_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "ThreadPoolExecutor", LaterJobFirstPool)
    class FailedStat(type(Path())):
        def stat(self, **kwargs):
            raise PermissionError("private-stat-CANARY")
    monkeypatch.setattr(tool_batches, "Path", FailedStat)
    observed = []
    def dispatch(name, args):
        observed.append(args["content"])
        return registry.dispatch(name, args)
    calls = (_call("a", tmp_path / "a.txt", "A"), _call("b", tmp_path / "b.txt", "B"))

    results = loop._execute_tool_calls(calls, dispatch)

    assert observed == ["A", "B"]
    assert all(json.loads(row["content"])["ok"] for row in results)
    assert "CANARY" not in json.dumps(results)
