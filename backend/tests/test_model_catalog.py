"""The live model catalog (model-routing, fatia B).

Every test here is hermetic: HTTP goes through an injected ``httpx.MockTransport``
and the Ollama probe is injected too. The autouse guard in conftest raises if any
test forgets and lets the catalog reach for a real network client.
"""

from __future__ import annotations

import httpx
import pytest

from lohra.catalog import catalog as cat
from lohra.catalog.catalog import build_catalog
from lohra.onboarding.detect import OllamaStatus

ALL_PROVIDERS = (
    "anthropic",
    "openai",
    "openrouter",
    "deepseek",
    "groq",
    "together",
    "gemini",
    "xai",
    "glm",
    "kimi",
    "ollama",
)

REMOTE = tuple(name for name in ALL_PROVIDERS if name != "ollama")

# Deliberately distinctive so a leak into a detail string is unmistakable.
KEYS = {
    "ANTHROPIC_API_KEY": "sk-ant-CANARY0",
    "OPENAI_API_KEY": "sk-oai-CANARY1",
    "OPENROUTER_API_KEY": "sk-or-CANARY2",
    "DEEPSEEK_API_KEY": "sk-ds-CANARY3",
    "GROQ_API_KEY": "gsk-CANARY4",
    "TOGETHER_API_KEY": "tg-CANARY5",
    "GEMINI_API_KEY": "gm-CANARY6",
    "XAI_API_KEY": "xa-CANARY7",
    "ZHIPUAI_API_KEY": "zp-CANARY8",
    "MOONSHOT_API_KEY": "mk-CANARY9",
}

EXPECTED_URLS = {
    "https://api.anthropic.com/v1/models?limit=1000",
    "https://api.openai.com/v1/models",
    "https://openrouter.ai/api/v1/models",
    "https://api.deepseek.com/models",
    "https://api.groq.com/openai/v1/models",
    "https://api.together.xyz/v1/models",
    "https://generativelanguage.googleapis.com/v1beta/openai/models",
    "https://api.x.ai/v1/models",
    "https://api.z.ai/api/paas/v4/models",
    "https://api.moonshot.ai/v1/models",
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})


def _dead_probe() -> OllamaStatus:
    return OllamaStatus(alive=False, url="http://localhost:11434/api/tags", detail="ConnectError")


def _catalog(handler=_ok, *, env=None, probe=_dead_probe, **kwargs):
    return build_catalog(
        env=KEYS if env is None else env,
        client=_client(handler),
        ollama_probe=probe,
        **kwargs,
    )


def _by_name(catalog):
    return {entry.provider: entry for entry in catalog.entries}


# --- endpoints ---------------------------------------------------------------


def test_every_provider_gets_its_documented_models_url():
    seen: set[str] = set()

    def handler(request):
        seen.add(str(request.url))
        return _ok(request)

    _catalog(handler, providers=ALL_PROVIDERS)
    assert seen == EXPECTED_URLS


def test_gemini_trailing_slash_base_url_does_not_double_up():
    # base_url ends in "/" — the join must not produce "…openai//models".
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return _ok(request)

    _catalog(handler, providers=("gemini",))
    assert seen == ["https://generativelanguage.googleapis.com/v1beta/openai/models"]


def test_anthropic_sends_its_two_headers_and_no_bearer():
    captured: dict[str, httpx.Headers] = {}

    def handler(request):
        captured[str(request.url)] = request.headers
        return _ok(request)

    _catalog(handler, providers=("anthropic",))
    headers = captured["https://api.anthropic.com/v1/models?limit=1000"]
    assert headers["x-api-key"] == KEYS["ANTHROPIC_API_KEY"]
    assert headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in headers


def test_openai_compatible_providers_send_a_bearer():
    captured: dict[str, httpx.Headers] = {}

    def handler(request):
        captured[str(request.url)] = request.headers
        return _ok(request)

    _catalog(handler, providers=("openai", "groq", "gemini"))
    for headers in captured.values():
        assert headers["authorization"].startswith("Bearer ")
        assert "x-api-key" not in headers


# --- no key: skipped, never a network call -----------------------------------


def test_a_provider_without_a_key_is_skipped_and_names_the_env_var():
    def handler(request):  # pragma: no cover - must never run
        raise AssertionError(f"no key configured, yet {request.url} was fetched")

    catalog = _catalog(handler, env={}, providers=REMOTE)
    entries = _by_name(catalog)
    for name in REMOTE:
        assert entries[name].source == "skipped"
        assert entries[name].models == ()
        assert entries[name].total == 0
    assert "OPENAI_API_KEY" in entries["openai"].detail


