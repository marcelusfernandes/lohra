"""A model that does not exist is SUBSTITUTED from the catalog, never silently
(W9-E8, issue #85; the owner's decision on #54).

The owner ruled that a nonexistent model chosen by the AUTHOR is ``agency`` — the
harness has a catalog and should not make that mistake — and that when the slug
came from a human instruction the harness must pick an existing adequate model
and WARN, rather than kill the leaf or stop the run for a person to answer.

These tests are the discriminators for an authority the two existing mechanisms
deliberately refuse:

- ``leaf_retry`` re-spawns on the SAME route ("re-routing is explicitly NOT
  here"), which for a slug that does not exist buys the same 404 N times;
- ``route_fault``/``routes`` re-route only inside the OPERATOR's written
  envelope ("zero new authority"), which nobody writes for a typo.

So the whole feature is: one new STRUCTURAL error category, one substitution per
node, from a pool the operator already sanctioned, and four ways of saying so out
loud (advisory fault, ``meta.model_substitutions``, the ``node.rerouted`` audit
line, and a durable ``agency`` insight).
"""

from __future__ import annotations

import json

import httpx
import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.pricing.estimate import ModelPrice
from lohra.providers import get_provider_profile
from lohra.providers.errors import MODEL_NOT_FOUND, classify_provider_error
from lohra.state import SessionDB
from lohra.workflow import library, quiescence
from lohra.workflow.audit import CHANNEL_CATALOG, NODE_REROUTED, sanitize_audit_event
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.leaf_retry import NO_RESPAWN_KINDS
from lohra.workflow.route_fault import ROUTE_FAULT, should_pause_on_route_fault
from lohra.workflow.model_substitution import anchor_tier, choose, offline_catalog
from lohra.workflow.schema import validate_spec
from lohra.workflow.tiers import Tier, TierMap
from tests.test_workflow_pipeline import ScriptedClient

DEAD_MODEL = "nonexistent-xyz"
REAL_MODEL = "claude-sonnet-4-6"
CHEAP_MODEL = "claude-haiku-4-5"
DEAR_MODEL = "claude-fable-5"

_TABLE = {
    ("anthropic", REAL_MODEL): ModelPrice(input_usd=3.0, output_usd=15.0),
    ("anthropic", CHEAP_MODEL): ModelPrice(input_usd=1.0, output_usd=5.0),
    ("anthropic", DEAR_MODEL): ModelPrice(input_usd=10.0, output_usd=50.0),
    ("openai-codex", "gpt-5.5-codex"): ModelPrice(input_usd=0.0, output_usd=0.0),
}
_CATALOG = {
    "anthropic": (CHEAP_MODEL, REAL_MODEL, DEAR_MODEL),
    "openai-codex": ("gpt-5.5-codex",),
    "openrouter": ("openai/gpt-4o",),
}


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """No test here reads the developer's own ``~/.lohra/pricing.json``: the
    price gate is load-bearing and an override could flip a verdict silently."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)


@pytest.fixture(autouse=True)
def fast_quiescence(monkeypatch):
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)


# --- 1. the STRUCTURAL shape of "that model does not exist" -------------------


def _anthropic_not_found():
    """The shape the installed anthropic SDK raises: 404 + ``not_found_error``.

    Built by HAND, never by importing the SDK class: ``classify_provider_error``
    must key on (module, name) + structure, so a duck with the same identity has
    to classify identically (this is also what keeps issue #80's rewrite of the
    classifier mergeable with this one)."""
    return _duck(
        "anthropic",
        "NotFoundError",
        status_code=404,
        body={"type": "error", "error": {"type": "not_found_error", "message": "model"}},
    )


def _openai_not_found():
    return _duck("openai", "NotFoundError", status_code=404, code="model_not_found")


# The body OpenRouter REALLY answers with for a model id it does not serve —
# captured live on 2026-09-05 by dogfood T17(a) and pasted here verbatim from
# ``scratchpad/w9/dogfood/T17a-stderr.txt`` (and ``T17a-raw.json``, where it is
# the ``last_error`` of the route_fault payload). The whole point of this
# fixture is that nobody invented it: the earlier HYPOTHETICAL shape assumed a
# ``code: "model_not_found"`` string, and the live gateway sends the INTEGER
# 400 — the HTTP status echoed back — with the only model-specific signal in
# ``message``.
OPENROUTER_BODY = {
    "error": {
        "message": "nonexistent-vendor/e8-xyz is not a valid model ID",
        "code": 400,
    },
    "user_id": "user_REDACTED",
}


def _openrouter_bad_request():
    """The CAPTURED (2026-09-05) OpenRouter shape, through the openai SDK.

    ``code`` is an int equal to the status, so the structural-code branch cannot
    fire; this is the case the message fingerprint exists for."""
    return _duck(
        "openai", "BadRequestError", status_code=400,
        code=OPENROUTER_BODY["error"]["code"], body=OPENROUTER_BODY,
    )


def _openrouter_near_miss():
    """The SAME class, status and code shape — a different sentence. Nothing in
    the structure distinguishes it, which is exactly why the fingerprint table
    must be exact substrings from captured bodies and never a pattern."""
    return _duck(
        "openai", "BadRequestError", status_code=400, code=400,
        body={"error": {"message": "temperature is not a valid parameter", "code": 400}},
    )


def _openrouter_with_a_string_code():
    """A gateway that DOES send a structural code is still read structurally —
    the fingerprint is a fallback for bodies that carry none, never a
    replacement for the code branch."""
    return _duck(
        "openai", "BadRequestError", status_code=400, code="model_not_found",
        body={"error": {"message": "nothing here matches a fingerprint", "code": "model_not_found"}},
    )


def _duck(module: str, name: str, **attrs):
    """An exception whose ``__module__``/``__name__`` are the SDK's, without the
    SDK: the classifier may not use ``isinstance`` against an optional extra."""
    cls = type(name, (Exception,), {"__module__": module})
    exc = cls("boom")
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


@pytest.mark.parametrize(
    "exc",
    [
        _anthropic_not_found(),
        _openai_not_found(),
        _openrouter_bad_request(),
        _openrouter_with_a_string_code(),
    ],
    ids=[
        "anthropic-404-captured",
        "openai-404-captured",
        "openrouter-400-captured-2026-09-05",
        "openrouter-400-string-code",
    ],
)
def test_a_model_that_does_not_exist_is_classified_structurally(exc):
    assert classify_provider_error(exc) == MODEL_NOT_FOUND


def test_a_near_miss_sentence_from_the_SAME_gateway_shape_is_refused():
    """The fingerprint is an exact substring captured from a real body, so a
    different complaint on the identical class/status/code shape classifies as
    nothing. This is the test that keeps the exception from widening into "any
    400 from openrouter is a missing model"."""
    assert classify_provider_error(_openrouter_near_miss()) is None


