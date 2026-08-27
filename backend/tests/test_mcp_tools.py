"""Tests for the MCP tool-registration core (spec §8).

Pure logic — no SDK, no network. A fake ``call_tool`` stands in for a live MCP
session so we can pin schema conversion, naming, result wrapping, registration
into the registry, and nuke-and-repave deregistration.
"""

import json

import pytest

from lohra.mcp.tools import (
    convert_mcp_schema,
    deregister_server,
    mcp_tool_name,
    register_server_tools,
    wrap_call_result,
)
from lohra.tools.registry import ToolRegistry


# --- naming ---


def test_tool_name_prefixes_server_and_tool():
    assert mcp_tool_name("github", "create_issue") == "mcp_github_create_issue"


def test_tool_name_sanitizes_separators_and_case():
    assert mcp_tool_name("My-Server", "do.thing") == "mcp_my_server_do_thing"


def test_tool_name_collapses_invalid_runs():
    assert mcp_tool_name("a b/c", "x") == "mcp_a_b_c_x"


# --- schema conversion ---


def test_convert_schema_maps_input_schema_to_parameters():
    tool = {
        "name": "search",
        "description": "Search the web",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    schema = convert_mcp_schema(tool)
    assert schema["description"] == "Search the web"
    assert schema["parameters"] == {"type": "object", "properties": {"q": {"type": "string"}}}


def test_convert_schema_defaults_empty_parameters():
    schema = convert_mcp_schema({"name": "t"})
    assert schema["parameters"] == {"type": "object", "properties": {}}
    assert schema["description"] == ""


def test_convert_schema_rejects_non_dict_input_schema():
    # a server returning a bogus (non-dict) inputSchema must not poison the array
    schema = convert_mcp_schema({"name": "t", "inputSchema": "not-a-dict"})
    assert schema["parameters"] == {"type": "object", "properties": {}}


# --- result wrapping ---


def test_wrap_text_content_blocks():
    result = {"content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]}
    out = json.loads(wrap_call_result(result))
    assert out["ok"] is True
    assert out["content"] == "hello world"


def test_wrap_error_result():
    result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    out = json.loads(wrap_call_result(result))
    assert "error" in out
    assert "boom" in out["error"]


def test_wrap_non_text_blocks_are_summarized():
    result = {"content": [{"type": "image", "data": "..."}]}
    out = json.loads(wrap_call_result(result))
    assert out["ok"] is True
    assert "image" in out["content"]  # a placeholder, not the raw bytes


def test_wrap_reads_attribute_style_result():
    class Block:
        type = "text"
        text = "hi"

    class Result:
        content = [Block()]
        isError = False

    out = json.loads(wrap_call_result(Result()))
    assert out["content"] == "hi"


# --- registration into the registry ---


@pytest.fixture
def registry():
    return ToolRegistry()


def _tools():
    return [
        {"name": "create_issue", "description": "Open an issue", "inputSchema": {"type": "object"}},
        {"name": "list_issues", "description": "List issues", "inputSchema": {"type": "object"}},
    ]


def test_register_adds_prefixed_tools_under_mcp_toolset(registry):
    names = register_server_tools(registry, "github", _tools(), call_tool=lambda n, a: {"content": []})
    assert set(names) == {"mcp_github_create_issue", "mcp_github_list_issues"}
    defs = {d["function"]["name"] for d in registry.get_definitions()}
    assert {"mcp_github_create_issue", "mcp_github_list_issues"} <= defs
    assert set(registry.names_in_toolset("mcp-github")) == set(names)


def test_registered_handler_dispatches_to_call_tool(registry):
    captured = {}

    def call_tool(name, args):
        captured["name"] = name
        captured["args"] = args
        return {"content": [{"type": "text", "text": "done"}]}

    register_server_tools(registry, "github", _tools(), call_tool=call_tool)
    out = json.loads(registry.dispatch("mcp_github_create_issue", {"title": "bug"}))
    # the handler must call the ORIGINAL (unprefixed) tool name
    assert captured["name"] == "create_issue"
    assert captured["args"] == {"title": "bug"}
    assert out["content"] == "done"


def test_call_tool_exception_becomes_error_envelope(registry):
    def call_tool(name, args):
        raise RuntimeError("server down")

    register_server_tools(registry, "github", _tools(), call_tool=call_tool)
    out = json.loads(registry.dispatch("mcp_github_create_issue", {}))
    assert "error" in out


def test_register_skips_nameless_tool(registry):
    names = register_server_tools(
        registry, "s", [{"inputSchema": {}}, {"name": "ok", "inputSchema": {}}],
        call_tool=lambda n, a: {"content": []},
    )
    assert names == ["mcp_s_ok"]


def test_register_keeps_first_of_slug_collision(registry):
    # two distinct tool names that sanitize to the same registry name
    names = register_server_tools(
        registry,
        "s",
        [{"name": "get-user", "inputSchema": {}}, {"name": "get_user", "inputSchema": {}}],
        call_tool=lambda n, a: {"content": []},
    )
    assert names == ["mcp_s_get_user"]  # first wins, second skipped
    assert len(registry.names_in_toolset("mcp-s")) == 1


def test_register_skips_collision_with_builtin(registry):
    # a non-mcp tool already owns the prefixed name -> skip it, don't raise
    registry.register("mcp_x_t", "builtin", {"description": "", "parameters": {}}, lambda a, **k: "{}")
    names = register_server_tools(
        registry, "x", [{"name": "t", "inputSchema": {}}], call_tool=lambda n, a: {"content": []}
    )
    assert names == []  # the collision was skipped
    assert registry._entries["mcp_x_t"].toolset == "builtin"  # original untouched


# --- nuke-and-repave ---


def test_deregister_server_removes_only_its_tools(registry):
    register_server_tools(registry, "github", _tools(), call_tool=lambda n, a: {"content": []})
    register_server_tools(
        registry, "files", [{"name": "read", "inputSchema": {}}], call_tool=lambda n, a: {"content": []}
    )
    deregister_server(registry, "github")
    remaining = {d["function"]["name"] for d in registry.get_definitions()}
    assert remaining == {"mcp_files_read"}


def test_reregister_after_deregister_refreshes(registry):
    register_server_tools(registry, "s", [{"name": "old", "inputSchema": {}}], call_tool=lambda n, a: {})
    deregister_server(registry, "s")
    register_server_tools(registry, "s", [{"name": "new", "inputSchema": {}}], call_tool=lambda n, a: {})
    names = set(registry.names_in_toolset("mcp-s"))
    assert names == {"mcp_s_new"}
