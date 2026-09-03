"""A dead ROUTE pauses the run instead of degrading in silence (#43, opção C).

The measured cost this closes: in ``lohra-notion-v4`` the Anthropic balance died
mid-run and the harness kept scheduling — four more nodes onto a route already
known to be dead, and 55% of the run's tokens spent outside any surviving cell.
Every death wrote its fault, so nothing was hidden; but ``degraded`` is a verdict
read after the money is gone, while a pause is the same information delivered
while the finished cells are still in the resume cache.

These tests are the DISCRIMINATORS for a trigger that must stay narrow. Stopping
a healthy run on one transient 502 is a worse failure than the one being fixed,
so exactly two shapes pause — a refused credential (deterministic within the run:
the client is cached per route) and a DECLARED series of same-route re-spawns
that exhausted — and a single generic death on a node that never wrote
``retries`` still faults, nulls and degrades exactly as it did before.

Nothing here grants new authority: no re-route, no ``resume_at``, no auto-resume
(that allow-list stays quota-only, pinned in ``test_workflow_pivot.py``).
"""

from __future__ import annotations

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.providers.errors import AUTH_FAILED, QUOTA_EXHAUSTED, TIMEOUT
from lohra.state import SessionDB
from lohra.workflow import quiescence
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED, Budget
from lohra.agent.types import Usage
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.leaf_retry import run_leaf_with_retries
from lohra.workflow.route_fault import (
    MAX_FAULT_CAUSE_CHARS,
    ROUTE_FAULT,
    ROUTE_FAULT_HINT,
    route_fault_payload,
    route_label,
    should_pause_on_route_fault,
)
from lohra.workflow.runstate_store import (
    RunStateStore,
    carried_faults,
    durable_rollup,
    pause_fields,
)
from lohra.workflow.schema import validate_spec
from tests.test_workflow_pipeline import ScriptedClient


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def fast_quiescence(monkeypatch):
    """A pause cancels what is in flight; never wait a real quiescence window for
    leaves that are already done (the wait itself is proved elsewhere)."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)


class _DuckError(Exception):
    """A provider error carrying the structured signals the classifier reads."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class _Node:
    """The only thing the predicate reads off a node: whether ``retries`` is
    written. Not a real Node on purpose — the predicate must be judgeable
    without an engine, a core or a spec."""

    def __init__(self, fields):
        self.id = "n"
        self.fields = fields


def _core(db, responder):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=4)


def _spec(**fields):
    node = {"id": "a", "type": "agent", "prompt": "go"}
    node.update(fields)
    return validate_spec(
        {
            "meta": {"name": "route-fault"},
            # 'b' is deliberately INDEPENDENT of 'a': a downstream node would
            # null on the upstream null and prove nothing about scheduling. This
            # one is free to run, so "it never ran" means the run really stopped.
            "nodes": [node, {"id": "b", "type": "agent", "prompt": "unrelated work"}],
        }
    )


def _raises(exc_factory):
    def action(_prompt):
        raise exc_factory()

    return action


def _faults(result):
    return "\n".join(result.faults)


# --- 1. the predicate: which deaths are evidence about the ROUTE --------------


_DECLARED = _Node({"retries": 2})
_UNDECLARED = _Node({})


