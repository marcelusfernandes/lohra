"""#55: search egress requires an operator opt-in, on both leaf surfaces."""

import json

import pytest

from lohra.orchestration.core import OrchestrationCore
from lohra.state import SessionDB
from lohra.tools.sandbox_denials import denial_of
from lohra.workflow.cache import content_hash
from lohra.workflow.sandbox import WorkflowPolicy, load_policy, sandbox_dispatch, sandbox_tool_definitions
from lohra.workflow.service import WorkflowService
from tests.test_workflow_sandbox_denials import _factory


@pytest.fixture(autouse=True)
def no_search_env(monkeypatch):
    monkeypatch.delenv("LOHRA_LEAF_ALLOW_SEARCH", raising=False)


def test_default_policy_refuses_search_before_base_dispatch(tmp_path):
    reached = []
    def base(name, args):
        reached.append((name, args))
        return '{"ok":true}'
    dispatch = sandbox_dispatch(base, working_root=tmp_path, policy=WorkflowPolicy(), tainted=False)
    result = dispatch("web_search", {"query": "private-CANARY"})
    assert reached == []
    assert "allow_search" in json.loads(result)["error"]
    assert "LOHRA_LEAF_ALLOW_SEARCH" in result
    assert denial_of(result).reason == "search_disabled"


@pytest.mark.parametrize("opt_in,tainted,allowed", [
    (False, False, False), (True, False, True), (True, True, False),
])
@pytest.mark.parametrize("nested,audit_enabled", [(False, True), (True, True), (False, False)])
def test_operator_json_reaches_real_service_and_leaf(
    tmp_path, monkeypatch, opt_in, tainted, allowed, nested, audit_enabled,
):
    monkeypatch.setenv("LOHRA_AUDIT", "on" if audit_enabled else "off")
    (tmp_path / "workflow_policy.json").write_text(json.dumps({"allow_search": opt_in}))
    reached, agents = [], []
    calls = [("web_search", {"query": "private-CANARY"})]
    base_factory = _factory(calls, reached)
    def factory():
        agent = base_factory()
        agents.append(agent)  # inspect AFTER the real sandbox wrapped this object
        return agent
    db = SessionDB(":memory:")
    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        spec = {"meta": {"name": "search-policy"}, "nodes": [
            {"id": "searcher", "type": "agent", "prompt": "search"},
        ]}
        expected = {"searcher": "done"}
        if nested:
            templates = tmp_path / "workflows" / "templates"
            templates.mkdir(parents=True)
            (templates / "search-policy.json").write_text(json.dumps(spec))
            spec = {"meta": {"name": "parent"}, "nodes": [
                {"id": "call", "type": "workflow", "ref": "search-policy"},
            ]}
            expected = {"call": expected}
        run_id = service.start(spec, tainted=tainted)["run_id"]
        result = service.status(run_id, wait=True, timeout=5)
        assert reached == (calls if allowed else [])
        assert len(agents) == 1
        visible = {d["function"]["name"] for d in agents[0].tool_definitions}
        assert ("web_search" in visible) is allowed
        assert result["status"] == "complete" and result["outputs"] == expected
        assert len(result["advisory_faults"]) == int(not allowed)
        assert service._audit.flush(timeout=5)
        audit = db.audit_query(run_id)
        assert audit["sandbox"]["denied_tool_calls"] == int(not allowed and audit_enabled)
        if audit_enabled:
            tool = next(e for e in audit["events"] if e["event_type"] == "tool.completed")
            if not allowed:
                assert tool["data"]["result"]["reason"] == ("tainted_egress" if tainted else "search_disabled")
            else:
                # The base returns 'sandbox denied' prose; it is not evidence.
                assert "denied" not in tool["data"]["result"]
        else:
            assert audit["availability"] == "unavailable"
        if not allowed:
            prefix = "sub[call]: searcher" if nested else "searcher"
            assert result["advisory_faults"][0].startswith(f"{prefix}: 1 tool calls denied by sandbox: web_search")
        assert "private-CANARY" not in json.dumps(audit)
    finally:
        service.shutdown()
        db.close()


@pytest.mark.parametrize("value", [False, "true", "false", 1, 0, None, [], {}])
def test_only_real_boolean_true_grants_search(tmp_path, value):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"allow_search": value}))
    for policy in [load_policy(path), WorkflowPolicy(allow_search=value)]:
        dispatch = sandbox_dispatch(
            lambda *_: pytest.fail("non-boolean opt-in reached base"),
            working_root=tmp_path, policy=policy, tainted=False,
        )
        assert denial_of(dispatch("web_search", {"query": "q"})).reason == "search_disabled"


@pytest.mark.parametrize("file_contents", [None, "invalid json", "[]", "{}", '{"allow_search":false}'])
@pytest.mark.parametrize("env", ["1", " on ", "TRUE", "yes"])
def test_env_can_enable_search_with_missing_bad_or_closed_policy(tmp_path, monkeypatch, file_contents, env):
    path = tmp_path / "policy.json"
    if file_contents is not None:
        path.write_text(file_contents)
    monkeypatch.setenv("LOHRA_LEAF_ALLOW_SEARCH", env)
    policy = load_policy(path)
    reached = []
    dispatch = sandbox_dispatch(lambda *args: reached.append(args), working_root=tmp_path,
                                policy=policy, tainted=False)
    dispatch("web_search", {"query": "q"})
    assert reached == [("web_search", {"query": "q"})]