def test_gemini_skipped_detail_names_both_of_its_env_vars():
    catalog = build_catalog(env={}, providers=("gemini",), ollama_probe=_dead_probe)
    detail = _by_name(catalog)["gemini"].detail
    assert "GEMINI_API_KEY" in detail and "GOOGLE_API_KEY" in detail


def test_no_http_client_is_created_when_nothing_is_fetchable():
    # The autouse conftest guard blows up if default_http_client is reached.
    catalog = build_catalog(env={}, providers=REMOTE, ollama_probe=_dead_probe)
    assert {e.source for e in catalog.entries} == {"skipped"}


def test_the_network_guard_actually_fires_when_a_key_is_present():
    with pytest.raises(AssertionError):
        build_catalog(env=KEYS, providers=("openai",), ollama_probe=_dead_probe)


# --- failure isolation + token-free details ----------------------------------


def test_one_provider_failing_never_takes_down_the_others():
    def handler(request):
        host = request.url.host
        if host == "api.openai.com":
            return httpx.Response(500, text="boom")
        if host == "api.deepseek.com":
            raise httpx.ConnectError("refused")
        return _ok(request)

    entries = _by_name(_catalog(handler, providers=REMOTE))
    assert entries["openai"].source == "error"
    assert entries["openai"].detail == "HTTP 500"
    assert entries["deepseek"].source == "error"
    assert entries["deepseek"].detail == "ConnectError"
    for name in ("anthropic", "openrouter", "groq", "together", "gemini"):
        assert entries[name].source == "live"
        assert entries[name].models == ("model-a", "model-b")


def test_an_error_detail_never_carries_the_key_or_the_response_body():
    secret_body = "invalid api key sk-oai-CANARY1 for org acct_12345"

    def handler(request):
        return httpx.Response(401, text=secret_body)

    catalog = _catalog(handler, providers=REMOTE)
    blob = repr(catalog.to_dict()) + repr(catalog)
    for key in KEYS.values():
        assert key not in blob
    assert "acct_12345" not in blob
    assert _by_name(catalog)["openai"].detail == "HTTP 401"


def test_a_non_json_body_degrades_to_an_error_not_a_crash():
    def handler(request):
        return httpx.Response(200, text="<html>nope</html>")

    entry = _by_name(_catalog(handler, providers=("openai",)))["openai"]
    assert entry.source == "error"
    assert entry.models == ()


def test_a_bare_list_payload_is_parsed_too():
    # Together returns a JSON array, not the {"data": [...]} envelope.
    def handler(request):
        return httpx.Response(200, json=[{"id": "meta/llama"}, {"id": "meta/llama"}])

    entry = _by_name(_catalog(handler, providers=("together",)))["together"]
    assert entry.source == "live"
    assert entry.models == ("meta/llama",)  # deduped, provider order preserved


def test_live_models_keep_the_providers_order():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "z"}, {"id": "a"}, {"id": "m"}]})

    entry = _by_name(_catalog(handler, providers=("openai",)))["openai"]
    assert entry.models == ("z", "a", "m")
    assert entry.total == 3


# --- ollama ------------------------------------------------------------------


def test_ollama_comes_from_the_injected_probe_not_from_a_models_endpoint():
    def handler(request):  # pragma: no cover - must never run
        raise AssertionError(f"ollama must use the native probe, not {request.url}")

    def probe():
        return OllamaStatus(alive=True, url="x", models=("llama3.2", "qwen3"))

    entry = _by_name(_catalog(handler, providers=("ollama",), probe=probe))["ollama"]
    assert entry.source == "live"
    assert entry.models == ("llama3.2", "qwen3")


def test_a_dead_ollama_is_an_error_with_a_short_detail():
    entry = _by_name(_catalog(providers=("ollama",)))["ollama"]
    assert entry.source == "error"
    assert entry.detail == "ConnectError"
    assert entry.models == ()


def test_a_probe_that_raises_does_not_break_the_catalog():
    def probe():
        raise RuntimeError("boom")

    entry = _by_name(_catalog(providers=("ollama",), probe=probe))["ollama"]
    assert entry.source == "error"
    assert "RuntimeError" in entry.detail


def test_ollama_needs_no_key_and_is_never_skipped():
    entry = _by_name(_catalog(env={}, providers=("ollama",)))["ollama"]
    assert entry.source == "error"  # dead, but reached — not "skipped"


# --- subscription ------------------------------------------------------------


