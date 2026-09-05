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
from lohra.workflow.model_substitution import choose, offline_catalog
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


def _openrouter_bad_request():
    """OpenRouter answers through the openai SDK with a 400 whose payload code
    still names the model."""
    return _duck("openai", "BadRequestError", status_code=400, code="model_not_found")


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
    [_anthropic_not_found(), _openai_not_found(), _openrouter_bad_request()],
    ids=["anthropic-404", "openai-404", "openrouter-400"],
)
def test_a_model_that_does_not_exist_is_classified_structurally(exc):
    assert classify_provider_error(exc) == MODEL_NOT_FOUND


def test_prose_alone_never_classifies_a_model_as_missing():
    """The module's own contract: never a regex over what the provider said. A
    tool result quoting "model_not_found" back at us must not move a route."""
    assert classify_provider_error(RuntimeError("model_not_found: nonexistent-xyz")) is None


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


def test_a_node_that_declared_no_tier_starts_at_the_CHEAPEST_mapped_tier():
    """No declared tier is no anchor at all, so the walk starts at ``small``:
    the only start that cannot spend more of the operator's money than they
    were asked for."""
    tiers = TierMap({"small": Tier(model=CHEAP_MODEL), "big": Tier(model=DEAR_MODEL)})
    assert choose(_CATALOG, "anthropic", DEAD_MODEL, None, tiers, table=_TABLE) == CHEAP_MODEL


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


def test_a_price_cap_the_operator_set_bounds_the_choice():
    tiers = TierMap({"medium": Tier(model=DEAR_MODEL)})
    cap = ModelPrice(input_usd=3.0, output_usd=15.0)
    assert (
        choose(_CATALOG, "anthropic", DEAD_MODEL, "medium", tiers, price_cap=cap, table=_TABLE)
        is None
    )


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


def _run(db, spec=None, *, tiers=None, client=None, on_audit=None):
    client = client if client is not None else _RoutedClient()
    core = _core(db, client)
    engine = WorkflowEngine(
        core,
        budget=Budget(),
        tiers=_tiers() if tiers is None else tiers,
        on_audit=on_audit,
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


def test_a_node_is_substituted_at_most_ONCE(db):
    """Bounded by construction: "the model does not exist" must never become a
    walk down the catalog at the operator's expense."""
    client = _RoutedClient(alive=())  # nothing exists, not even the substitute
    result, _engine, _client = _run(db, _spec(tier="medium"), client=client)
    assert result.outputs["a"] is None
    assert client.models == [DEAD_MODEL, REAL_MODEL]
    # The one node produced nothing, so the run FAILS exactly as it does today —
    # a substitution that also died launders nothing.
    assert result.status == "failed"


def test_with_no_substitute_the_behaviour_is_exactly_todays(db):
    """No tier map, no candidate, no substitution: the leaf dies alone and the
    run degrades, exactly as it did before this slice."""
    result, _engine, client = _run(db, _spec(), tiers=TierMap({}))
    assert result.outputs["a"] is None
    assert result.status == "failed"
    assert client.models == [DEAD_MODEL]
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