@pytest.mark.parametrize("env", ["", "0", "off", "FALSE", "no", "maybe"])
@pytest.mark.parametrize("file_opt_in", [False, True])
def test_env_only_widens_the_file_policy(tmp_path, monkeypatch, caplog, env, file_opt_in):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"allow_search": file_opt_in}))
    monkeypatch.setenv("LOHRA_LEAF_ALLOW_SEARCH", env)
    assert load_policy(path).allow_search is file_opt_in
    if env == "maybe" and not file_opt_in:
        assert "LOHRA_LEAF_ALLOW_SEARCH" in caplog.text
    else:
        assert caplog.records == []


def test_explicit_policy_does_not_inherit_file_or_env_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("LOHRA_LEAF_ALLOW_SEARCH", "1")
    (tmp_path / "workflow_policy.json").write_text('{"allow_search":true}')
    reached = []
    db = SessionDB(":memory:")
    service = WorkflowService(
        base_child_factory=_factory([("web_search", {"query": "q"})], reached),
        db=db, home=tmp_path, policy=WorkflowPolicy(),
    )
    try:
        run_id = service.start({"meta": {"name": "explicit"}, "nodes": [
            {"id": "searcher", "type": "agent", "prompt": "search"},
        ]})["run_id"]
        result = service.status(run_id, wait=True, timeout=5)
        assert reached == [] and len(result["advisory_faults"]) == 1
    finally:
        service.shutdown()
        db.close()


def test_search_and_fetch_permissions_are_independent(tmp_path):
    calls = []
    for policy in [WorkflowPolicy(egress_allow=("html.duckduckgo.com",)),
                   WorkflowPolicy(allow_search=True)]:
        dispatch = sandbox_dispatch(lambda *args: calls.append(args), working_root=tmp_path,
                                    policy=policy, tainted=False)
        search = dispatch("web_search", {"query": "q"})
        fetch = dispatch("web_fetch", {"url": "https://html.duckduckgo.com/"})
        assert (denial_of(search) is None) is policy.allow_search
        assert (denial_of(fetch) is None) is bool(policy.egress_allow)
    assert [name for name, _ in calls] == ["web_fetch", "web_search"]


def test_filtering_search_keeps_other_definitions_and_parent_unchanged():
    definitions = tuple({"function": {"name": name}} for name in (
        "web_search", "web_fetch", "read_file", "some_other_tool",
    ))
    for tainted in [False, True]:
        filtered = sandbox_tool_definitions(definitions, policy=WorkflowPolicy(), tainted=tainted)
        assert filtered == definitions[1:]
        assert definitions[0]["function"]["name"] == "web_search"
    assert sandbox_tool_definitions(definitions, policy=WorkflowPolicy(allow_search=True),
                                    tainted=False) == definitions


@pytest.mark.parametrize("location", ["node", "root"])
def test_authored_spec_cannot_grant_search(tmp_path, location):
    reached = []
    db = SessionDB(":memory:")
    service = WorkflowService(base_child_factory=_factory([("web_search", {"query": "q"})], reached),
                              db=db, home=tmp_path)
    spec = {"meta": {"name": "injected"}, "nodes": [
        {"id": "searcher", "type": "agent", "prompt": "search"},
    ]}
    (spec["nodes"][0] if location == "node" else spec)["allow_search"] = True
    try:
        started = service.start(spec)
        if "run_id" in started:  # unknown top-level fields may be ignored by validation
            result = service.status(started["run_id"], wait=True, timeout=5)
            assert len(result["advisory_faults"]) == 1
        else:
            assert started.get("invalid_spec") is True
        assert reached == []
    finally:
        service.shutdown()
        db.close()


@pytest.mark.parametrize("legacy_stamp", [False, True])
def test_search_policy_change_is_marked_on_replay_without_reexecution(tmp_path, monkeypatch, legacy_stamp):
    monkeypatch.setenv("LOHRA_AUDIT", "on")
    path = tmp_path / "policy-state.db"
    policy_file = tmp_path / "workflow_policy.json"
    policy_file.write_text('{"allow_search":true}')
    db = SessionDB(path)
    reached = []
    factory = _factory([("web_search", {"query": "q"})], reached)
    spec = {"meta": {"name": "cached-search"}, "nodes": [
        {"id": "searcher", "type": "agent", "prompt": "search"},
    ]}
    first = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    run_id = first.start(spec)["run_id"]
    assert first.status(run_id, wait=True, timeout=5)["status"] == "complete"
    first.shutdown()
    assert len(reached) == 1
    if legacy_stamp:
        # Exact pre-#55 policy shape: no search field, implicit search permission.
        old_hash = content_hash({"allow_terminal": False, "egress_allow": [],
                                 "fs_allow": [], "mcp_allow": []})
        db._connection.execute("UPDATE workflow_node_cache SET policy_hash=?", (old_hash,))
        db._connection.commit()
    db.close()
    policy_file.write_text('{"allow_search":false}')
    db = SessionDB(path)
    second = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        assert second.start(resume_run_id=run_id)["cache_preview"]["replay"] == 1
        result = second.status(run_id, wait=True, timeout=5)
        assert result["status"] == "complete" and len(reached) == 1
        assert len(result["advisory_faults"]) == 1
        assert "different sandbox policy" in result["advisory_faults"][0]
        assert second._audit.flush(timeout=5)
        replays = db.audit_query(run_id, event_type="cache.replayed")["events"]
        assert [row["data"].get("reason") for row in replays] == ["policy_changed"]
    finally:
        second.shutdown()
        db.close()


def test_search_outside_workflow_sandbox_still_reaches_base():
    db = SessionDB(":memory:")
    reached = []
    calls = [("web_search", {"query": "q"})]
    core = OrchestrationCore(db, _factory(calls, reached))
    try:
        leaf = core.spawn("search outside workflows")
        assert core.collect(leaf, wait=True, timeout=5)["status"] == "complete"
        assert reached == calls
    finally:
        core.shutdown()
        db.close()