def _activate_subscription(home, monkeypatch, tmp_path, model="gpt-5.5-test"):
    from lohra.subscription import store

    store.write_config(home, store.SubscriptionConfig("subscription", True))
    codex = tmp_path / "codex-home"
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "config.toml").write_text(f'model = "{model}"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex))


def test_an_active_subscription_adds_a_config_entry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _activate_subscription(home, monkeypatch, tmp_path)
    # No providers filter: env={} means every remote provider is skipped, so
    # this stays hermetic without naming them.
    catalog = build_catalog(env={}, home=home, ollama_probe=_dead_probe)
    entry = _by_name(catalog)["openai-codex"]
    assert entry.source == "config"
    assert entry.models == ("gpt-5.5-test",)
    assert entry.detail


def test_no_subscription_no_entry(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    catalog = build_catalog(env={}, home=home, ollama_probe=_dead_probe)
    assert "openai-codex" not in _by_name(catalog)


def test_a_provider_filter_naming_only_registry_names_drops_the_subscription(
    tmp_path, monkeypatch
):
    # "only these" means only these: openai-codex is not in the registry, so a
    # filter that does not name it excludes it like any other provider.
    home = tmp_path / "home"
    home.mkdir()
    _activate_subscription(home, monkeypatch, tmp_path)
    catalog = build_catalog(env={}, home=home, providers=("openai",), ollama_probe=_dead_probe)
    assert [e.provider for e in catalog.entries] == ["openai"]


def test_the_subscription_entry_survives_a_provider_filter_naming_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _activate_subscription(home, monkeypatch, tmp_path)
    catalog = build_catalog(
        env={}, home=home, providers=("openai-codex",), ollama_probe=_dead_probe
    )
    assert [e.provider for e in catalog.entries] == ["openai-codex"]


# --- shape / filtering -------------------------------------------------------


def test_an_alias_resolves_to_its_provider():
    catalog = build_catalog(env={}, providers=("claude",), ollama_probe=_dead_probe)
    assert [e.provider for e in catalog.entries] == ["anthropic"]


def test_head_truncates_the_ids_but_keeps_the_real_total():
    entry = cat.ProviderModels("openrouter", "live", tuple(f"m{i}" for i in range(300)))
    short = entry.head(25)
    assert len(short.models) == 25
    assert short.total == 300
    assert short.truncated is True
    assert entry.head(1000) is entry


def test_to_dict_is_json_serialisable_and_names_every_entry():
    import json

    payload = _catalog(providers=("openai", "ollama")).to_dict()
    assert json.loads(json.dumps(payload))
    assert [p["provider"] for p in payload["providers"]] == ["openai", "ollama"]


def test_entries_follow_the_registry_order():
    catalog = _catalog(providers=ALL_PROVIDERS)
    assert [e.provider for e in catalog.entries] == list(ALL_PROVIDERS)


def test_catalog_get_finds_an_entry_by_name():
    catalog = _catalog(providers=("openai", "ollama"))
    assert catalog.get("openai").source == "live"
    assert catalog.get("nope") is None


def test_a_payload_that_is_not_a_list_is_an_error_not_a_crash():
    def handler(request):
        return httpx.Response(200, json={"object": "list"})  # no "data"

    entry = _by_name(_catalog(handler, providers=("openai",)))["openai"]
    assert entry.source == "error"
    assert entry.detail == "unexpected response shape"


def test_a_live_but_empty_ollama_is_live_with_a_reason():
    def probe():
        return OllamaStatus(alive=True, url="x", models=())

    entry = _by_name(_catalog(providers=("ollama",), probe=probe))["ollama"]
    assert entry.source == "live"
    assert entry.total == 0
    assert entry.detail == "daemon alive, no models pulled"


def test_a_dead_probe_without_a_detail_still_explains_itself():
    def probe():
        return OllamaStatus(alive=False, url="x")

    assert _by_name(_catalog(providers=("ollama",), probe=probe))["ollama"].detail == "not running"


# --- client ownership ---------------------------------------------------------


def test_a_client_the_catalog_opened_is_the_one_it_closes(monkeypatch):
    opened = _client(_ok)
    monkeypatch.setattr(cat, "default_http_client", lambda timeout=3.0: opened)
    build_catalog(env=KEYS, providers=("openai",), ollama_probe=_dead_probe)
    assert opened.is_closed is True


def test_an_injected_client_is_left_open_for_its_owner():
    injected = _client(_ok)
    build_catalog(
        env=KEYS, providers=("openai",), client=injected, ollama_probe=_dead_probe
    )
    assert injected.is_closed is False
    injected.close()


# --- pagination: a bound is never silent -------------------------------------


def test_anthropic_asks_for_a_full_page_instead_of_the_default_20():
    # GET /v1/models is paginated (SDK: limit defaults to 20, ranges 1..1000).
    # Reading the default page would report 20 as if it were the whole account.
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return _ok(request)

    _catalog(handler, providers=("anthropic",))
    assert seen == [f"https://api.anthropic.com/v1/models?limit={cat.ANTHROPIC_PAGE_LIMIT}"]


def test_a_paginated_answer_says_out_loud_that_more_pages_exist():
    def handler(request):
        return httpx.Response(
            200, json={"data": [{"id": "a"}, {"id": "b"}], "has_more": True, "last_id": "b"}
        )

    entry = _by_name(_catalog(handler, providers=("anthropic",)))["anthropic"]
    assert entry.source == "live"
    assert entry.models == ("a", "b")
    assert "more" in entry.detail  # the cap is disclosed, never silent


def test_a_single_page_answer_carries_no_pagination_noise():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "a"}], "has_more": False})

    assert _by_name(_catalog(handler, providers=("anthropic",)))["anthropic"].detail is None


