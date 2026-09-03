"""The OPERATOR's route envelope: a pre-authorized fallback with a durable brake.

Issue #63, opção B of #43 — the half the 0.0.21 pause left to a human. A dead
route still stops the run by default; what changes is that an operator who wrote
``~/.lohra/workflow_routes.json`` BEFORE the run has already answered, and the
harness may move that one node to the next route on their list.

These tests are the DISCRIMINATORS for an authority that must stay small. The
feature is only worth having if every one of them holds:

- the envelope comes off the DISK and only off the disk (a spec that names
  ``routes``/``fallback`` gets nothing);
- "never more expensive" is decided on the PRICE TABLE, and an unreadable price
  on either side means the run pauses — the harness never acts on a bill it
  cannot read;
- the chain is bounded by a DURABLE counter, so a resume cannot refill it;
- the credential/opt-in gate is untouched (``openai-codex`` still needs
  ``subscription_active``);
- a node whose route is not in its CELL KEY is never re-routed, because the cell
  the dead route wrote must stay exactly as replayable as it was.
"""

from __future__ import annotations

import json

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.agent.client_pool import ProviderError
from lohra.providers import get_provider_profile
from lohra.pricing.estimate import ModelPrice, list_price
from lohra.state import SessionDB
from lohra.state.db import ROUTE_FALLBACKS_PER_ROUTE
from lohra.workflow import quiescence
from lohra.workflow.audit import CHANNEL_ROUTE_ENVELOPE, sanitize_audit_event
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.route_fault import (
    ENVELOPE_TAILS,
    ROUTE_FAULT,
    apply_reroutes,
    route_fault_hint,
)
from lohra.workflow.routes import (
    DEFAULT_MAX_FALLBACKS_PER_RUN,
    EMPTY_ENVELOPE,
    OUTCOMES,
    REROUTED,
    ROUTES_FILE,
    RouteEnvelope,
    cheaper_or_equal,
    load_routes,
    next_route,
    route_key,
    route_override,
    split_route_key,
)
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient

# The run's own route, and the two the snapshot prices around it: haiku is
# cheaper on BOTH meters (1/5 against 5/25), fable is dearer on both (10/50).
DEAD_MODEL = "claude-opus-4-8"
CHEAP_MODEL = "claude-haiku-4-5"
DEAR_MODEL = "claude-fable-5"
DEAD_ROUTE = f"anthropic/{DEAD_MODEL}"
CHEAP_ROUTE = f"anthropic/{CHEAP_MODEL}"
DEAR_ROUTE = f"anthropic/{DEAR_MODEL}"


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """No test here may read the developer's own ``~/.lohra/pricing.json``.

    The price comparison is the load-bearing gate, and an operator override on
    this machine could flip a verdict silently in either direction."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)


@pytest.fixture(autouse=True)
def fast_quiescence(monkeypatch):
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)


class _DuckError(Exception):
    """A provider error carrying the structured signal the classifier reads."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _dead(_prompt):
    raise _DuckError("invalid x-api-key", status_code=401)


class _Pool:
    """The ``ClientPool`` surface a leaf actually uses: ``get(name)`` ->
    (profile, client). ``gated`` names the providers that REFUSE to build, which
    is how a real pool answers for a missing key and for ``openai-codex``
    without ``subscription_active``."""

    def __init__(self, client, *, gated: tuple[str, ...] = ()) -> None:
        self._client = client
        self._gated = gated
        self.asked: list[str] = []

    def get(self, name):
        self.asked.append(name)
        if name in self._gated:
            raise ProviderError(f"{name}: not enabled for this run")
        return (get_provider_profile("anthropic"), self._client)


class _Ledger:
    """The durable brake, in memory, with the SAME two ceilings the SQLite
    implementation enforces — so the engine tests can pin the policy and the db
    tests can pin the storage, separately."""

    def __init__(self) -> None:
        self.used: dict[str, int] = {}

    def __call__(self, route: str, max_per_run: int) -> bool:
        if sum(self.used.values()) >= max_per_run:
            return False
        if self.used.get(route, 0) >= ROUTE_FALLBACKS_PER_ROUTE:
            return False
        self.used[route] = self.used.get(route, 0) + 1
        return True

    @property
    def total(self) -> int:
        return sum(self.used.values())