def test_the_fingerprint_table_is_a_closed_constant_of_exact_substrings():
    """The documented exception to "never prose" is only defensible while it
    stays a table: provider-scoped keys, exact substrings, no patterns."""
    from lohra.providers.errors import _MESSAGE_FINGERPRINTS

    assert set(_MESSAGE_FINGERPRINTS) == {("openai", "BadRequestError", 400)}
    for key, fingerprints in _MESSAGE_FINGERPRINTS.items():
        assert isinstance(key, tuple) and len(key) == 3
        assert isinstance(fingerprints, tuple) and fingerprints
        for fingerprint in fingerprints:
            assert isinstance(fingerprint, str) and fingerprint
            # No regex metacharacters: this is `in`, never `re.search`.
            assert not set(fingerprint) & set("^$*+?[]()|\\")


def test_a_fingerprint_never_fires_for_another_CLASS_or_STATUS():
    """The table is keyed on (module, name, status): the same sentence from a
    class or a status nobody captured is not the shape that was measured."""
    body = {"error": {"message": "x is not a valid model ID", "code": 404}}
    assert classify_provider_error(
        _duck("openai", "BadRequestError", status_code=404, code=404, body=body)
    ) is None
    assert classify_provider_error(
        _duck("anthropic", "BadRequestError", status_code=400, code=400,
              body={"error": {"message": "x is not a valid model ID", "code": 400}})
    ) is None


def test_a_fingerprint_needs_the_code_to_be_the_STATUS_echoed_back():
    """The branch fires only where NO structural code exists. A gateway that
    sends some other int, or a string, is not the captured shape."""
    body = {"error": {"message": "x is not a valid model ID", "code": 500}}
    assert classify_provider_error(
        _duck("openai", "BadRequestError", status_code=400, code=500, body=body)
    ) is None


def test_prose_alone_never_classifies_a_model_as_missing():
    """The module's own contract, unweakened by the fingerprint table: the
    identity gate runs first, so a tool result quoting either sentence back at
    us moves no route."""
    assert classify_provider_error(RuntimeError("model_not_found: nonexistent-xyz")) is None
    assert classify_provider_error(RuntimeError("x is not a valid model ID")) is None


def test_a_payload_code_from_a_class_the_harness_does_not_know_is_refused():
    """Fail-closed on identity for BOTH routes into the category, not just the
    status one: ``code`` is read off an exception object, and some other
    library's exception carrying that word is not a provider verdict."""
    assert (
        classify_provider_error(
            _duck("requests", "HTTPError", status_code=404, code="model_not_found")
        )
        is None
    )


