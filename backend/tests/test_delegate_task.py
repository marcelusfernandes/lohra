"""Tests for delegate_task — isolated subagents (spec §6, Phase 5 half B).

Covers the four load-bearing requirements: fresh child context, iteration caps
(parent 90 / child 50), MAX_DEPTH=1 (no grandchildren), and concurrency cap 3.
Subagents run via a fake runner so no real API is touched.
"""

import json


from lohra.agent.delegate import (
    CHILD_MAX_ITERATIONS,
    MAX_DEPTH,
    PARENT_MAX_ITERATIONS,
    SUBAGENT_SYSTEM,
    DelegateTaskTool,
    build_child_agent,
    child_tool_definitions,
    make_child_factory,
    subagent_dispatch,
)
from lohra.tools.registry import tool_result


# --- fixtures / fakes ---------------------------------------------------------


def _defs(*names):
    """Build an OpenAI-style tool-definitions tuple from bare tool names."""
    return tuple(
        {"type": "function", "function": {"name": n, "description": n, "parameters": {}}}
        for n in names
    )


def _names(definitions):
    return {d["function"]["name"] for d in definitions}


class _Sentinel:
    """Stand-in for a provider/client; never called, only carried by the Agent."""


def _ok_result(text="done"):
    return {
        "final_response": text,
        "messages": [],
        "api_calls": 1,
        "completed": True,
        "partial": False,
        "interrupted": False,
        "error": None,
        "compacted": False,
    }


# --- child_tool_definitions ---------------------------------------------------


def test_child_definitions_strip_delegate_and_stateful_tools():
    parent = _defs("read_file", "terminal", "delegate_task", "memory", "session_search")
    child = child_tool_definitions(parent)
    assert _names(child) == {"read_file", "terminal"}


def test_child_definitions_keeps_plain_tools_untouched():
    parent = _defs("read_file", "write_file", "terminal")
    assert _names(child_tool_definitions(parent)) == {"read_file", "write_file", "terminal"}


def test_max_depth_is_one():
    assert MAX_DEPTH == 1


# --- subagent_dispatch (auto-deny + depth guard) ------------------------------


def test_subagent_dispatch_routes_safe_tools_to_base():
    base = lambda name, args: tool_result(via="base", name=name)  # noqa: E731
    dispatch = subagent_dispatch(base)
    out = json.loads(dispatch("read_file", {"path": "x"}))
    assert out["via"] == "base"


def test_subagent_dispatch_auto_denies_dangerous_terminal():
    base = lambda name, args: tool_result(ran=True)  # noqa: E731
    dispatch = subagent_dispatch(base)
    out = json.loads(dispatch("terminal", {"command": "rm -rf /tmp/x"}))
    assert "error" in out
    assert "ran" not in out  # base must never have executed


def test_subagent_dispatch_allows_safe_terminal():
    base = lambda name, args: tool_result(ran=True)  # noqa: E731
    dispatch = subagent_dispatch(base)
    out = json.loads(dispatch("terminal", {"command": "ls -la"}))
    assert out.get("ran") is True


def test_subagent_dispatch_blocks_delegate_task():
    base = lambda name, args: tool_result(ran=True)  # noqa: E731
    dispatch = subagent_dispatch(base)
    out = json.loads(dispatch("delegate_task", {"tasks": ["x"]}))
    assert "error" in out
    assert "ran" not in out


def test_subagent_dispatch_blocks_stateful_tools():
    base = lambda name, args: tool_result(ran=True)  # noqa: E731
    dispatch = subagent_dispatch(base)
    for name in ("memory", "skill_view", "skill_manage", "session_search"):
        out = json.loads(dispatch(name, {}))
        assert "error" in out, name


# --- build_child_agent (fresh context + caps) ---------------------------------


def test_child_agent_is_fresh_and_isolated():
    base = lambda name, args: tool_result()  # noqa: E731
    child = build_child_agent(
        model="m",
        provider=_Sentinel(),
        client=_Sentinel(),
        tool_definitions=_defs("read_file", "delegate_task", "memory"),
        tool_dispatch=subagent_dispatch(base),
    )
    assert child.memory_store is None
    assert child.skill_store is None
    assert child.context_files == ()
    assert child.identity is None  # no SOUL persona leaks into the child
    assert child.context_engine is None
    assert child.system_message == SUBAGENT_SYSTEM
    assert child.max_iterations == CHILD_MAX_ITERATIONS == 50


def test_child_agent_cannot_see_delegate_task():
    child = build_child_agent(
        model="m",
        provider=_Sentinel(),
        client=_Sentinel(),
        tool_definitions=_defs("read_file", "delegate_task"),
        tool_dispatch=lambda n, a: "",
    )
    assert "delegate_task" not in _names(child.tool_definitions)


def test_parent_cap_is_ninety():
    assert PARENT_MAX_ITERATIONS == 90


