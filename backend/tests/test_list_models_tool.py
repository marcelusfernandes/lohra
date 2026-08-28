"""The ``list_models`` tool + the ``lohra models`` command (model-routing, fatia B).

The tool is exercised against an injected catalog builder (no HTTP at all); one
test wires the real builder through an httpx MockTransport to prove the seam.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lohra.catalog.catalog import Catalog, ProviderModels
from lohra.catalog.tool import DEFAULT_LIMIT, MAX_LIMIT, ListModelsTool
from lohra.onboarding.detect import OllamaStatus


def _builder(catalog: Catalog, seen: dict | None = None):
    def build(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return catalog

    return build


def _big_openrouter(n: int = 300) -> Catalog:
    return Catalog(
        (
            ProviderModels("openrouter", "live", tuple(f"vendor/model-{i}" for i in range(n))),
            ProviderModels("openai", "skipped", detail="no API key — set OPENAI_API_KEY"),
        )
    )


def _tool(tmp_path, catalog):
    return ListModelsTool(tmp_path, builder=_builder(catalog))


def _call(tool, **args):
    return json.loads(tool.handle(args))


# --- bounded output ----------------------------------------------------------


def test_a_huge_provider_is_bounded_and_says_so_out_loud(tmp_path):
    out = _call(_tool(tmp_path, _big_openrouter()))
    entry = next(p for p in out["providers"] if p["provider"] == "openrouter")
    assert len(entry["models"]) == DEFAULT_LIMIT
    assert entry["total"] == 300
    note = entry["note"]
    assert "300" in note and "25" in note and "query" in note


def test_a_small_provider_carries_no_truncation_note(tmp_path):
    catalog = Catalog((ProviderModels("openai", "live", ("gpt-4o", "gpt-4o-mini")),))
    entry = _call(_tool(tmp_path, catalog))["providers"][0]
    assert entry["models"] == ["gpt-4o", "gpt-4o-mini"]
    assert entry["total"] == 2
    assert "note" not in entry


def test_limit_is_clamped_never_rejected(tmp_path):
    tool = _tool(tmp_path, _big_openrouter())
    assert len(_call(tool, limit=9999)["providers"][0]["models"]) == MAX_LIMIT
    assert len(_call(tool, limit=0)["providers"][0]["models"]) == 1
    assert len(_call(tool, limit=-5)["providers"][0]["models"]) == 1
    assert len(_call(tool, limit="nope")["providers"][0]["models"]) == DEFAULT_LIMIT


def test_query_filters_case_insensitively_and_retotals(tmp_path):
    catalog = Catalog((ProviderModels("openai", "live", ("GPT-4o", "o3-mini", "gpt-4o-mini")),))
    entry = _call(_tool(tmp_path, catalog), query="gpt")["providers"][0]
    assert entry["models"] == ["GPT-4o", "gpt-4o-mini"]
    assert entry["total"] == 2


def test_query_with_no_hit_returns_an_empty_but_honest_entry(tmp_path):
    catalog = Catalog((ProviderModels("openai", "live", ("gpt-4o",)),))
    entry = _call(_tool(tmp_path, catalog), query="zzz")["providers"][0]
    assert entry["models"] == []
    assert entry["total"] == 0


def test_provider_is_pushed_down_to_the_builder_not_filtered_after(tmp_path):
    seen: dict = {}
    catalog = Catalog((ProviderModels("groq", "live", ("llama-3.3-70b-versatile",)),))
    tool = ListModelsTool(tmp_path, builder=_builder(catalog, seen))
    out = _call(tool, provider="groq")
    assert seen["providers"] == ("groq",)
    assert [p["provider"] for p in out["providers"]] == ["groq"]


def test_an_unknown_provider_is_a_clean_tool_error(tmp_path):
    tool = ListModelsTool(tmp_path, builder=_builder(Catalog(())))
    out = _call(tool, provider="nope")
    assert "error" in out and "nope" in out["error"]


def test_a_skipped_provider_keeps_its_detail(tmp_path):
    entry = next(
        p
        for p in _call(_tool(tmp_path, _big_openrouter()))["providers"]
        if p["provider"] == "openai"
    )
    assert entry["source"] == "skipped"
    assert "OPENAI_API_KEY" in entry["detail"]


def test_a_builder_that_explodes_becomes_a_tool_error_not_a_crash(tmp_path):
    def boom(**_kwargs):
        raise RuntimeError("nope")

    out = _call(ListModelsTool(tmp_path, builder=boom))
    assert "error" in out and "RuntimeError" in out["error"]


# --- tiers -------------------------------------------------------------------


def test_the_operator_tier_map_rides_along(tmp_path):
    (tmp_path / "workflow_tiers.json").write_text(
        json.dumps({"big": {"model": "claude-opus-4-8", "effort": "high"}, "small": "haiku"}),
        encoding="utf-8",
    )
    tiers = _call(_tool(tmp_path, Catalog(())))["tiers"]
    assert tiers["big"] == {"model": "claude-opus-4-8", "effort": "high"}
    assert tiers["small"] == {"model": "haiku"}
    assert tiers["medium"] is None


def test_tiers_are_reloaded_fresh_on_every_call(tmp_path):
    path = tmp_path / "workflow_tiers.json"
    tool = _tool(tmp_path, Catalog(()))
    assert _call(tool)["tiers"]["big"] is None
    path.write_text(json.dumps({"big": "claude-opus-4-8"}), encoding="utf-8")
    assert _call(tool)["tiers"]["big"] == {"model": "claude-opus-4-8"}


def test_a_missing_tier_file_is_not_an_error(tmp_path):
    out = _call(_tool(tmp_path, Catalog(())))
    assert out["ok"] is True
    assert out["tiers"] == {"small": None, "medium": None, "big": None}


# --- the real seam -----------------------------------------------------------


def test_the_default_builder_is_the_real_catalog(tmp_path, monkeypatch):
    from lohra.catalog import tool as tool_mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-CANARY")

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/models"
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    real = tool_mod.build_catalog

    def build(**kwargs):
        return real(
            client=client,
            ollama_probe=lambda: OllamaStatus(alive=False, url="x", detail="off"),
            **kwargs,
        )

    entry = _call(ListModelsTool(tmp_path, builder=build), provider="openai")["providers"][0]
    assert entry == {"provider": "openai", "source": "live", "total": 1, "models": ["gpt-4o"]}


# --- registration / exclusion ------------------------------------------------


def test_schema_registered_and_intercepted_fallback():
    from lohra.catalog.tool import register_list_models_tool_schema
    from lohra.tools import registry

    register_list_models_tool_schema()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "list_models" in names
    assert "error" in json.loads(registry.dispatch("list_models", {}))


def test_list_models_is_excluded_from_subagents():
    from lohra.agent.delegate import _CHILD_EXCLUDED_TOOLS, child_tool_definitions

    assert "list_models" in _CHILD_EXCLUDED_TOOLS
    definitions = ({"function": {"name": "list_models"}}, {"function": {"name": "read_file"}})
    assert child_tool_definitions(definitions) == ({"function": {"name": "read_file"}},)


def test_the_subagent_dispatch_refuses_it_at_runtime():
    from lohra.agent.delegate import subagent_dispatch

    dispatch = subagent_dispatch(lambda name, args: json.dumps({"ok": True}))
    assert "error" in json.loads(dispatch("list_models", {}))


def test_register_all_tools_includes_it():
    from lohra.agent.equip import register_all_tools
    from lohra.tools import registry

    register_all_tools()
    assert "list_models" in {d["function"]["name"] for d in registry.get_definitions()}


def test_build_session_dispatch_binds_it_when_a_home_is_given(tmp_path):
    from lohra.agent.equip import build_session_dispatch, build_session_stores, register_all_tools

    register_all_tools()
    memory, skills = build_session_stores(tmp_path)
    dispatch = build_session_dispatch(memory, skills, home=tmp_path)
    out = json.loads(dispatch("list_models", {"provider": "ollama"}))
    assert out["ok"] is True
    assert [p["provider"] for p in out["providers"]] == ["ollama"]


# --- CLI ---------------------------------------------------------------------


@pytest.fixture
def virgin(monkeypatch, tmp_path):
    """No provider key anywhere: every remote provider is skipped, ollama is dead
    (the conftest probe), and no HTTP client is ever built."""
    from lohra.providers.base import list_providers

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    for profile in list_providers():
        for var in profile.env_vars:
            monkeypatch.delenv(var, raising=False)
    for var in ("LOHRA_PROFILE", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_cli_models_prints_a_line_per_provider(virgin, capsys):
    from lohra import cli

    assert cli.main(["models"]) == 0
    out = capsys.readouterr().out
    assert "openai" in out and "skipped" in out
    assert "OPENAI_API_KEY" in out


def test_cli_models_json_is_exactly_one_object(virgin, capsys):
    from lohra import cli

    assert cli.main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert {p["provider"] for p in payload["providers"]} >= {"openai", "ollama"}


def test_cli_models_provider_filter(virgin, capsys):
    from lohra import cli

    assert cli.main(["models", "--provider", "ollama", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [p["provider"] for p in payload["providers"]] == ["ollama"]


def test_cli_models_rejects_an_unknown_provider(virgin, capsys):
    from lohra import cli

    assert cli.main(["models", "--provider", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


# --- loudness: the network guard must not be swallowed -----------------------


def test_an_assertion_from_the_network_guard_is_re_raised_not_downgraded(tmp_path):
    # The conftest guard raises AssertionError when a test forgets to inject a
    # transport. A broad ``except Exception`` here would turn that into a tidy
    # tool_error and a GREEN run — the exact failure the guard exists to prevent.
    def guard(**_kwargs):
        raise AssertionError("the model catalog tried to open a real HTTP client")

    with pytest.raises(AssertionError):
        ListModelsTool(tmp_path, builder=guard).handle({})


# --- known-but-unavailable is not unknown ------------------------------------


def test_a_known_provider_that_is_merely_off_is_reported_not_denied(tmp_path):
    catalog = Catalog(
        (ProviderModels("openai-codex", "skipped", detail="subscription mode is off — …"),)
    )
    out = _call(_tool(tmp_path, catalog), provider="openai-codex")
    assert "error" not in out
    assert out["providers"][0]["source"] == "skipped"


# --- the filter is never silent either ---------------------------------------


def test_a_query_that_matches_nothing_still_reports_what_it_filtered(tmp_path):
    catalog = Catalog((ProviderModels("openrouter", "live", tuple(f"m{i}" for i in range(42))),))
    entry = _call(_tool(tmp_path, catalog), query="zzz")["providers"][0]
    assert entry["models"] == []
    assert entry["total"] == 0
    assert "42" in entry["note"]  # zero hits vs. an empty provider are different facts


def test_a_query_that_matches_carries_the_pre_filter_total_too(tmp_path):
    catalog = Catalog((ProviderModels("openai", "live", ("gpt-4o", "o3-mini")),))
    entry = _call(_tool(tmp_path, catalog), query="gpt")["providers"][0]
    assert entry["total"] == 1
    assert "2" in entry["note"]


# --- limit: coerced when coercible, disclosed when not -----------------------


def test_a_numeric_string_or_float_limit_is_honoured(tmp_path):
    tool = _tool(tmp_path, _big_openrouter())
    assert len(_call(tool, limit="50")["providers"][0]["models"]) == 50
    assert len(_call(tool, limit=50.0)["providers"][0]["models"]) == 50


def test_an_unusable_limit_falls_back_out_loud(tmp_path):
    tool = _tool(tmp_path, _big_openrouter())
    out = _call(tool, limit="nope")
    assert len(out["providers"][0]["models"]) == DEFAULT_LIMIT
    assert "nope" in out["note"] and str(DEFAULT_LIMIT) in out["note"]
    # bool is an int subclass: True must not silently mean "one model".
    boolean = _call(tool, limit=True)
    assert len(boolean["providers"][0]["models"]) == DEFAULT_LIMIT
    assert "True" in boolean["note"]


def test_a_plain_limit_adds_no_noise(tmp_path):
    assert "note" not in _call(_tool(tmp_path, _big_openrouter()), limit=10)


# --- CLI: the render an operator with keys actually sees ---------------------


def _stub_catalog(monkeypatch, catalog=None, *, boom=None):
    from lohra.catalog import catalog as cat_mod

    def build(**_kwargs):
        if boom is not None:
            raise boom
        return catalog

    # run_models imports build_catalog per call, so the module attribute is the seam.
    monkeypatch.setattr(cat_mod, "build_catalog", build)


def test_cli_models_prints_every_model_under_its_provider(virgin, capsys, monkeypatch):
    from lohra import cli

    _stub_catalog(
        monkeypatch,
        Catalog(
            (
                ProviderModels("openai", "live", ("gpt-4o", "gpt-4o-mini")),
                ProviderModels("ollama", "error", detail="ConnectError"),
            )
        ),
    )
    assert cli.main(["models"]) == 0
    out = capsys.readouterr().out
    assert "openai [live] 2 model(s)" in out
    assert "  gpt-4o\n" in out and "  gpt-4o-mini\n" in out
    assert "ollama [error] — ConnectError" in out
    assert "2 model(s) reachable across 2 provider(s)" in out


def test_cli_models_provider_flag_is_case_insensitive_for_the_subscription(virgin, capsys):
    from lohra import cli

    # get_provider_profile lowercases; the subscription comparison must too.
    assert cli.main(["models", "--provider", "OpenAI-Codex"]) == 0
    assert "openai-codex" in capsys.readouterr().out


def test_cli_models_json_stays_one_json_object_even_when_it_refuses(virgin, capsys):
    from lohra import cli

    assert cli.main(["models", "--provider", "nope", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "nope" in payload["error"]
    assert payload["providers"] == []


def test_cli_models_degrades_instead_of_dumping_a_traceback(virgin, capsys, monkeypatch):
    from lohra import cli

    _stub_catalog(monkeypatch, boom=RuntimeError("httpx is missing"))
    assert cli.main(["models"]) == 1
    assert "RuntimeError" in capsys.readouterr().err


def test_cli_models_still_lets_the_network_guard_through(virgin, monkeypatch):
    from lohra import cli

    _stub_catalog(monkeypatch, boom=AssertionError("real HTTP client"))
    with pytest.raises(AssertionError):
        cli.main(["models"])