def _core(db, responder):
    def factory():
        return Agent(
            model=DEAD_MODEL,
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=4)


def _spec(node_type: str = "agent", **fields):
    node = {"id": "a", "type": node_type}
    node.update(
        {"prompt": "go"} if node_type == "agent" else {"finding": "go", "skeptics": 1}
    )
    node.update(fields)
    return validate_spec({"meta": {"name": "envelope"}, "nodes": [node]})


def _envelope(*fallbacks: str, dead: str = DEAD_ROUTE, ceiling: int | None = None):
    return RouteEnvelope(
        {dead: tuple(fallbacks)},
        DEFAULT_MAX_FALLBACKS_PER_RUN if ceiling is None else ceiling,
    )


def _run(db, spec, *, envelope, ledger=None, pool=None, alive="rerouted answer",
         cache=None, run_id=None, on_audit=None):
    """One run whose default route auth-fails and whose POOL client answers.

    The two clients are the whole experiment: the leaf on the dead route can only
    raise, and the leaf the envelope re-routes can only be reached through the
    pool. So "the run finished" is proof the re-route really happened, and not
    that a retry got lucky on the same route."""
    core = _core(db, _dead)
    pool = pool if pool is not None else _Pool(ScriptedClient(lambda _p: alive))
    engine = WorkflowEngine(
        core,
        budget=Budget(),
        client_pool=pool,
        routes=envelope,
        route_fallback_try=ledger,
        cache=cache,
        run_id=run_id,
        on_audit=on_audit,
    )
    try:
        return engine.run(spec, {}), engine, pool
    finally:
        core.shutdown()


# --- 1. the envelope FILE: fail-soft, and never wider than what was written ---


def test_a_missing_or_broken_file_is_simply_no_envelope(tmp_path):
    """Operator config never turns a run into an exception — and the fail-soft
    direction is also the fail-CLOSED one: no envelope means today's pause."""
    assert load_routes(tmp_path / "absent.json").empty
    for text in ("{ not json", "[]", '"a string"', '{"routes": []}', "{}"):
        path = tmp_path / ROUTES_FILE
        path.write_text(text, encoding="utf-8")
        assert load_routes(path).empty, text


def test_the_file_is_read_as_written_including_the_bare_list_shorthand(tmp_path):
    path = tmp_path / ROUTES_FILE
    path.write_text(
        json.dumps(
            {
                "routes": {
                    DEAD_ROUTE: {"fallback": [CHEAP_ROUTE, DEAR_ROUTE]},
                    "openai/gpt-5.5": ["openai/gpt-4o-mini"],
                    # A key that names no route can never match a dead one.
                    "anthropic": [CHEAP_ROUTE],
                    # ...and an entry with nothing usable in it is not an entry.
                    "openai/gpt-4o": [None, 7],
                },
                "max_fallbacks_per_run": 5,
            }
        ),
        encoding="utf-8",
    )
    envelope = load_routes(path)
    assert envelope.routes == {
        DEAD_ROUTE: (CHEAP_ROUTE, DEAR_ROUTE),
        "openai/gpt-5.5": ("openai/gpt-4o-mini",),
    }
    assert envelope.max_fallbacks_per_run == 5
    assert envelope.fallbacks("anthropic/unknown") == ()