# --- reachable-but-empty is not a failure ------------------------------------


def test_a_provider_that_answers_200_with_an_empty_list_is_live_not_broken():
    # Same fact as "ollama alive with nothing pulled": reachable, zero models.
    def handler(request):
        return httpx.Response(200, json={"data": []})

    entry = _by_name(_catalog(handler, providers=("openai",)))["openai"]
    assert entry.source == "live"
    assert entry.total == 0
    assert entry.detail == "reachable, no models listed"


# --- keyless providers -------------------------------------------------------


def test_a_keyless_provider_is_fetched_without_auth_instead_of_skipped(monkeypatch):
    # Discriminated by ``requires_api_key``, not by the name "ollama": a future
    # local endpoint (lmstudio, vllm) must not be reported as missing a key.
    from lohra.providers import base

    profile = base.ProviderProfile(
        name="fakelocal",
        base_url="http://localhost:1234/v1",
        env_vars=("FAKELOCAL_API_KEY",),
        requires_api_key=False,
    )
    # setitem, never register_provider: the registry is process-global and has no
    # unregister — a leaked profile would make later tests reach for the network.
    monkeypatch.setitem(base._REGISTRY, "fakelocal", profile)
    seen: dict[str, httpx.Headers] = {}

    def handler(request):
        seen[str(request.url)] = request.headers
        return _ok(request)

    entry = _by_name(_catalog(handler, env={}, providers=("fakelocal",)))["fakelocal"]
    assert entry.source == "live"
    headers = seen["http://localhost:1234/v1/models"]
    assert "authorization" not in headers


# --- subscription ------------------------------------------------------------


def test_naming_the_subscription_provider_while_it_is_off_explains_itself(tmp_path):
    # "unknown provider" would be a lie: it is known, just not opted into.
    home = tmp_path / "home"
    home.mkdir()
    entry = _by_name(
        build_catalog(
            env={}, home=home, providers=("openai-codex",), ollama_probe=_dead_probe
        )
    )["openai-codex"]
    assert entry.source == "skipped"
    assert entry.models == ()
    assert "lohra auth enable" in entry.detail


def test_naming_it_case_insensitively_still_resolves(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    catalog = build_catalog(
        env={}, home=home, providers=("OpenAI-Codex",), ollama_probe=_dead_probe
    )
    assert [e.provider for e in catalog.entries] == ["openai-codex"]


def test_a_broken_subscription_store_reports_an_error_instead_of_vanishing(
    tmp_path, monkeypatch
):
    from lohra.subscription import credentials

    def boom(_home):
        raise OSError("auth.json is unreadable")

    monkeypatch.setattr(credentials, "subscription_active", boom)
    home = tmp_path / "home"
    home.mkdir()
    entry = _by_name(build_catalog(env={}, home=home, ollama_probe=_dead_probe))["openai-codex"]
    assert entry.source == "error"
    assert entry.detail == "OSError"


# --- transport: timeout + parallelism ----------------------------------------


def test_the_per_provider_timeout_reaches_the_transport(monkeypatch):
    captured: dict[str, float] = {}

    def recorder(timeout=cat.DEFAULT_TIMEOUT):
        captured["timeout"] = timeout
        return _client(_ok)

    monkeypatch.setattr(cat, "default_http_client", recorder)
    build_catalog(env=KEYS, providers=("openai",), timeout=7.5, ollama_probe=_dead_probe)
    assert captured["timeout"] == 7.5


def test_the_real_transport_is_built_with_that_timeout(_no_real_model_catalog_fetch):
    # The fixture hands back the REAL factory (see conftest). Constructing a
    # client opens no socket — this asserts the timeout reaches the transport,
    # which the injected-client tests above can never see.
    client = _no_real_model_catalog_fetch(2.5)
    try:
        assert client.timeout.read == 2.5
        assert client.follow_redirects is False
    finally:
        client.close()


def test_every_fetchable_provider_flies_at_the_same_time():
    # A barrier that only clears when all N requests are in flight: a sequential
    # implementation breaks it, and each provider degrades to an "error" entry.
    import threading

    names = ("openai", "groq", "together")
    barrier = threading.Barrier(len(names), timeout=5)

    def handler(request):
        barrier.wait()
        return _ok(request)

    entries = _by_name(_catalog(handler, providers=names))
    assert {entries[name].source for name in names} == {"live"}


# --- byte cap ----------------------------------------------------------------


def test_an_absurdly_large_body_is_refused_instead_of_parsed(monkeypatch):
    monkeypatch.setattr(cat, "MAX_RESPONSE_BYTES", 10)

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "a-very-long-model-id"}]})

    entry = _by_name(_catalog(handler, providers=("openai",)))["openai"]
    assert entry.source == "error"
    assert "too large" in entry.detail