def test_a_404_from_a_class_the_harness_does_not_know_stays_unclassified():
    """Fail-closed on identity: a 404 from some other library is an ordinary
    failure whose leaf dies alone, not a licence to change a model."""
    assert classify_provider_error(_duck("requests", "HTTPError", status_code=404)) is None


def test_an_anthropic_404_that_is_not_a_not_found_error_is_not_a_missing_model():
    assert (
        classify_provider_error(
            _duck("anthropic", "NotFoundError", status_code=404, body={"nope": 1})
        )
        is None
    )


def test_a_timeout_is_still_a_timeout():
    """Ordering regression: the new branch must not shadow its siblings."""
    from lohra.providers.errors import TIMEOUT

    assert classify_provider_error(httpx.ReadTimeout("slow")) == TIMEOUT


def test_a_dead_slug_still_PAUSES_the_run_when_nothing_can_replace_it():
    """The pause of #43 must survive this slice. A dead slug is deterministic
    within the route exactly as a refused credential is, so when no substitute
    exists the run must STOP and ask a human — not schedule every remaining node
    onto a model the provider has already said it does not have."""
    assert should_pause_on_route_fault(_Node({}), "error", MODEL_NOT_FOUND) is True


class _Node:
    """The two attributes ``should_pause_on_route_fault`` reads."""

    def __init__(self, fields):
        self.fields = fields


def test_a_dead_slug_never_buys_a_same_route_respawn():
    """``retries`` must not spend N attempts proving the same slug still does not
    exist — the failure is deterministic within the route, exactly like an auth
    refusal, so the remedy is a different MODEL, not another attempt."""
    assert MODEL_NOT_FOUND in NO_RESPAWN_KINDS


# --- 2. choosing the substitute: OFFLINE, operator-sanctioned, priced ---------


def test_the_offline_catalog_never_touches_the_network():
    """``build_catalog`` is a live fetch and must never be reached from the
    dispatch path. The offline source is the price snapshot: every id in it is a
    real model AND has a readable bill, which is the pair the choice needs."""
    catalog = offline_catalog(table=_TABLE)
    assert REAL_MODEL in catalog["anthropic"]
    assert "openai-codex" not in catalog  # a plan is not a per-token route (#63)


def test_the_substitute_is_the_operators_model_for_the_declared_tier():
    tiers = TierMap({"medium": Tier(model=REAL_MODEL), "big": Tier(model=DEAR_MODEL)})
    assert (
        choose(_CATALOG, "anthropic", DEAD_MODEL, "medium", tiers, table=_TABLE)
        == REAL_MODEL
    )


def test_an_unmapped_tier_walks_UP_never_down():
    """"the nearest above": a tier the operator never mapped falls to the next
    tier up, never to a cheaper one the author did not ask for."""
    tiers = TierMap({"small": Tier(model=CHEAP_MODEL), "big": Tier(model=DEAR_MODEL)})
    assert choose(_CATALOG, "anthropic", DEAD_MODEL, "medium", tiers, table=_TABLE) == DEAR_MODEL


def test_a_node_that_declared_no_tier_and_has_no_ANCHOR_substitutes_nothing():
    """No declared tier is no anchor at all, and the harness does not invent
    one: guessing ``small`` would be the harness choosing a capability the
    author never asked for, on a spec whose only defect is a slug."""
    tiers = TierMap({"small": Tier(model=CHEAP_MODEL), "big": Tier(model=DEAR_MODEL)})
    assert choose(_CATALOG, "anthropic", DEAD_MODEL, None, tiers, table=_TABLE) is None


def test_the_anchor_is_the_tier_this_SESSION_is_working_at():
    """What the harness may read instead of guessing: the tier the operator
    mapped onto the session's OWN default model. That is a fact about how this
    run was launched, not an invention about what the author meant."""
    tiers = TierMap({"small": Tier(model=CHEAP_MODEL), "medium": Tier(model=REAL_MODEL)})
    assert anchor_tier(tiers, ("anthropic", REAL_MODEL), "anthropic") == "medium"
    assert (
        choose(
            _CATALOG, "anthropic", DEAD_MODEL, None, tiers,
            anchor_tier="medium", table=_TABLE,
        )
        == REAL_MODEL
    )


def test_a_session_default_the_operator_never_mapped_is_no_anchor():
    tiers = TierMap({"small": Tier(model=CHEAP_MODEL)})
    assert anchor_tier(tiers, ("anthropic", "claude-opus-4-8"), "anthropic") is None


def test_a_session_default_on_ANOTHER_provider_is_no_anchor():
    """The substitution is same-provider by rule, so a default that lives
    somewhere else says nothing about the tier this node was working at."""
    tiers = TierMap({"medium": Tier(model=REAL_MODEL)})
    assert anchor_tier(tiers, ("openai", REAL_MODEL), "anthropic") is None
    assert anchor_tier(tiers, None, "anthropic") is None