# --- make_child_factory -------------------------------------------------------


def test_factory_returns_fresh_agents_each_call():
    factory = make_child_factory(
        model="m",
        provider=_Sentinel(),
        client=_Sentinel(),
        tool_definitions=_defs("read_file", "delegate_task"),
    )
    a, b = factory(), factory()
    assert a is not b
    assert a.max_iterations == CHILD_MAX_ITERATIONS
    assert "delegate_task" not in _names(a.tool_definitions)


# --- DelegateTaskTool.handle: now backed by the OrchestrationCore (milestone C) -


def _core(outputs):
    from lohra.agent.agent import Agent
    from lohra.orchestration.core import OrchestrationCore
    from lohra.providers import get_provider_profile
    from lohra.state import SessionDB
    from tests.test_loop import FakeClient, _text_response

    queue = [[_text_response(o)] for o in outputs]

    def factory():
        responses = queue.pop(0) if queue else [_text_response("ok")]
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient(responses),
        )

    return OrchestrationCore(SessionDB(":memory:"), factory)


def _tool(core, parent=None):
    return DelegateTaskTool(core, parent)


def test_missing_tasks_errors():
    core = _core([])
    try:
        assert "error" in json.loads(_tool(core).handle({}))
        assert "error" in json.loads(_tool(core).handle({"tasks": []}))
        assert "error" in json.loads(_tool(core).handle({"tasks": [123]}))
        assert "error" in json.loads(_tool(core).handle({"tasks": ["   "]}))
    finally:
        core.shutdown()


def test_batch_returns_sub_ids_and_summaries():
    core = _core(["result A", "result B"])
    try:
        out = json.loads(_tool(core, parent="p1").handle({"tasks": ["task a", "task b"]}))
        results = out["results"]
        assert len(results) == 2
        assert all("sub_id" in r for r in results)
        assert {r["summary"] for r in results} == {"result A", "result B"}
        assert all(r["status"] == "complete" for r in results)
    finally:
        core.shutdown()


def test_string_tasks_coerced_to_single_item():
    core = _core(["only"])
    try:
        out = json.loads(_tool(core).handle({"tasks": "lone task"}))
        assert len(out["results"]) == 1
    finally:
        core.shutdown()


def test_resume_continues_an_existing_child():
    # first turn -> "first"; resume steers the SAME child -> second turn -> "second".
    from lohra.agent.agent import Agent
    from lohra.orchestration.core import OrchestrationCore
    from lohra.providers import get_provider_profile
    from lohra.state import SessionDB
    from tests.test_loop import FakeClient, _text_response

    def factory():
        # one child, two turns' worth of responses
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response("first"), _text_response("second")]),
        )

    core = OrchestrationCore(SessionDB(":memory:"), factory)
    try:
        tool = _tool(core)
        first = json.loads(tool.handle({"tasks": ["start"]}))
        sub_id = first["results"][0]["sub_id"]
        assert first["results"][0]["summary"] == "first"
        resumed = json.loads(tool.handle({"resume_id": sub_id, "tasks": ["keep going"]}))
        assert resumed["results"][0]["sub_id"] == sub_id
        assert resumed["results"][0]["summary"] == "second"
    finally:
        core.shutdown()


def test_batch_isolates_a_failing_spawn():
    # one spawn raising must not abort the batch or orphan the others.
    from lohra.agent.agent import Agent
    from lohra.orchestration.core import OrchestrationCore
    from lohra.providers import get_provider_profile
    from lohra.state import SessionDB
    from tests.test_loop import FakeClient, _text_response

    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 2:  # the 2nd task's spawn blows up
            raise RuntimeError("spawn boom")
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response("ok")]),
        )

    core = OrchestrationCore(SessionDB(":memory:"), factory)
    try:
        out = json.loads(DelegateTaskTool(core).handle({"tasks": ["a", "b", "c"]}))
        results = out["results"]
        assert len(results) == 3
        assert results[1]["status"] == "error" and "spawn boom" in results[1]["summary"]
        assert results[0]["status"] == "complete" and results[2]["status"] == "complete"
    finally:
        core.shutdown()


def test_resume_unknown_id_errors():
    core = _core([])
    try:
        out = json.loads(_tool(core).handle({"resume_id": "ghost", "tasks": ["go"]}))
        assert "error" in out
    finally:
        core.shutdown()


def test_resume_requires_a_followup_task():
    core = _core([])
    try:
        out = json.loads(_tool(core).handle({"resume_id": "x", "tasks": []}))
        assert "error" in out
    finally:
        core.shutdown()


# --- registry registration ----------------------------------------------------


def test_register_schema_and_intercepted_fallback():
    from lohra.agent.delegate import register_delegate_task_schema
    from lohra.tools import registry

    register_delegate_task_schema()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "delegate_task" in names
    # Reached only if wiring forgot to intercept -> must fail safe, not run.
    out = json.loads(registry.dispatch("delegate_task", {"tasks": ["x"]}))
    assert "error" in out