# --- source vocabulary --------------------------------------------------------


def test_every_source_the_catalog_emits_belongs_to_the_declared_vocabulary(tmp_path):
    def handler(request):
        if request.url.host == "api.openai.com":
            return httpx.Response(500)
        return _ok(request)

    home = tmp_path / "home"
    home.mkdir()
    catalog = _catalog(handler, env=dict(KEYS, GROQ_API_KEY=""), home=home)
    assert {e.source for e in catalog.entries} <= set(cat.SOURCES)


def test_a_source_outside_the_vocabulary_is_rejected_at_construction():
    with pytest.raises(ValueError):
        cat.ProviderModels("openai", "skiped")  # typo must not reach --json


# --- one seam, one name -------------------------------------------------------


def test_the_network_seam_has_exactly_one_binding_to_neutralize():
    # conftest rebinds ``catalog.default_http_client``. A package-level re-export
    # would be a SECOND binding the guard does not cover — an importer using it
    # would reach the real network inside a "hermetic" test.
    import lohra.catalog as package

    assert not hasattr(package, "default_http_client")


def test_models_json_emits_an_envelope_even_on_a_bad_profile(monkeypatch, capsys):
    # The --json contract (stdout = exactly one JSON object) must hold for the
    # pre-dispatch profile validation too, not just for run_models.
    import json

    from lohra import cli

    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    rc = cli.main(["models", "--json", "--profile", "../bad"])
    out = capsys.readouterr()
    assert rc == 2
    payload = json.loads(out.out)  # exactly one parseable object
    assert payload["providers"] == [] and payload["error"]


def test_a_close_that_raises_never_sinks_the_entries(monkeypatch):
    # "Never raises once the client exists" includes the teardown: a client
    # whose close() blows up must not discard the fetched entries.
    import httpx

    from lohra.catalog import catalog as cat

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    class ExplodingClient(httpx.Client):
        def close(self):
            raise RuntimeError("boom")

    exploding = ExplodingClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cat, "default_http_client", lambda timeout: exploding)
    result = cat.build_catalog(
        env={"ANTHROPIC_API_KEY": "k"}, home=None, providers=("anthropic",)
    )
    entry = {e.provider: e for e in result.entries}["anthropic"]
    assert entry.source == "live" and entry.models == ("m1",)


def test_json_envelope_on_bad_profile_covers_chat_and_doctor(monkeypatch, capsys):
    # Sol's finding: the pre-dispatch profile error emitted an envelope only
    # for `models --json` — chat/doctor broke the one-JSON-object contract.
    import json

    from lohra import cli

    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    for argv in (
        ["chat", "oi", "--json", "--profile", "../bad"],
        ["doctor", "--json", "--profile", "../bad"],
    ):
        rc = cli.main(argv)
        out = capsys.readouterr()
        assert rc == 2
        payload = json.loads(out.out)
        assert payload["error"]


def test_fetch_requests_identity_encoding(monkeypatch):
    # Sol's finding: iter_bytes() hands DEcompressed chunks, so a gzip bomb
    # inflates in memory before the 4MB check. Asking for identity encoding
    # makes the cap bound what actually crosses the wire.
    import httpx

    from lohra.catalog import catalog as cat
    from lohra.providers import get_provider_profile

    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cat.fetch_models(get_provider_profile("openai"), api_key="k", client=client)
    client.close()
    assert seen.get("accept-encoding") == "identity"
