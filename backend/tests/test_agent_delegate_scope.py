"""Anti-drift: the closed set of tools a delegated subagent may ever receive.

Issue #84 (Wave 9, epic E6). Hypothesis under test: the denylist in
``agent/delegate.py:_CHILD_EXCLUDED_TOOLS`` is correct TODAY (every stateful or
author-time-only tool is excluded, including ``list_models``), but nothing pins
the *set* — a new tool registered without an explicit doctrine decision would
silently reach the child.

``child_tool_definitions`` is the single production filter for this: both
``delegate_task`` subagents (agent/delegate.py) and the agentic HTTP server
allow-list (server/agentic.py:31 calls it before applying its own allow-list)
reuse it. MCP tools are deliberately OUT of this closed table — they are
dynamic/config-dependent (no MCP server is configured in this test), a
separate, already-tracked gap (see the report for issue #84).
"""

from __future__ import annotations

import pytest

from lohra.agent.delegate import child_tool_definitions
from lohra.agent.equip import register_all_tools
from lohra.tools import registry


@pytest.fixture(autouse=True)
def _registered():
    # register_all_tools() is idempotent and registers every built-in +
    # intercepted schema (no MCP server is configured here).
    register_all_tools()


# The ONLY tool names a delegated subagent receives today. Any addition or
# removal must be a deliberate doctrine decision — bumping this set is the
# signal that a reviewer decided a new tool's scope, not an accident of it
# reaching the registry with no exclusion.
DELEGATED_SUBAGENT_TOOLS = frozenset(
    {
        "terminal",
        "read_file",
        "write_file",
        "web_fetch",
        "web_search",
    }
)


def test_delegated_subagent_receives_exactly_the_closed_set():
    parent_definitions = tuple(registry.get_definitions())
    child_definitions = child_tool_definitions(parent_definitions)
    names = {d["function"]["name"] for d in child_definitions}
    assert names == DELEGATED_SUBAGENT_TOOLS


def test_every_author_time_only_tool_is_absent_from_subagent_definitions():
    """Rule test: the ``author_time_only`` registry flag, not a hand-written
    list, decides what a subagent must never see. A tool registered with the
    flag set but missing from the exclusion is a doctrine violation caught
    here instead of silently leaking to a child.
    """
    parent_definitions = tuple(registry.get_definitions())
    child_definitions = child_tool_definitions(parent_definitions)
    child_names = {d["function"]["name"] for d in child_definitions}

    flagged = registry.author_time_only_names()
    assert flagged, "expected at least one author_time_only tool registered"
    leaked = flagged & child_names
    assert not leaked, f"author_time_only tools leaked to the subagent: {leaked}"


def test_list_models_is_flagged_author_time_only():
    # Named explicitly because it is the tool the issue calls out by name —
    # authoring which model/route to use is an orchestrator decision, not a
    # leaf's, per lohra/catalog/tool.py:5-9.
    assert "list_models" in registry.author_time_only_names()