# --- _summary envelope: error_kind / tokens / route (issue #88, E7a) ----------
#
# ``collect()``/``collect_session`` already expose the child's structured
# outcome (error_kind, the five usage meters, provider/model, forced_fallback,
# usage_uncertain, retry_after) — ``_summary()`` used to throw all of that away
# and hand the parent only sub_id/status/summary. A parent that delegated to a
# child which then hit a refused credential had no structural signal at all,
# only the error's prose (which also happens to survive today, via `summary`).


class _DuckAuthError(Exception):
    """Duck-typed 401, same shape ``classify_provider_error`` already reads."""

    def __init__(self, message, *, status_code=401):
        super().__init__(message)
        self.status_code = status_code


def test_a_dead_child_s_envelope_carries_error_kind_and_route():
    from lohra.agent.agent import Agent
    from lohra.orchestration.core import OrchestrationCore
    from lohra.providers import get_provider_profile
    from lohra.state import SessionDB
    from tests.test_loop import FakeClient

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_DuckAuthError("invalid x-api-key")]),
        )

    core = OrchestrationCore(SessionDB(":memory:"), factory)
    try:
        out = json.loads(DelegateTaskTool(core).handle({"tasks": ["go"]}))
        result = out["results"][0]
        # today: status is "error" and summary carries the raw prose — that much
        # already worked and must keep working.
        assert result["status"] == "error"
        assert "invalid x-api-key" in result["summary"]
        # desired (currently missing): the same structured fields
        # collect_session already exposes for this same sub_id, same names.
        assert result.get("error_kind") == "auth_failed"
        assert result.get("provider") == "anthropic"
        assert result.get("model") == "claude-opus-4-8"
        assert "tokens_in" in result
        assert "tokens_out" in result
    finally:
        core.shutdown()


def test_a_successful_child_s_envelope_carries_tokens_and_route():
    core = _core(["result A"])
    try:
        out = json.loads(_tool(core).handle({"tasks": ["task a"]}))
        result = out["results"][0]
        assert result["status"] == "complete"
        assert result.get("provider") == "anthropic"
        assert result.get("model") == "claude-opus-4-8"
        assert "tokens_in" in result and "tokens_out" in result
        # a clean success never had an error, so the classifier field is absent
        # rather than fabricated as None/"" — never invented.
        assert "error_kind" not in result
        assert "retry_after" not in result
    finally:
        core.shutdown()


def test_a_child_with_usage_uncertain_carries_the_flag():
    # An interrupted turn is the one path that sets usage_uncertain (issue #42).
    from lohra.agent.agent import Agent
    from lohra.orchestration.core import OrchestrationCore
    from lohra.providers import get_provider_profile
    from lohra.state import SessionDB

    class _AbortingClient:
        """Returns an aborted-stream sentinel the loop reads as interrupted."""

        def create(self, **kwargs):  # pragma: no cover - GatewaySession always streams
            raise AssertionError("orchestration children stream, not create()")

        def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
            from lohra.agent.stream_abort import AbortedStream

            return AbortedStream()

        def close(self):
            pass

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=_AbortingClient(),
        )

    core = OrchestrationCore(SessionDB(":memory:"), factory)
    try:
        out = json.loads(DelegateTaskTool(core).handle({"tasks": ["go"]}))
        result = out["results"][0]
        assert result["status"] == "interrupted"
        assert result.get("usage_uncertain") is True
    finally:
        core.shutdown()


def test_envelope_keys_are_a_closed_set_matching_collect_session():
    # Anti-drift: the extra fields _summary() may add are exactly the fields
    # collect()/collect_session already expose beyond status/output — a single
    # shared tuple, so the two names can never quietly diverge.
    from lohra.orchestration.core import SUB_SESSION_METRIC_FIELDS
    from lohra.orchestration.tools import OrchestrationTool

    core = _core(["result A"])
    try:
        out = json.loads(_tool(core).handle({"tasks": ["task a"]}))
        result = out["results"][0]
        extra_keys = set(result) - {"sub_id", "status", "summary"}
        assert extra_keys <= set(SUB_SESSION_METRIC_FIELDS)

        collected = core.collect(result["sub_id"])
        assert set(collected) - {"status", "output"} == set(SUB_SESSION_METRIC_FIELDS)

        # ...and through the ACTUAL collect_session tool (not just core.collect
        # directly) — tools.py wraps it in tool_result, which adds "ok"; that's
        # the only extra key a future change to tools.py could introduce
        # without this test noticing.
        via_tool = json.loads(
            OrchestrationTool(core).collect({"sub_id": result["sub_id"]})
        )
        assert set(via_tool) - {"ok", "status", "output"} == set(SUB_SESSION_METRIC_FIELDS)
    finally:
        core.shutdown()