def test_a_declared_tier_always_beats_the_anchor():
    tiers = TierMap({"small": Tier(model=CHEAP_MODEL), "big": Tier(model=DEAR_MODEL)})
    assert (
        choose(
            _CATALOG, "anthropic", DEAD_MODEL, "small", tiers,
            anchor_tier="big", table=_TABLE,
        )
        == CHEAP_MODEL
    )


def test_a_tier_mapped_onto_another_provider_is_never_a_candidate():
    """Cross-provider substitution is out of scope (#85) and out of authority
    (#63): a different provider is a different credential and a different bill."""
    tiers = TierMap({"medium": Tier(model=REAL_MODEL, provider="openai")})
    assert choose(_CATALOG, "anthropic", DEAD_MODEL, "medium", tiers, table=_TABLE) is None


def test_a_candidate_the_catalog_does_not_know_is_refused():
    tiers = TierMap({"medium": Tier(model="also-not-real")})
    assert choose(_CATALOG, "anthropic", DEAD_MODEL, "medium", tiers, table=_TABLE) is None


def test_a_candidate_nobody_prices_is_refused():
    """Doctrine 2 of #63, unchanged: the harness never acts on a bill it cannot
    read — so openrouter substitutes nothing without an operator override."""
    tiers = TierMap({"medium": Tier(model="openai/gpt-4o")})
    assert choose(_CATALOG, "openrouter", "nope", "medium", tiers, table=_TABLE) is None


def test_a_subscription_is_never_a_candidate():
    tiers = TierMap({"medium": Tier(model="gpt-5.5-codex")})
    assert choose(_CATALOG, "openai-codex", DEAD_MODEL, "medium", tiers, table=_TABLE) is None


def test_the_dead_slug_is_never_its_own_substitute():
    tiers = TierMap({"medium": Tier(model=DEAD_MODEL), "big": Tier(model=DEAR_MODEL)})
    assert choose(_CATALOG, "anthropic", DEAD_MODEL, "medium", tiers, table=_TABLE) == DEAR_MODEL


def test_no_tier_map_at_all_substitutes_nothing():
    """The pool is the OPERATOR's map and nothing else — the harness never picks
    a model off a catalog nobody sanctioned."""
    assert choose(_CATALOG, "anthropic", DEAD_MODEL, "medium", None, table=_TABLE) is None


# --- 3. the engine: one substitution, and four ways of saying so --------------


class _RoutedClient(ScriptedClient):
    """A client that answers for the models that exist and raises the structural
    404 for the one that does not — so "the run finished" is proof the MODEL
    moved, not that a retry got lucky."""

    def __init__(self, answer="substituted answer", alive=(REAL_MODEL, CHEAP_MODEL, DEAR_MODEL)):
        super().__init__(lambda _p: answer)
        self._alive = alive
        self.models: list[str] = []

    def create(self, **kwargs):
        model = kwargs.get("model")
        self.models.append(model)
        if model not in self._alive:
            raise _anthropic_not_found()
        return super().create(**kwargs)


def _core(db, client):
    def factory():
        return Agent(
            model=DEAD_MODEL,
            provider=get_provider_profile("anthropic"),
            client=client,
        )

    return OrchestrationCore(db, factory, max_concurrent=4)


def _spec(**fields):
    node = {"id": "a", "type": "agent", "prompt": "go", "model": DEAD_MODEL}
    node.update(fields)
    return validate_spec({"meta": {"name": "substitution"}, "nodes": [node]})


def _tiers():
    return TierMap({"medium": Tier(model=REAL_MODEL), "big": Tier(model=DEAR_MODEL)})


# What the SESSION was launched on. The engine reads it only to derive the
# anchor tier for a node that declared none; the default here maps to `medium`
# in ``_tiers()``, so the anchored walk starts where this run was working.
DEFAULT_ROUTE = ("anthropic", REAL_MODEL)


def _run(db, spec=None, *, tiers=None, client=None, on_audit=None,
         default_route=DEFAULT_ROUTE):
    client = client if client is not None else _RoutedClient()
    core = _core(db, client)
    engine = WorkflowEngine(
        core,
        budget=Budget(),
        tiers=_tiers() if tiers is None else tiers,
        on_audit=on_audit,
        default_route=default_route,
    )
    try:
        return engine.run(spec if spec is not None else _spec(), {}), engine, client
    finally:
        core.shutdown()


def test_a_nonexistent_model_is_substituted_once_and_the_run_completes(db):
    result, _engine, client = _run(db, _spec(tier="medium"))
    assert result.outputs["a"] == "substituted answer"
    assert result.status == "complete"
    assert client.models == [DEAD_MODEL, REAL_MODEL]
    assert result.leaf_respawns == 1