def test_an_entry_the_loader_cannot_read_at_all_is_not_an_entry(tmp_path):
    """Every shape a hand-written file can get wrong, dropped rather than
    guessed at — and a malformed neighbour never costs the good entries."""
    path = tmp_path / ROUTES_FILE
    path.write_text(
        json.dumps(
            {
                "routes": {
                    "a/dead": "anthropic/opus",  # a bare string is not a list
                    "b/dead": 7,
                    "c/dead": {"fallbacks": [CHEAP_ROUTE]},  # misspelled key
                    "d/dead": {"fallback": "not a list"},
                    "e/dead": {"fallback": []},
                    DEAD_ROUTE: [CHEAP_ROUTE],
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_routes(path).routes == {DEAD_ROUTE: (CHEAP_ROUTE,)}
    # ...and a candidate that is not a route cannot become one.
    assert route_override("anthropic") is None


@pytest.mark.parametrize("knob", ["max_usd_per_cell", "on"])
def test_an_entry_naming_a_knob_this_version_cannot_enforce_is_dropped(tmp_path, knob):
    """Both knobs the issue sketched would NARROW the authorization. Honouring
    the fallback list without them would silently widen what the operator wrote,
    so the entry is dropped whole — the run pauses, as with no file at all."""
    path = tmp_path / ROUTES_FILE
    path.write_text(
        json.dumps({"routes": {DEAD_ROUTE: {"fallback": [CHEAP_ROUTE], knob: 0.01}}}),
        encoding="utf-8",
    )
    assert load_routes(path).empty


@pytest.mark.parametrize("ceiling", [None, "two", True, -1, "nonsense"])
def test_a_typo_in_the_ceiling_falls_back_to_the_default_never_to_unlimited(
    tmp_path, ceiling
):
    path = tmp_path / ROUTES_FILE
    path.write_text(
        json.dumps({"routes": {DEAD_ROUTE: [CHEAP_ROUTE]}, "max_fallbacks_per_run": ceiling}),
        encoding="utf-8",
    )
    assert load_routes(path).max_fallbacks_per_run == DEFAULT_MAX_FALLBACKS_PER_RUN


def test_a_route_key_needs_both_halves_and_splits_on_the_first_slash():
    """Model ids already contain slashes (openrouter serves ``openai/gpt-4o``),
    so the LAST slash is the wrong boundary. And half a route matches nothing:
    a leaf on the run's default may name no model, and one entry must never
    authorize a fallback for every model on a provider."""
    assert route_key("anthropic", "opus") == "anthropic/opus"
    assert route_key("anthropic", None) is None
    assert route_key(None, "opus") is None
    assert route_key("anthropic", "  ") is None
    assert split_route_key("openrouter/openai/gpt-4o") == ("openrouter", "openai/gpt-4o")
    assert split_route_key("anthropic") is None
    assert split_route_key("/opus") is None
    assert route_override(CHEAP_ROUTE) == {"provider": "anthropic", "model": CHEAP_MODEL}


def test_the_next_route_is_the_operators_order_minus_what_was_already_tried():
    envelope = _envelope(CHEAP_ROUTE, DEAR_ROUTE)
    assert next_route(envelope, DEAD_ROUTE) == CHEAP_ROUTE
    assert next_route(envelope, DEAD_ROUTE, [CHEAP_ROUTE]) == DEAR_ROUTE
    assert next_route(envelope, DEAD_ROUTE, [CHEAP_ROUTE, DEAR_ROUTE]) is None
    # A chain never doubles back onto the route that just died.
    assert next_route(_envelope(DEAD_ROUTE, CHEAP_ROUTE), DEAD_ROUTE) == CHEAP_ROUTE
    assert next_route(EMPTY_ENVELOPE, DEAD_ROUTE) is None


# --- 2. "never more expensive" is the PRICE TABLE, and unknown means no -------


def test_the_price_comparison_is_per_token_on_both_meters():
    assert cheaper_or_equal(DEAD_ROUTE, CHEAP_ROUTE) is True
    assert cheaper_or_equal(DEAD_ROUTE, DEAD_ROUTE) is True  # equal passes
    assert cheaper_or_equal(DEAD_ROUTE, DEAR_ROUTE) is False
    # A route that halves one meter and triples the other is not cheaper: it is
    # a different bet, and taking it would be the harness choosing on the
    # operator's money.
    table = {
        ("x", "dead"): ModelPrice(input_usd=2.0, output_usd=2.0),
        ("x", "mixed"): ModelPrice(input_usd=1.0, output_usd=6.0),
    }
    assert cheaper_or_equal("x/dead", "x/mixed", table=table, overrides={}) is False


def test_an_unreadable_price_on_either_side_is_None_never_a_guess():
    """None is the fail-closed answer and every caller reads it as "do not
    re-route". Three ways to get it, each deliberate."""
    # A model nobody prices.
    assert cheaper_or_equal(DEAD_ROUTE, "anthropic/claude-nonesuch-9") is None
    # A dynamic provider with no operator override.
    assert cheaper_or_equal(DEAD_ROUTE, "openrouter/anthropic/claude-haiku-4-5") is None
    assert list_price("openrouter", "anything") is None
    # ...which an override DOES rescue, exactly as it does in estimate_cost.
    overrides = {("openrouter", "cheap"): ModelPrice(input_usd=0.1, output_usd=0.2)}
    assert cheaper_or_equal(DEAD_ROUTE, "openrouter/cheap", overrides=overrides) is True
    # A subscription plan has no per-token bill at all, and no override makes it
    # one: a notional price must never authorize a real re-route.
    assert list_price("openai-codex", "gpt-5.5") is None
    assert (
        list_price(
            "openai-codex",
            "gpt-5.5",
            overrides={("openai-codex", "gpt-5.5"): ModelPrice(0.0, 0.0)},
        )
        is None
    )
    assert cheaper_or_equal(DEAD_ROUTE, "openai-codex/gpt-5.5") is None
    # A local provider is a known ZERO, not an unknown.
    assert list_price("ollama", "llama3").input_usd == 0.0
    assert cheaper_or_equal(DEAD_ROUTE, "ollama/llama3") is True
    # ...and a key that names no route prices nothing, on either side.
    assert cheaper_or_equal("anthropic", CHEAP_ROUTE) is None
    assert cheaper_or_equal(DEAD_ROUTE, "anthropic") is None


# --- 3. the whole loop: a dead route re-routed inside the envelope ------------


def test_an_authorized_cheaper_fallback_re_routes_and_the_run_completes(db):
    ledger = _Ledger()
    result, engine, pool = _run(
        db, _spec(), envelope=_envelope(CHEAP_ROUTE), ledger=ledger
    )
    # The run FINISHED — on a client only the re-route could reach.
    assert result.status == "complete"
    assert result.outputs["a"] == "rerouted answer"
    assert result.pause_reason is None
    assert pool.asked == ["anthropic"] * 2  # the gate check, then the real build
    # ...and it says so, in the run's own faults, naming both routes and the
    # authority. The line is DISCOUNTED from the verdict (that is why the run
    # can seal ``complete``) but never hidden.
    record = next(f for f in result.faults if "re-routed by operator envelope" in f)
    assert f"{DEAD_ROUTE} -> {CHEAP_ROUTE}" in record
    assert "never chosen by the harness beyond the operator's list" in record
    assert record in result.rerouted_faults
    # The death on the dead route is reported AND retired: the node concluded.
    assert any("invalid x-api-key" in f for f in result.faults)
    assert result.recovered_faults
    # One slot spent, on the DEAD route's key.
    assert ledger.used == {DEAD_ROUTE: 1}
    # ...and the spec-shaped half, for the line a resume reads.
    assert result.reroutes == [
        {"node_id": "a", "provider": "anthropic", "model": CHEAP_MODEL}
    ]


def test_the_re_routed_cell_is_a_NEW_cell_and_the_dead_one_stays_replayable(db):
    """The cache safety argument, measured. Only the winning cell is stored, and
    its key carries the route that produced it — so nothing a resume replays can
    ever be attributed to the model that refused."""
    result, engine, _pool = _run(
        db, _spec(), envelope=_envelope(CHEAP_ROUTE), ledger=_Ledger(),
        cache=NodeCache(db, "run-cells"), run_id="run-cells",
    )
    assert result.status == "complete"
    rows = db._connection.execute(
        "SELECT content_hash FROM workflow_node_cache WHERE node_id = 'a'"
    ).fetchall()
    assert len(rows) == 1  # the dead attempt cached nothing
    dead_key = engine.cell_hash("a", "agent", "go", None, None, None, None, None, None)
    live_key = engine.cell_hash(
        "a", "agent", "go", None, CHEAP_MODEL, None, "anthropic", None, None
    )
    assert dead_key != live_key
    assert rows[0]["content_hash"] == live_key


def test_the_audit_ledger_records_the_move_and_the_channel(db):
    """The move is in the durable ledger, and it SURVIVES sanitization.

    That second half is the one worth a test: the audit's allow-list turns any
    field it does not know into ``excluded_by_policy``, which reads as content
    the audit REFUSED — so a route event whose vocabulary was never declared
    would show up as a redaction in a run where nothing was withheld."""
    raw: list[dict] = []
    result, _engine, _pool = _run(
        db, _spec(), envelope=_envelope(CHEAP_ROUTE), ledger=_Ledger(), on_audit=raw.append
    )
    assert result.status == "complete"
    events = [e for e in raw if e.get("event_type") == "node.rerouted"]
    assert len(events) == 1
    data = sanitize_audit_event(events[0])["data"]
    # The SAME typed shape #64's command channel emits — two surfaces for one
    # act, derived by the same ``route_change``, so "was this node re-routed?"
    # is never a question about which code path ran.
    assert data["node_id"] == "a"
    assert data["from"] == {"provider": "anthropic", "model": DEAD_MODEL}
    assert data["to"] == {"provider": "anthropic", "model": CHEAP_MODEL}
    # The CHANNEL, never an author: the harness observes where a route came
    # from, and this one came from a file the operator wrote.
    assert data["channel"] == CHANNEL_ROUTE_ENVELOPE
    # ...and a channel nobody declared is refused rather than echoed — twice
    # over: ``rerouted_event`` maps an unknown one to "unavailable" at emission,
    # and the sanitizer refuses it again on the way to the ledger.
    invented = {**events[0], "data": {**events[0]["data"], "channel": "vibes"}}
    assert sanitize_audit_event(invented)["data"]["channel"] != "vibes"


# --- 4. every refusal, and the pause it leaves behind -------------------------


def _refused(db, spec, *, envelope, ledger=None, pool=None):
    result, _engine, _pool = _run(db, spec, envelope=envelope, ledger=ledger, pool=pool)
    assert result.status == "paused"
    assert result.pause_reason == ROUTE_FAULT
    assert result.route_fault is not None
    return result


def test_an_unpriced_candidate_pauses_exactly_as_before(db):
    """openrouter is priced dynamically and this machine has no override for it:
    the bill is unreadable, so the harness does not act on it."""
    ledger = _Ledger()
    result = _refused(
        db,
        _spec(),
        envelope=_envelope("openrouter/anthropic/claude-haiku-4-5"),
        ledger=ledger,
    )
    assert result.route_fault["envelope"] == "unpriced"
    assert ledger.total == 0  # a refused candidate never spends the allowance
    assert ENVELOPE_TAILS["unpriced"] in route_fault_hint(result.route_fault)


def test_a_costlier_candidate_pauses(db):
    ledger = _Ledger()
    result = _refused(db, _spec(), envelope=_envelope(DEAR_ROUTE), ledger=ledger)
    assert result.route_fault["envelope"] == "costlier"
    assert ledger.total == 0
    assert "bills MORE per token" in route_fault_hint(result.route_fault)


def test_a_gated_provider_still_needs_the_operators_opt_in(db):
    """The ``ClientPool`` gate is untouched: an envelope may not escalate into
    ``openai-codex`` (or any provider with no credential) that the operator has
    not enabled. Priced FIRST, so the refusal here is the gate and not the
    price: gpt-4o-mini is cheaper than opus on both meters."""
    ledger = _Ledger()
    pool = _Pool(ScriptedClient(lambda _p: "never"), gated=("openai",))
    result = _refused(
        db, _spec(), envelope=_envelope("openai/gpt-4o-mini"), ledger=ledger, pool=pool
    )
    assert pool.asked == ["openai"]
    assert result.route_fault["envelope"] == "gated"
    assert ledger.total == 0


def test_openai_codex_in_the_envelope_never_re_routes_a_run(db):
    """Belt and braces on the same doctrine: a subscription route is refused on
    PRICE before it ever reaches the gate, because a plan has no per-token bill
    to compare. Either refusal alone would be enough; both is the point."""
    ledger = _Ledger()
    pool = _Pool(ScriptedClient(lambda _p: "never"), gated=("openai-codex",))
    result = _refused(
        db, _spec(), envelope=_envelope("openai-codex/gpt-5.5"), ledger=ledger, pool=pool
    )
    assert result.route_fault["envelope"] == "unpriced"
    assert pool.asked == []  # never even asked to build it
    assert ledger.total == 0


def test_the_run_ceiling_cuts_the_chain(db):
    """``max_fallbacks_per_run: 0`` is an operator who wrote a list and then
    allowed nothing — refused, and told so."""
    ledger = _Ledger()
    result = _refused(
        db, _spec(), envelope=_envelope(CHEAP_ROUTE, ceiling=0), ledger=ledger
    )
    # A ceiling of zero authorizes nothing at all, which is an EMPTY envelope by
    # value — the same state as no file.
    assert result.route_fault["envelope"] == "no_envelope"
    ledger.used[DEAD_ROUTE] = 1
    result = _refused(db, _spec(), envelope=_envelope(CHEAP_ROUTE, ceiling=1), ledger=ledger)
    assert result.route_fault["envelope"] == "exhausted"
    assert "allowance is SPENT" in route_fault_hint(result.route_fault)


def test_a_second_fallback_for_the_same_dead_route_is_refused_within_one_run(db):
    """The brake the 0.0.21 resume loop lacked. The ceiling is 2 here, so what
    refuses the second attempt is the PER-ROUTE bound, not the per-run one."""
    ledger = _Ledger()
    ledger.used[DEAD_ROUTE] = ROUTE_FALLBACKS_PER_ROUTE
    result = _refused(
        db,
        _spec(),
        envelope=_envelope(CHEAP_ROUTE, DEAR_ROUTE, ceiling=5),
        ledger=ledger,
    )
    assert result.route_fault["envelope"] == "exhausted"
    assert ledger.used == {DEAD_ROUTE: ROUTE_FALLBACKS_PER_ROUTE}


def test_no_durable_brake_wired_means_no_re_route(db):
    """Fail-closed: an engine with an envelope and no ledger could not bound the
    chain, so it does not start one."""
    result = _refused(db, _spec(), envelope=_envelope(CHEAP_ROUTE), ledger=None)
    assert result.route_fault["envelope"] == "exhausted"


def test_no_client_pool_means_no_re_route(db):
    """Without a pool nothing could build the new route's client anyway — the
    re-routed leaf would die on "provider unavailable" instead of running."""
    core = _core(db, _dead)
    try:
        result = WorkflowEngine(
            core,
            budget=Budget(),
            routes=_envelope(CHEAP_ROUTE),
            route_fallback_try=_Ledger(),
        ).run(_spec(), {})
    finally:
        core.shutdown()
    assert result.status == "paused"
    assert result.route_fault["envelope"] == "gated"


# --- 5. only where the route is in the CELL KEY -------------------------------


def test_a_rigor_node_is_never_re_routed_by_the_envelope(db):
    """A ``verify`` node's strategy owns its own leaf loop and keys on routing
    only when it declares any. Re-routing below a cell whose key did not move
    would poison the cache, so it pauses exactly as it did before."""
    ledger = _Ledger()
    result = _refused(db, _spec("verify"), envelope=_envelope(CHEAP_ROUTE), ledger=ledger)
    assert result.route_fault["envelope"] == "ineligible"
    assert ledger.total == 0
    assert "only an `agent` node" in route_fault_hint(result.route_fault)


def test_a_rigor_node_that_DOES_declare_a_route_is_still_refused_in_v1(db):
    """Named, not assumed: declaring routing puts the route in that node's cell
    key, but the strategy has no re-route loop, so v1 pauses for it too."""
    ledger = _Ledger()
    result = _refused(
        db,
        _spec("verify", provider="anthropic", model=DEAD_MODEL),
        envelope=_envelope(CHEAP_ROUTE),
        ledger=ledger,
        # A declared route is built THROUGH the pool, so the pool's client is
        # the one that has to refuse for this node's route to be the dead one.
        pool=_Pool(ScriptedClient(_dead)),
    )
    assert result.route_fault["envelope"] == "ineligible"
    assert ledger.total == 0


# --- 6. anti-drift: the envelope is the OPERATOR's, never the spec's ----------


def test_a_spec_can_neither_declare_an_envelope_nor_widen_one(db):
    """Both halves of the doctrine, in one run.

    A node-level ``fallback:`` is refused by the closed field set BEFORE
    anything spawns (the validator), and a top-level ``routes:`` is read by
    nothing at all — so a spec carrying the exact envelope this feature honours
    still pauses on a dead route."""
    refused = validate_spec(
        {
            "meta": {"name": "injected"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go", "fallback": [CHEAP_ROUTE]}
            ],
        }
    )
    assert not hasattr(refused, "nodes")  # a ValidationError, not a spec
    assert "fallback" in str(refused)

    spec = validate_spec(
        {
            "meta": {"name": "injected"},
            # The shape a spec would use if this were spec vocabulary. It is not.
            "routes": {DEAD_ROUTE: {"fallback": [CHEAP_ROUTE]}},
            "max_fallbacks_per_run": 9,
            "nodes": [{"id": "a", "type": "agent", "prompt": "go"}],
        }
    )
    ledger = _Ledger()
    result = _refused(db, spec, envelope=EMPTY_ENVELOPE, ledger=ledger)
    assert result.route_fault["envelope"] == "no_envelope"
    assert ledger.total == 0


def test_every_outcome_word_has_a_remedy_and_only_one_does_not_pause():
    """Anti-drift between ``routes.py``'s vocabulary and the hint that explains
    it: a new outcome with no tail would leave a paused operator reading a
    remedy that never mentions the file they wrote."""
    assert set(ENVELOPE_TAILS) == set(OUTCOMES) - {REROUTED}
    for outcome, tail in ENVELOPE_TAILS.items():
        assert "envelope" in tail, outcome
        assert route_fault_hint({"envelope": outcome}).endswith(tail)
    # An outcome nobody declared adds nothing rather than writing prose from the
    # payload into the remedy.
    assert route_fault_hint({"envelope": "invented"}) == route_fault_hint({})


# --- 7. the DURABLE half: the brake and the route survive the process ---------


def test_the_fallback_counter_is_durable_across_processes(tmp_path):
    """The whole point of putting it in SQLite: a resumed run must not come back
    with its allowance refilled."""
    path = tmp_path / "state.db"
    first = SessionDB(str(path))
    try:
        assert first.route_fallback_try("r1", DEAD_ROUTE, 2) is True
        # ...and the SAME dead route never twice, whatever the run ceiling says.
        assert first.route_fallback_try("r1", DEAD_ROUTE, 9) is False
        assert first.route_fallbacks_used("r1") == 1
    finally:
        first.close()
    second = SessionDB(str(path))
    try:
        assert second.route_fallbacks_used("r1") == 1
        assert second.route_fallback_try("r1", DEAD_ROUTE, 9) is False
        # A DIFFERENT dead route may still buy the run's remaining slot...
        assert second.route_fallback_try("r1", CHEAP_ROUTE, 2) is True
        # ...and then the run's own ceiling closes the chain.
        assert second.route_fallback_try("r1", DEAR_ROUTE, 2) is False
        assert second.route_fallbacks_used("r1") == 2
        # Another run is another allowance.
        assert second.route_fallback_try("r2", DEAD_ROUTE, 2) is True
        assert second.route_fallbacks_used("r2") == 1
        assert second.route_fallbacks_used("unknown") == 0
    finally:
        second.close()


def test_the_new_route_is_folded_into_the_spec_a_resume_would_read():
    """In memory the re-route dies with the stretch. Folded into the persisted
    spec, it survives one — which is what keeps a resume from scheduling every
    remaining node back onto the route that died."""
    spec = {
        "meta": {"name": "persisted"},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "go"},
            {"id": "b", "type": "agent", "prompt": "other"},
        ],
    }
    reroutes = [{"node_id": "a", "provider": "anthropic", "model": CHEAP_MODEL}]
    adapted = apply_reroutes(spec, reroutes)
    assert adapted["nodes"][0] == {
        "id": "a", "type": "agent", "prompt": "go",
        "provider": "anthropic", "model": CHEAP_MODEL,
    }
    assert adapted["nodes"][1] == spec["nodes"][1]
    # Immutable: the document a concurrent reader holds is untouched.
    assert spec["nodes"][0] == {"id": "a", "type": "agent", "prompt": "go"}
    # Idempotent, because a stretch persists more than once.
    assert apply_reroutes(adapted, reroutes) == adapted
    # Nothing to fold is the original object, not a copy.
    assert apply_reroutes(spec, []) is spec
    assert apply_reroutes(spec, None) is spec
    # A refusal is skipped, never raised: the re-route already happened and its
    # fault already says so — failing the persist would throw away the line.
    assert apply_reroutes(spec, [{"node_id": "nope", "model": "x"}]) == spec
    assert apply_reroutes(spec, ["not a dict", {}]) == spec


def test_a_run_that_was_re_routed_resumes_on_the_new_route(db, tmp_path):
    """End to end through the SERVICE, with the envelope read off the disk: the
    line the run persists carries the route the envelope chose, and a resume
    replays the re-routed cell instead of re-buying it."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ROUTES_FILE).write_text(
        json.dumps({"routes": {DEAD_ROUTE: {"fallback": [CHEAP_ROUTE]}}}),
        encoding="utf-8",
    )
    seen: list[str | None] = []

    def alive(_prompt):
        return "rerouted answer"

    def factory():
        return Agent(
            model=DEAD_MODEL,
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(_dead),
        )

    pool = _Pool(_CountingClient(alive, seen))
    service = WorkflowService(
        base_child_factory=factory, db=db, home=home, client_pool=pool
    )
    spec = {"meta": {"name": "persisted-route"}, "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"}
    ]}
    try:
        run_id = service.start(spec, {})["run_id"]
        status = service.status(run_id, wait=True, timeout=20)
        assert status["status"] == "complete", status
        assert db.route_fallbacks_used(run_id) == 1
        assert len(seen) == 1
        # The LINE, read as a fresh process would read it.
        durable = service._store.load(run_id)
        assert durable.spec["nodes"][0]["provider"] == "anthropic"
        assert durable.spec["nodes"][0]["model"] == CHEAP_MODEL
        # ...and the re-route is stamped on the run for whoever certifies it.
        assert any("re-routed by operator envelope" in f for f in durable.prior_rerouted)
        # A resume replays the re-routed cell: no second leaf on any route.
        resumed = service.start(None, None, resume_run_id=run_id)
        assert "error" not in resumed, resumed
        assert service.status(run_id, wait=True, timeout=20)["status"] == "complete"
        assert len(seen) == 1
    finally:
        service.shutdown()


class _CountingClient(ScriptedClient):
    """A scripted client that records every call, so "the cache replayed it"
    can be measured rather than assumed."""

    def __init__(self, responder, seen):
        super().__init__(responder)
        self._seen = seen

    def create(self, **kwargs):
        self._seen.append(kwargs.get("model"))
        return super().create(**kwargs)
