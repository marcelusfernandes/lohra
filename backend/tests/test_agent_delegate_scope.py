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

``registry.author_time_only_names()`` is metadata, not the mechanism:
``_CHILD_EXCLUDED_TOOLS`` in agent/delegate.py still drives what a child
actually receives (unchanged, per the issue's "fora de escopo"). The tests
below only verify the flag stays a faithful, checkable mirror of that
denylist — both directions, so drift in either one fails the suite.
"""

from __future__ import annotations

import pytest

from lohra.agent.delegate import _CHILD_EXCLUDED_TOOLS, child_tool_definitions
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
    """Rule test (one direction): the ``author_time_only`` registry flag is
    checked against the child's definitions. A tool registered with the flag
    set but missing from ``_CHILD_EXCLUDED_TOOLS`` is a doctrine violation
    caught here instead of silently leaking to a child. This alone does not
    make the flag authoritative — see the equality test below for that.
    """
    parent_definitions = tuple(registry.get_definitions())
    child_definitions = child_tool_definitions(parent_definitions)
    child_names = {d["function"]["name"] for d in child_definitions}

    flagged = registry.author_time_only_names()
    assert flagged, "expected at least one author_time_only tool registered"
    leaked = flagged & child_names
    assert not leaked, f"author_time_only tools leaked to the subagent: {leaked}"


def test_author_time_only_flag_matches_the_denylist_exactly():
    """Rule test (other direction): every excluded tool must ALSO carry the
    flag, and vice versa. Without this, a tool could be added to
    ``_CHILD_EXCLUDED_TOOLS`` (correctly excluded at runtime) while never
    being flagged — the flag would then silently stop being a faithful
    picture of what the denylist actually does, and a future rename or
    refactor of the denylist could drift from it undetected.
    """
    flagged = registry.author_time_only_names()
    # delegate_task itself is excluded for the depth guard (MAX_DEPTH=1, no
    # grandchildren) as well as being intercepted/author-time; every other
    # name in the denylist is excluded because it needs parent-bound state.
    assert flagged == _CHILD_EXCLUDED_TOOLS


def test_list_models_is_flagged_author_time_only():
    # Named explicitly because it is the tool the issue calls out by name —
    # authoring which model/route to use is an orchestrator decision, not a
    # leaf's, per lohra/catalog/tool.py:5-9.
    assert "list_models" in registry.author_time_only_names()