def test_the_substitution_is_never_silent(db):
    result, _engine, _client = _run(db, _spec(tier="medium"))
    advisory = [f for f in result.advisory_faults if "does not exist" in f]
    assert advisory, result.faults
    assert DEAD_MODEL in advisory[0] and REAL_MODEL in advisory[0] and "anthropic" in advisory[0]
    # ...and every advisory is a fault too: one door in, never a second channel.
    assert set(result.advisory_faults) <= set(result.faults)


def test_the_substitution_is_recorded_as_data_not_only_as_prose(db):
    result, _engine, _client = _run(db, _spec(tier="medium"))
    assert result.model_substitutions == [
        {"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}
    ]


def test_the_audit_ledger_records_the_move_and_names_the_catalog_channel(db):
    events: list[dict] = []
    _run(db, _spec(tier="medium"), on_audit=events.append)
    moved = [e for e in events if e.get("event_type") == NODE_REROUTED]
    assert len(moved) == 1
    # Sanitized, because the ledger's allow-list is where a vocabulary nobody
    # declared turns into ``excluded_by_policy`` — a redaction in a run where
    # nothing was withheld.
    data = sanitize_audit_event(moved[0])["data"]
    assert data["node_id"] == "a"
    assert data["from"] == {"provider": "anthropic", "model": DEAD_MODEL}
    assert data["to"] == {"provider": "anthropic", "model": REAL_MODEL}
    assert data["channel"] == CHANNEL_CATALOG


def test_the_death_on_the_dead_slug_does_not_degrade_the_run_that_survived_it(db):
    """The mirror of Q2/#63: a node that produced its output on the substitute is
    not evidence the SHAPE is broken, so the original death is retired from the
    verdict (it stays in ``faults``, verbatim)."""
    result, _engine, _client = _run(db, _spec(tier="medium"))
    assert any("leaf error" in f for f in result.faults)
    assert any("leaf error" in f for f in result.recovered_faults)


def test_the_substituted_route_is_folded_into_the_spec_a_resume_would_read(db):
    """Without this a resume schedules the node back onto the slug that does not
    exist, and pays for the same 404 again."""
    from lohra.workflow.route_fault import apply_reroutes

    result, _engine, _client = _run(db, _spec(tier="medium"))
    spec = apply_reroutes(
        {"meta": {"name": "s"}, "nodes": [{"id": "a", "type": "agent", "prompt": "go",
                                           "model": DEAD_MODEL}]},
        result.reroutes,
    )
    assert spec["nodes"][0]["model"] == REAL_MODEL


def test_a_node_is_substituted_at_most_ONCE_and_then_the_run_PAUSES(db):
    """Bounded by construction: "the model does not exist" must never become a
    walk down the catalog at the operator's expense. And when the substitute is
    dead too, the run stops and asks a human — the #43 pause, on a route the
    harness has now proved twice it cannot use."""
    client = _RoutedClient(alive=())  # nothing exists, not even the substitute
    result, _engine, _client = _run(db, _spec(tier="medium"), client=client)
    assert result.outputs["a"] is None
    assert client.models == [DEAD_MODEL, REAL_MODEL]
    assert result.status == "paused"
    assert result.pause_reason == ROUTE_FAULT
    # The pause names the model that ACTUALLY died last — the substitute — or a
    # human would go looking for a slug the run had already replaced.
    assert result.route_fault["model"] == REAL_MODEL
    assert result.route_fault["error_kind"] == MODEL_NOT_FOUND


def test_with_no_substitute_the_run_PAUSES_after_ONE_leaf(db):
    """The regression this slice must not cause. A dead slug is deterministic
    within the route, so with nothing to replace it the run stops and asks a
    human — it does NOT schedule every remaining node onto a model the provider
    has already refused."""
    spec = validate_spec({
        "meta": {"name": "three"},
        "nodes": [
            {"id": node_id, "type": "agent", "prompt": "go", "model": DEAD_MODEL}
            for node_id in ("a", "b", "c")
        ],
    })
    result, _engine, client = _run(db, spec, tiers=TierMap({}))
    assert result.status == "paused"
    assert result.pause_reason == ROUTE_FAULT
    assert result.route_fault["error_kind"] == MODEL_NOT_FOUND
    assert result.route_fault["model"] == DEAD_MODEL
    assert client.models == [DEAD_MODEL]  # ONE leaf, not three
    assert not result.model_substitutions


def test_a_nested_template_is_never_substituted(db):
    """Same refusal, same reason, as the envelope's: that node is not in the spec
    this run persists, so no resume could carry the new model forward."""
    client = _RoutedClient()
    core = _core(db, client)
    engine = WorkflowEngine(core, budget=Budget(), tiers=_tiers(), depth=1)
    try:
        result = engine.run(_spec(tier="medium"), {})
    finally:
        core.shutdown()
    assert result.outputs["a"] is None
    assert not result.model_substitutions


def test_a_rigor_node_is_never_substituted(db):
    """A verify/debate node keys its cell on its routing only when it declares
    any, and its strategy owns its own leaf loop — substituting under a cell
    whose key did not move would poison the cache."""
    spec = validate_spec({
        "meta": {"name": "s"},
        "nodes": [{"id": "a", "type": "verify", "finding": "go", "skeptics": 1,
                   "model": DEAD_MODEL, "tier": "medium"}],
    })
    result, _engine, _client = _run(db, spec)
    assert not result.model_substitutions


# --- 4. the learning loop: this is AGENCY, by the owner's decision -------------


def test_the_substitution_is_learned_as_agency(db):
    from lohra.workflow.service import model_substitution_signals

    signals = model_substitution_signals()
    from lohra.workflow.failure_taxonomy import Responsibility, classify_failure

    observation = classify_failure(
        status="model_substituted", mechanism="validation", signals=signals, confidence=1.0
    )
    assert observation.responsibility is Responsibility.AGENCY
    assert "rule:model_not_found" in signals


# --- 5. certification: a template never hides what its run had to correct ------


def test_the_certified_template_names_the_models_it_substituted(db, tmp_path):
    from lohra.workflow.accounting import RunResult

    home = tmp_path / "home"
    result = RunResult(status="complete", nodes_total=1)
    library.record_outcome(
        home,
        {"meta": {"name": "sub"}, "nodes": []},
        result,
        model_substitutions=[{"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}],
    )
    stamped = json.loads((home / "workflows" / "templates" / "sub.json").read_text())
    assert stamped["meta"]["model_substitutions"] == [
        {"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}
    ]


def test_a_template_nobody_substituted_carries_no_new_noise(db, tmp_path):
    from lohra.workflow.accounting import RunResult

    home = tmp_path / "home"
    library.record_outcome(home, {"meta": {"name": "plain"}, "nodes": []},
                           RunResult(status="complete", nodes_total=1))
    stamped = json.loads((home / "workflows" / "templates" / "plain.json").read_text())
    assert "model_substitutions" not in stamped["meta"]


def test_a_declared_retry_series_never_burns_an_attempt_on_the_dead_slug(db):
    """The RED probe measured `retries: 2` spending THREE whole leaves proving
    the same nonexistent slug still did not exist, and only then pausing. The
    substitution must land on the FIRST death, not after the series."""
    result, _engine, client = _run(db, _spec(tier="medium", retries=2))
    assert client.models == [DEAD_MODEL, REAL_MODEL]
    assert result.leaf_respawns == 1
    assert result.status == "complete"
    assert result.pause_reason is None


def test_the_substituted_node_is_named_by_BOTH_stamps(db, tmp_path):
    """A substituted node really was MOVED, so it belongs in the channel-blind
    ``rerouted_nodes`` too. ``model_substitutions`` beside it is what says WHICH
    model replaced which — the one question the blind stamp cannot answer."""
    from lohra.workflow.accounting import RunResult

    home = tmp_path / "home"
    result = RunResult(status="complete", nodes_total=1)
    result.reroutes.append({"node_id": "a", "model": REAL_MODEL})
    result.model_substitutions.append({"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL})
    library.record_outcome(
        home,
        {"meta": {"name": "both"}, "nodes": []},
        result,
        rerouted_nodes=["a"],
        model_substitutions=list(result.model_substitutions),
    )
    meta = json.loads((home / "workflows" / "templates" / "both.json").read_text())["meta"]
    assert meta["rerouted_nodes"] == ["a"]
    assert meta["model_substitutions"] == [
        {"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}
    ]


def test_a_substitution_survives_a_resume_into_the_certified_template():
    """The stretch that certifies is usually not the stretch that substituted.
    Without ``carried_substitutions`` the template would publish the corrected
    slug in ``nodes[].model`` while ``meta`` read as a run nobody had to fix."""
    from lohra.workflow.accounting import RunResult
    from lohra.workflow.runstate_store import carried_substitutions

    prior = [{"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}]
    fresh = RunResult()  # the later stretch never saw the earlier substitution
    assert carried_substitutions(prior, fresh) == prior
    assert carried_substitutions([], None) == []
    # ...and a half-named row from an older (or hostile) line names nothing.
    assert carried_substitutions([{"node": "a", "to": REAL_MODEL}], None) == []


def test_the_durable_line_carries_the_substitutions_it_was_given(db):
    """Cross-process, not only cross-stretch: a resume in a FRESH process reads
    the line, so the rows have to survive the JSON round-trip."""
    from lohra.workflow.runstate_store import RunStateStore

    store = RunStateStore(db)
    rows = [{"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}]
    store.save(run_id="run-1", status="complete", prior_substitutions=rows)
    assert store.load("run-1").prior_substitutions == rows


# --- 6. the fix round: what a substitution may claim, and for whom ------------


def test_a_substitution_that_never_ANSWERED_is_neither_stamped_nor_learned(db):
    """The row and the insight are claims that a correction WORKED. A substitute
    that died too corrected nothing: stamping it would publish a template whose
    `meta` advertises a model that never produced a token, and teaching it would
    feed the loop a lesson whose own evidence is a second failure. The advisory
    fault stays — the attempt really was made and the run really was billed for
    it — and so does the `reroutes` row, which is what keeps a resume off the
    slug that does not exist."""
    client = _RoutedClient(alive=())
    result, _engine, _client = _run(db, _spec(tier="medium"), client=client)
    assert result.model_substitutions == []
    assert any("does not exist" in fault for fault in result.advisory_faults)
    assert result.reroutes == [{"node_id": "a", "model": REAL_MODEL}]


def test_a_substitution_that_answered_IS_stamped(db):
    """...and the mirror, so the discount above is not simply "never stamp"."""
    result, _engine, _client = _run(db, _spec(tier="medium"))
    assert result.model_substitutions == [
        {"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}
    ]


def test_a_node_that_declared_NO_routing_is_never_substituted(db):
    """The dead slug is the SESSION's default, not the author's choice. Every
    recording surface of this mechanism blames the SPEC —
    `meta.model_substitutions` on the template, an `agency` insight in the
    learning loop — so substituting here would attribute the operator's profile
    configuration to the person who wrote the workflow. The run pauses instead,
    with the advice that names the real remedy."""
    spec = validate_spec({
        "meta": {"name": "bare"},
        "nodes": [{"id": "a", "type": "agent", "prompt": "go"}],
    })
    result, _engine, client = _run(db, spec)
    assert not result.model_substitutions
    assert client.models == [DEAD_MODEL]
    assert result.status == "paused"
    assert result.pause_reason == ROUTE_FAULT
    advice = [f for f in result.advisory_faults if "session default model" in f]
    assert advice, result.faults
    assert "declare" in advice[0]


# --- 7. the dogfood: the shape a real gateway actually sends ------------------


class _GatewayClient(ScriptedClient):
    """A client that fails the way OPENROUTER really failed on 2026-09-05 —
    the captured 400 whose only model-specific signal is the message."""

    def __init__(self, answer="substituted answer", alive=(REAL_MODEL,)):
        super().__init__(lambda _p: answer)
        self._alive = alive
        self.models: list[str] = []

    def create(self, **kwargs):
        model = kwargs.get("model")
        self.models.append(model)
        if model not in self._alive:
            raise _duck(
                "openai", "BadRequestError", status_code=400,
                code=400,
                body={
                    "error": {"message": f"{model} is not a valid model ID", "code": 400},
                    "user_id": "user_REDACTED",
                },
            )
        return super().create(**kwargs)


def test_the_CAPTURED_gateway_shape_reaches_the_substitution_end_to_end(db):
    """Dogfood T17(a)/(b) failed here and nowhere else: the classifier never
    named the kind, so `substitute_model` was never consulted, the whole
    `retries` series burned, and the run paused on the GENERIC route_fault with
    `error_kind: null` — as if #85 did not exist. One leaf, one substitution."""
    client = _GatewayClient()
    result, _engine, _client = _run(db, _spec(tier="medium", retries=2), client=client)
    assert client.models == [DEAD_MODEL, REAL_MODEL]  # not three attempts
    assert result.leaf_respawns == 1
    assert result.status == "complete"
    assert result.model_substitutions == [
        {"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}
    ]
    assert any("does not exist" in fault for fault in result.advisory_faults)


def test_the_CAPTURED_gateway_shape_with_no_substitute_pauses_on_the_NAMED_kind(db):
    """...and the other half of what the dogfood measured: with nothing to
    substitute the run still pauses, but now the payload NAMES the kind instead
    of reporting `error_kind: null` after three burned attempts."""
    client = _GatewayClient(alive=())
    result, _engine, _client = _run(
        db, _spec(retries=2), tiers=TierMap({}), client=client
    )
    assert client.models == [DEAD_MODEL]
    assert result.leaf_respawns == 0
    assert result.status == "paused"
    assert result.pause_reason == ROUTE_FAULT
    assert result.route_fault["error_kind"] == MODEL_NOT_FOUND


# --- 8. dogfood round 2: what the SDK hands us, not what the wire sent --------
#
# The first dogfood fix pasted the RAW HTTP body into the fixture and passed
# every test while staying dead in production: the openai SDK UNWRAPS the
# ``error`` layer (``_make_status_error``: ``data = body.get("error", body)``)
# before the exception exists, so ``exc.body`` is the inner dict and any
# accessor doing ``body["error"]`` reads None. Anthropic does NOT unwrap.
# These tests go through the SDKs' own constructors so a fixture can never
# again disagree with the live path.

OPENROUTER_RAW_BODY = {
    "error": {
        "message": "nonexistent-vendor/e8b-xyz is not a valid model ID",
        "code": 400,
    },
    "user_id": "user_REDACTED",
}


def _sdk_openrouter_error(raw_body=None, status=400):
    """The exception the INSTALLED openai SDK builds from the captured body.

    Verbatim from scratchpad/w9/dogfood/T17b-a-stderr.txt (dogfood T17 re-run,
    2026-09-05). Built through ``_make_status_error_from_response`` — the same
    private path a live 4xx takes — so the unwrapping the SDK performs has
    already happened when the classifier sees it."""
    import openai

    client = openai.OpenAI(api_key="test-key", base_url="https://openrouter.ai/api/v1")
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        json=OPENROUTER_RAW_BODY if raw_body is None else raw_body,
        headers={"content-type": "application/json"},
    )
    return client._make_status_error_from_response(response)


def test_the_SDK_BUILT_openrouter_error_is_classified():
    """The test the first fix did not have. Its hand-built fixture asserted on
    the wire body; this asserts on the object the SDK really produces."""
    exc = _sdk_openrouter_error()
    assert exc.body == {
        "message": "nonexistent-vendor/e8b-xyz is not a valid model ID",
        "code": 400,
    }, "the openai SDK unwraps `error`; if this changes, the accessor must too"
    assert classify_provider_error(exc) == MODEL_NOT_FOUND


def test_the_near_miss_is_still_refused_when_the_SDK_builds_it():
    exc = _sdk_openrouter_error(
        {"error": {"message": "temperature is not a valid parameter", "code": 400}}
    )
    assert classify_provider_error(exc) is None


def test_an_SDK_BUILT_anthropic_not_found_still_classifies():
    """Anthropic keeps the wrapper, so the tolerant accessor must read BOTH
    shapes — fixing one must not break the other."""
    import anthropic

    client = anthropic.Anthropic(api_key="test-key")
    response = httpx.Response(
        404,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json={"type": "error", "error": {"type": "not_found_error", "message": "model"}},
        headers={"content-type": "application/json"},
    )
    exc = client._make_status_error_from_response(response)
    assert "error" in exc.body, "anthropic does NOT unwrap; the accessor must tolerate it"
    assert classify_provider_error(exc) == MODEL_NOT_FOUND


class _SdkGatewayClient(ScriptedClient):
    """The live failure end-to-end: a leaf whose provider raises the exception
    the SDK really constructs for an unknown OpenRouter model id."""

    def __init__(self, answer="substituted answer", alive=(REAL_MODEL,)):
        super().__init__(lambda _p: answer)
        self._alive = alive
        self.models: list[str] = []

    def create(self, **kwargs):
        model = kwargs.get("model")
        self.models.append(model)
        if model not in self._alive:
            raise _sdk_openrouter_error(
                {"error": {"message": f"{model} is not a valid model ID", "code": 400}}
            )
        return super().create(**kwargs)


def test_the_SDK_BUILT_shape_reaches_the_substitution_end_to_end(db):
    """What dogfood T17 measured twice: three burned attempts and a generic
    route_fault with `error_kind: null`. One leaf, one substitution."""
    client = _SdkGatewayClient()
    result, _engine, _client = _run(db, _spec(tier="medium", retries=2), client=client)
    assert client.models == [DEAD_MODEL, REAL_MODEL]
    assert result.leaf_respawns == 1
    assert result.status == "complete"
    assert result.model_substitutions == [
        {"node": "a", "from": DEAD_MODEL, "to": REAL_MODEL}
    ]
    assert any("does not exist" in fault for fault in result.advisory_faults)


def test_the_SDK_BUILT_shape_with_no_substitute_pauses_on_the_NAMED_kind(db):
    client = _SdkGatewayClient(alive=())
    result, _engine, _client = _run(
        db, _spec(retries=2), tiers=TierMap({}), client=client
    )
    assert client.models == [DEAD_MODEL]
    assert result.leaf_respawns == 0
    assert result.status == "paused"
    assert result.pause_reason == ROUTE_FAULT
    assert result.route_fault["error_kind"] == MODEL_NOT_FOUND