@pytest.mark.parametrize(
    "node, status, error_kind, declared, exhausted, expected",
    [
        # (a) a refused credential. Deterministic within the run, whatever the
        # node declared: the same cached client answers every later leaf.
        (None, "error", AUTH_FAILED, False, False, True),
        (_UNDECLARED, "error", AUTH_FAILED, False, False, True),
        # (b) a DECLARED series that spent every attempt on one route.
        (_DECLARED, "error", None, True, True, True),
        (_DECLARED, "error", "server_error", True, True, True),
        # ...and the ways branch (b) must NOT fire.
        (_DECLARED, "error", None, True, False, False),  # retries: 0 — no series ran
        (_UNDECLARED, "error", None, False, True, False),  # the v4 shape: no retries
        (_UNDECLARED, "error", None, True, True, False),  # caller says series, node did not
        (_DECLARED, "error", QUOTA_EXHAUSTED, True, True, False),  # the quota pause owns it
        (_DECLARED, "error", TIMEOUT, True, True, False),  # the timeout knobs own it
        (_DECLARED, "error", TOKEN_BUDGET_EXHAUSTED, True, True, False),  # a human owns it
        # Not a leaf that died carrying an exception at all.
        (_DECLARED, "complete", None, True, True, False),
        (_DECLARED, "cancelled", None, True, True, False),
        (_DECLARED, "interrupted", None, True, True, False),
        (_DECLARED, "running", None, True, True, False),
        (_DECLARED, None, None, True, True, False),
        (_DECLARED, "unknown", None, True, True, False),  # fail-closed on a guess
    ],
)
def test_only_route_evidence_pauses(node, status, error_kind, declared, exhausted, expected):
    assert (
        should_pause_on_route_fault(node, status, error_kind, declared, exhausted) is expected
    )


def test_the_payload_names_the_route_and_bounds_the_cause():
    payload = route_fault_payload(
        node_id="a", provider="anthropic", model="opus", error_kind=AUTH_FAILED,
        cause="x" * (MAX_FAULT_CAUSE_CHARS + 500),
    )
    assert payload["node_id"] == "a"
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "opus"
    assert payload["error_kind"] == AUTH_FAILED
    # A stack trace lands in a durable blob the agent reads back: bounded there
    # for the same reason it is bounded in the fault text.
    assert len(payload["cause"]) == MAX_FAULT_CAUSE_CHARS


def test_a_route_with_a_missing_half_still_says_something():
    """A node that named no model ran on the run's default; "None/None" would
    name nothing at all."""
    assert route_label("anthropic", "opus") == "anthropic/opus"
    assert route_label("anthropic", None) == "anthropic"
    assert route_label(None, "opus") == "opus"
    assert route_label(None, None) == "the run's default route"


# --- 2. (a) the refused credential --------------------------------------------


