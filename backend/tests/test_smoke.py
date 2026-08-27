"""Smoke tests for the Phase 0 scaffold: the core contracts exist and behave.

These pin the invariants the rest of the build depends on. More coverage lands
per phase (see docs/ROADMAP.md); TDD: tests first, 80%+ coverage.
"""

import json

from lohra import __version__
from lohra.agent.types import NormalizedResponse, ToolCall, map_finish_reason
from lohra.providers.base import ProviderProfile, get_provider_profile, register_provider
from lohra.tools.registry import ToolRegistry, tool_error


def test_version_exposed():
    assert isinstance(__version__, str) and __version__


def test_normalized_response_is_immutable():
    resp = NormalizedResponse(content="hi", finish_reason="stop")
    assert resp.tool_calls == ()
    try:
        resp.content = "mutated"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("NormalizedResponse must be frozen")


def test_tool_call_carries_provider_data():
    tc = ToolCall(id="call_1", name="read_file", arguments='{"path":"x"}')
    assert json.loads(tc.arguments)["path"] == "x"
    assert tc.provider_data is None


def test_map_finish_reason_defaults_to_stop():
    assert map_finish_reason("end_turn", {"end_turn": "stop"}) == "stop"
    assert map_finish_reason("tool_use", {"tool_use": "tool_calls"}) == "tool_calls"
    assert map_finish_reason(None, {}) == "stop"
    assert map_finish_reason("weird", {}) == "stop"


def test_provider_registry_resolves_name_and_alias():
    register_provider(
        ProviderProfile(name="acme", aliases=("acme-fast",), base_url="https://api.acme.test")
    )
    assert get_provider_profile("acme") is not None
    assert get_provider_profile("acme-fast").name == "acme"
    assert get_provider_profile("unknown") is None


def test_provider_hostname_derived_from_base_url():
    p = ProviderProfile(name="x", base_url="https://api.example.com/v1")
    assert p.get_hostname() == "api.example.com"


def test_tool_registry_rejects_cross_toolset_shadowing():
    reg = ToolRegistry()
    schema = {"description": "d", "parameters": {"type": "object"}}
    reg.register("t", "file", schema, lambda args: "{}")
    gen_after_first = reg.generation
    try:
        reg.register("t", "web", schema, lambda args: "{}")
    except ValueError:
        pass
    else:
        raise AssertionError("shadowing across toolsets must be rejected")
    # override=True is allowed and bumps the generation
    reg.register("t", "web", schema, lambda args: "{}", override=True)
    assert reg.generation > gen_after_first


def test_tool_registry_dispatch_wraps_errors():
    reg = ToolRegistry()

    def boom(args):
        raise RuntimeError("kaboom")

    reg.register("boom", "test", {"description": "", "parameters": {}}, boom)
    out = json.loads(reg.dispatch("boom", {}))
    assert "error" in out and "kaboom" in out["error"]
    assert json.loads(reg.dispatch("missing", {}))["error"].startswith("Unknown tool")


def test_tool_definitions_use_openai_function_shape():
    reg = ToolRegistry()
    reg.register("rf", "file", {"description": "read", "parameters": {"type": "object"}}, lambda a: "{}")
    defs = reg.get_definitions()
    assert defs[0]["type"] == "function"
    assert defs[0]["function"]["name"] == "rf"


def test_tool_error_is_json():
    assert json.loads(tool_error("nope", code=1)) == {"error": "nope", "code": 1}