def test_a_refused_credential_pauses_and_stops_scheduling(db):
    calls: list[str] = []

    def responder(prompt):
        calls.append(prompt)
        raise _DuckError("invalid x-api-key", status_code=401)

    core = _core(db, responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(_spec(), {})
        assert len(calls) == 1  # 'b' was never scheduled onto the dead route
        assert result.status == "paused"
        assert result.pause_reason == ROUTE_FAULT
        # Nothing about a refused credential fixes itself with time.
        assert result.retry_after is None
        assert result.route_fault == {
            "node_id": "a",
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "error_kind": AUTH_FAILED,
            "cause": result.route_fault["cause"],
        }
        assert "provider refused this route's credential" in result.route_fault["cause"]
        # ONE fault, and it is the PAUSE's own — so a later stretch discounts it
        # instead of carrying "this spec is broken" forever.
        assert len(result.faults) == 1
        assert result.pause_fault == result.faults[0]
        assert "invalid x-api-key" in result.faults[0]  # the provider's own words
        assert "anthropic/claude-opus-4-8" in result.faults[0]  # ...and the dead route
        # A route payload must never arrive dressed as a human gate.
        assert result.checkpoint is None
    finally:
        core.shutdown()


# --- 3. (b) the declared series that exhausted --------------------------------


def test_an_exhausted_declared_series_pauses_with_the_route_named(db):
    core = _core(db, _raises(lambda: _DuckError("insufficient balance", status_code=400)))
    try:
        result = WorkflowEngine(core, budget=Budget()).run(_spec(retries=2), {})
        assert result.status == "paused"
        assert result.pause_reason == ROUTE_FAULT
        assert result.route_fault["node_id"] == "a"
        assert result.route_fault["model"] == "claude-opus-4-8"
        assert "re-spawns exhausted" in result.route_fault["cause"]
        assert "b" not in result.outputs  # the run stopped at the dead route
    finally:
        core.shutdown()


# --- 4. what must NOT pause ---------------------------------------------------


def test_a_single_death_without_declared_retries_still_only_degrades(db):
    """The transient-502 guard. Without a declared series there is no evidence
    about the ROUTE — one call was unlucky — so this is the behaviour the harness
    has always had: fault, null, keep going, ``degraded``."""
    calls: list[str] = []

    def responder(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise _DuckError("bad gateway", status_code=502)
        return "second node is fine"

    core = _core(db, responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(_spec(), {})
        assert result.status == "degraded"
        assert result.pause_reason is None
        assert result.route_fault is None
        assert result.outputs["a"] is None
        # The whole point: the run was NOT stopped. 'b' still ran.
        assert len(calls) == 2
        assert "bad gateway" in _faults(result)
    finally:
        core.shutdown()


def test_a_transient_failure_a_declared_retry_recovers_never_pauses(db):
    """A series that RECOVERED is the opposite of evidence that the route died —
    the second attempt reached the same provider and got an answer."""
    calls: list[str] = []

    def responder(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise _DuckError("bad gateway", status_code=502)
        return "recovered"

    core = _core(db, responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(_spec(retries=1), {})
        assert result.pause_reason is None
        assert result.status != "paused"
        assert result.outputs["a"] == "recovered"
        assert len(calls) == 3  # a's two attempts + b
        assert "re-spawns exhausted" not in _faults(result)
    finally:
        core.shutdown()


# --- 5. what the agent reads back ---------------------------------------------


def test_the_pause_remedy_states_the_sup04_boundary_and_names_the_route():
    fields = pause_fields(
        "paused", ROUTE_FAULT, None, 0, None,
        route_fault={"node_id": "a", "provider": "anthropic", "model": "opus",
                     "error_kind": AUTH_FAILED, "cause": "refused"},
    )
    assert fields["reason"] == ROUTE_FAULT
    assert fields["resume_at"] is None  # nothing wakes this run on its own
    assert fields["route"]["provider"] == "anthropic"
    assert fields["hint"] == ROUTE_FAULT_HINT
    # The SUP-04 boundary, stated where the agent will actually read it.
    for token in ("SAME provider", "credential/billing route", "HUMAN", "resume_run_id"):
        assert token in fields["hint"]
    # It must never read as a licence to trade up.
    assert "costlier" in fields["hint"]


def test_the_dead_route_survives_the_process_that_hit_it(db):
    """A pause is only useful if the NEXT process can still say what died.

    The payload rides the run's durable line (WF-29), so a ``workflow_status``
    from a fresh process — the CLI, a later turn — reads the same route, the
    same cause and the same remedy the run that hit it would have reported."""
    store = RunStateStore(db, holder="route-fault-test", clock=lambda: 1000.0)
    payload = {
        "node_id": "target", "provider": "anthropic", "model": "opus",
        "error_kind": AUTH_FAILED, "cause": "the provider refused this credential",
    }
    assert store.save(
        run_id="r1", name="a run", status="paused", pause_reason=ROUTE_FAULT,
        route_fault=payload, resume_at=None, attempts=0,
    )

    row = RunStateStore(db, holder="another-process", clock=lambda: 1000.0).load("r1")
    assert row.route_fault == payload
    out = durable_rollup(row, spent_total=0, stale=False)
    assert out["reason"] == ROUTE_FAULT
    assert out["route"] == payload
    assert out["hint"] == ROUTE_FAULT_HINT
    assert out["resume_at"] is None


class _FakeEngine:
    """Just enough engine to drive ``run_leaf_with_retries`` off-core.

    Its ``note_route_fault`` always DECLINES — the shape of "another pause
    already owns this run", which a real core cannot be made to produce on
    demand without racing two failures against each other."""

    def __init__(self):
        self.faults: list[str] = []
        self.respawns = 0
        self.stopped = False
        self._n = 0

    def causal_context(self, **kwargs):
        return None

    def spawn_leaf(self, _text, **_kwargs):
        self._n += 1
        return f"sub-{self._n}"

    def collect_validated(self, _node, _sub_id, **_kwargs):
        return None  # every attempt dies

    def leaf_retryable(self, _sub_id):
        return True

    def leaf_result(self, _sub_id):
        return {"status": "error", "error_kind": None, "provider": "p", "model": "m"}

    def leaf_cost(self, _sub_id):
        return Usage()

    def note_route_fault(self, *_args, **_kwargs):
        return False

    def count_leaf_respawn(self):
        self.respawns += 1

    def mark_recovered(self, _sub_ids):
        raise AssertionError("no winner in this series")

    def mark_route_fault_caused(self, _sub_ids):
        # No pause latched, so nothing is retired — the real engine checks the
        # reason itself and this fake never gets one.
        return None

    def record_fault(self, message):
        self.faults.append(message)


def test_a_declined_pause_never_swallows_the_verdict():
    """The fallback contract: whoever does not pause still has to be told.

    A pause that loses the latch — another cause got there first — must leave
    the exhaustion verdict recorded as an ordinary fault. Silently returning
    would delete the only line naming what the series did, which is the
    fail-closed rule M1/M2 bought and this slice may not spend."""
    engine = _FakeEngine()
    node = _Node({"retries": 1})
    node.id = "a"
    output, _cost = run_leaf_with_retries(
        engine, node, "go", None, None, cell_id="cell"
    )
    assert output is None
    assert engine.faults == [
        "a: leaf failed on the same route after 2 attempt(s); re-spawns exhausted"
    ]
    assert engine.respawns == 1  # the cost is counted whoever writes the verdict


def test_a_rigor_node_on_the_dead_route_pauses_too(db):
    """Scope, pinned rather than asserted.

    The exhausted-series trigger is ``agent``-only by construction (only
    ``run_leaf_with_retries`` runs a declared series), but ``auth_failed``
    travels through ``note_leaf_failure``, which every rigor leaf reaches via
    ``collect_with_schema`` and every pipeline stage via ``_stage_done``. A
    ``verify`` whose skeptics meet a refused credential must stop the run, not
    quietly refute nothing."""
    calls: list[str] = []

    def responder(prompt):
        calls.append(prompt)
        raise _DuckError("invalid x-api-key", status_code=401)

    spec = validate_spec(
        {
            "meta": {"name": "rigor-route-fault"},
            "nodes": [
                {"id": "check", "type": "verify", "finding": "a claim", "skeptics": 2},
                {"id": "after", "type": "agent", "prompt": "unrelated work"},
            ],
        }
    )
    core = _core(db, responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.status == "paused"
        assert result.pause_reason == ROUTE_FAULT
        assert result.route_fault["node_id"] == "check"
        assert "after" not in result.outputs  # the run stopped before the next node
    finally:
        core.shutdown()


def test_a_mixed_series_that_ends_in_auth_retires_its_earlier_attempts(db):
    """The other door into the discount, reached from the EARLY return.

    A series that starts with an ordinary death and then meets a refused
    credential never reaches the exhaustion verdict: ``auth_failed`` buys no
    re-spawn, so the loop gives up where it stands. The pause is still the
    verdict, so attempt 1's numbered fault is still the pause's evidence — and
    a stretch that ends there must not seal ``prior_degraded`` on it."""
    calls: list[str] = []

    def responder(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise _DuckError("bad gateway", status_code=502)
        raise _DuckError("invalid x-api-key", status_code=401)

    core = _core(db, responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(_spec(retries=2), {})
        assert len(calls) == 2  # the 401 ended the series; the third attempt never ran
        assert result.status == "paused"
        assert result.pause_reason == ROUTE_FAULT
        assert result.route_fault["error_kind"] == AUTH_FAILED
        # Attempt 1's numbered fault is retired into the pause; attempt 2's IS
        # the pause fault (auth is never numbered — it buys no re-spawn).
        assert result.pause_faults == [result.faults[0]]
        assert "(attempt 1/3)" in result.faults[0]
        assert result.pause_fault == result.faults[1]
        assert carried_faults([], result) == (result.faults, False)
        assert result.leaf_respawns == 1  # the extra leaf is still on the bill
    finally:
        core.shutdown()
