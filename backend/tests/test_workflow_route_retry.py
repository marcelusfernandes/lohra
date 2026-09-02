"""Same-route re-spawn on a TERMINAL leaf failure, gated by ``retries`` (E1, #43).

The run that motivated this (lohra-notion-v4) carried ``retries: 1`` on all eight
nodes and retried **none** of its four provider failures: ``retries`` only ever
covered an EMPTY answer. A re-run on the same route later recovered 3 of those 4
nodes by hand — i.e. the cheapest available remedy was one the harness already
had the authorization to apply and did not.

So ``retries`` now covers two failure classes on the ONE knob the author already
writes:

- an empty answer (WF-7, unchanged) — re-spawn WITH a correction;
- a terminal provider failure on the same route (new) — re-spawn with the SAME
  prompt, same model, same provider. The prompt is not what failed.

Everything else is deliberately out, and these tests are the discriminators:
``quota_exhausted`` (the pause owns it), either timeout (the read window and the
leaf deadline own theirs), an administrative stop (a human owns it), and
``retries: 0`` (the author opted out). The cell's content hash never moves
between attempts — a re-spawn is the same cell, not a new one — and every
attempt is charged to the run's budget, so an always-failing shape is bounded.
"""

import threading

import httpx
import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.providers.errors import QUOTA_EXHAUSTED, TIMEOUT
from lohra.state import SessionDB
from lohra.workflow import quiescence
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED, Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.leaf_retry import EMPTY_OUTPUT_CORRECTION, is_retryable_failure
from lohra.workflow.schema import validate_spec
from tests.test_workflow_pipeline import ScriptedClient


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def fast_quiescence(monkeypatch):
    """These leaves block inside the client, where a cooperative cancel cannot
    reach them; the wait itself is proved in ``test_workflow_quiescence.py``."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)


class _DuckError(Exception):
    """A provider error carrying the structured signals the classifier reads."""

    def __init__(self, message, *, status_code=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _core(db, responder, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _engine(core, **kwargs):
    kwargs.setdefault("budget", Budget())
    return WorkflowEngine(core, **kwargs)


def _spec(**fields):
    node = {"id": "a", "type": "agent", "prompt": "go"}
    node.update(fields)
    return validate_spec({"meta": {"name": "e1"}, "nodes": [node]})


def _faults(result):
    return "\n".join(result.faults)


def _cells(core):
    """Record the cell_id + attempt of every leaf this run spawns."""
    seen: list[tuple[str, int]] = []
    original = core.spawn

    def spy(*args, **kwargs):
        causal = kwargs.get("causal_context")
        seen.append((getattr(causal, "cell_id", None), getattr(causal, "attempt", None)))
        return original(*args, **kwargs)

    core.spawn = spy
    return seen


def _prompts():
    """A responder factory that records the prompt it was asked and then acts."""
    seen: list[str] = []

    def make(action):
        def responder(prompt):
            seen.append(prompt)
            return action(prompt)

        return responder

    return seen, make


def _raises(exc_factory):
    def action(_prompt):
        raise exc_factory()

    return action


def _cached_rows(db, run_id):
    return db._connection.execute(
        "SELECT count(*) FROM workflow_node_cache WHERE run_id = ?", (run_id,)
    ).fetchone()[0]


# --- 1. the predicate: which deaths a same-route re-spawn could fix -----------


@pytest.mark.parametrize(
    "status, error_kind, expected",
    [
        ("error", None, True),  # the v4 case: HTTP 400, unclassified
        ("error", "server_error", True),  # any generic provider error
        ("error", QUOTA_EXHAUSTED, False),  # the pause owns it
        ("error", TIMEOUT, False),  # the read window / leaf deadline own theirs
        ("error", TOKEN_BUDGET_EXHAUSTED, False),  # a human owns the budget
        ("interrupted", None, False),  # administrative: somebody stopped it
        ("cancelled", None, False),  # administrative
        ("complete", None, False),  # not a death at all (empty/invalid answer)
        ("running", None, False),  # not terminal
        ("unknown", None, False),  # fail-closed: never re-spawn on a guess
    ],
)
def test_only_a_generic_terminal_provider_failure_is_retryable(status, error_kind, expected):
    assert is_retryable_failure(status, error_kind) is expected


def test_a_cancelled_leaf_is_never_retryable_on_the_real_core(db):
    """The predicate against the statuses a REAL core produces, not a table."""
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    try:
        engine = _engine(core)
        sub_id = core.spawn("hold")
        core.cancel(sub_id)
        assert engine.leaf_retryable(sub_id) is False
    finally:
        gate.set()
        core.shutdown()


# --- 2. the re-spawn itself --------------------------------------------------


def test_terminal_failure_respawns_on_the_same_route(db):
    seen, make = _prompts()
    core = _core(db, make(_raises(lambda: _DuckError("insufficient balance", status_code=400))))
    cells = _cells(core)
    budget = Budget()
    try:
        result = _engine(core, budget=budget).run(_spec(retries=2), {})
        assert result.outputs["a"] is None
        # Three attempts: the authored one plus the two the author paid for.
        assert len(seen) == 3
        # SAME route, SAME prompt: no correction is bolted on — the prompt is not
        # what failed (that is the empty-output retry, a different class).
        assert seen[0] == seen[1] == seen[2]
        assert EMPTY_OUTPUT_CORRECTION not in seen[0]
        # ...and the SAME cell: a re-spawn is another attempt at one cell, never
        # a new one, so a resume still recognises the work it already paid for.
        assert len({cell_id for cell_id, _ in cells}) == 1
        assert [attempt for _, attempt in cells] == [0, 1, 2]
        # Every attempt is charged. Nothing is cached: no attempt completed.
        assert budget.lifetime - budget.lifetime_remaining == 3
    finally:
        core.shutdown()


def test_the_winning_attempt_settles_the_node_and_the_cell(db):
    replies = iter([None, "REAL"])

    def action(_prompt):
        nxt = next(replies, "REAL")
        if nxt is None:
            raise _DuckError("bad gateway", status_code=502)
        return nxt

    seen, make = _prompts()
    core = _core(db, make(action))
    try:
        cache = NodeCache(db, "run-win")
        result = _engine(core, cache=cache).run(_spec(retries=2), {})
        assert result.outputs["a"] == "REAL"
        assert len(seen) == 2  # stopped the moment one attempt answered
        assert _cached_rows(db, "run-win") == 1  # the winner's output, cached once
        # ...and the run is ``degraded``, not ``complete``: a recovered node is
        # still a node that cost two leaves and hit a real provider failure.
        # Reporting it clean would hide from ``library`` exactly the run shape
        # this whole feature exists to make survivable.
        assert result.status == "degraded"
        assert "a: leaf error: bad gateway (attempt 1/3)" in result.faults
    finally:
        core.shutdown()


def test_retries_zero_never_respawns(db):
    seen, make = _prompts()
    core = _core(db, make(_raises(lambda: _DuckError("boom", status_code=500))))
    try:
        result = _engine(core).run(_spec(retries=0), {})
        assert len(seen) == 1
        assert result.outputs["a"] is None
        # The author opted out: the leaf's own cause, and not one word more.
        assert result.faults == ["a: leaf error: boom"]
    finally:
        core.shutdown()


def test_cost_stays_bounded_when_every_attempt_fails(db):
    seen, make = _prompts()
    core = _core(db, make(_raises(lambda: _DuckError("boom", status_code=500))))
    budget = Budget()
    try:
        result = _engine(core, budget=budget).run(_spec(retries=3), {})
        assert len(seen) == 4  # retries + 1, never one more
        assert budget.lifetime - budget.lifetime_remaining == 4
        assert result.outputs["a"] is None
    finally:
        core.shutdown()


# --- 3. what a re-spawn must NEVER touch -------------------------------------


def test_quota_exhaustion_never_respawns(db):
    seen, make = _prompts()
    core = _core(db, make(_raises(lambda: _DuckError("slow down", status_code=429))))
    try:
        result = _engine(core).run(_spec(retries=2), {})
        assert len(seen) == 1  # the pause owns this failure, not the retry knob
        assert result.status == "paused"
        assert result.pause_reason == QUOTA_EXHAUSTED
    finally:
        core.shutdown()


def test_provider_read_timeout_never_respawns(db):
    seen, make = _prompts()
    request = httpx.Request("POST", "https://api.example.test/v1/messages")
    core = _core(db, make(_raises(lambda: httpx.ReadTimeout("silence", request=request))))
    try:
        result = _engine(core).run(_spec(retries=2), {})
        assert len(seen) == 1
        assert result.outputs["a"] is None
        assert "provider read timeout" in _faults(result)
    finally:
        core.shutdown()


def test_leaf_timeout_never_respawns(db):
    """The OTHER timeout: the leaf blew the node's own deadline and was cancelled.

    A re-spawn here would buy the same wait again — and the cancel is cooperative,
    so the leaf it stranded may still be running when the next one starts.
    """
    gate = threading.Event()
    seen, make = _prompts()
    core = _core(db, make(lambda prompt: (gate.wait(5), "late")[1]))
    try:
        result = _engine(core).run(_spec(timeout=0.2, retries=2), {})
        assert len(seen) == 1
        assert result.outputs["a"] is None
        assert "leaf timeout after" in _faults(result)
    finally:
        gate.set()
        core.shutdown()


# --- 4. what the author reads ------------------------------------------------


def test_every_attempt_names_itself_and_the_last_says_it_gave_up(db):
    seen, make = _prompts()
    core = _core(db, make(_raises(lambda: _DuckError("insufficient balance", status_code=400))))
    try:
        result = _engine(core).run(_spec(retries=2), {})
        assert result.faults == [
            "a: leaf error: insufficient balance (attempt 1/3)",
            "a: leaf error: insufficient balance (attempt 2/3)",
            "a: leaf error: insufficient balance (attempt 3/3)",
            "a: leaf failed on the same route after 3 attempt(s); re-spawns exhausted",
        ]
        assert len(seen) == 3
    finally:
        core.shutdown()


def test_empty_output_still_gets_its_correction_and_its_own_verdict(db):
    """The other retry class is untouched: correction bolted on, its own fault."""
    seen, make = _prompts()
    core = _core(db, make(lambda _prompt: "   "))
    try:
        result = _engine(core).run(_spec(retries=1), {})
        assert len(seen) == 2
        assert EMPTY_OUTPUT_CORRECTION in seen[1]
        assert "empty output after retry (2 attempt(s))" in _faults(result)
        assert "re-spawns exhausted" not in _faults(result)
    finally:
        core.shutdown()


# --- 5. required: aborts AFTER the retries, never before ---------------------


def test_required_aborts_only_once_the_respawns_are_exhausted(db):
    seen, make = _prompts()
    core = _core(db, make(_raises(lambda: _DuckError("boom", status_code=500))))
    spec = validate_spec(
        {
            "meta": {"name": "req"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go", "retries": 1, "required": True},
                {"id": "b", "type": "agent", "prompt": "after ${a}"},
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {})
        assert len(seen) == 2  # BOTH attempts ran before the run gave up on 'a'
        assert result.required_failure == "a"
        assert "b" not in result.outputs  # never spawned: skipped, not attempted
        assert "re-spawns exhausted" in _faults(result)
    finally:
        core.shutdown()


# --- 6. the steer path: a corrected leaf that then DIES ----------------------
#
# ALTO-1. ``_collect_validate`` numbers its schema-correction rounds with a local
# ``attempt``, and E1 gave the method a parameter of the same name carrying
# ``(i, n)``. The loop variable SHADOWED it, so the second ``note_leaf_failure``
# — the one reached when the steered turn dies — handed an int to code that
# indexes a tuple. The TypeError escaped as an engine fault: the real cause was
# replaced by a harness crash, the attempt was never accounted, and a leaf the
# pause had cancelled stopped being discounted as administrative. It fires for
# EVERY caller with a schema, ``attempt=None`` ones included.


def _invalid_then_dead():
    """Answer unparseable JSON once, then die on the steered turn."""
    state = {"turns": 0}

    def action(_prompt):
        state["turns"] += 1
        if state["turns"] == 1:
            return "definitely not the json you asked for"
        raise _DuckError("bad gateway", status_code=502)

    return action


def test_a_steered_leaf_that_dies_reports_the_leaf_cause_not_a_harness_crash(db):
    """(a) an ``agent`` node — the caller that DOES pass ``attempt``."""
    seen, make = _prompts()
    core = _core(db, make(_invalid_then_dead()))
    spec = validate_spec(
        {
            "meta": {"name": "e1-steer"},
            "schemas": {"Verdict": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
            "nodes": [
                {
                    "id": "a", "type": "agent", "prompt": "go",
                    "schema_ref": "Verdict", "retries": 0,
                }
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {})
        assert result.outputs["a"] is None
        assert result.engine_faults == 0  # the harness did not crash on itself
        assert _faults(result) == "a: leaf error: bad gateway"
    finally:
        core.shutdown()


def test_the_same_holds_for_a_caller_that_passes_no_attempt(db):
    """(b) a ``verify`` node — reaches the identical steer path with attempt=None."""
    core = _core(db, _invalid_then_dead())
    spec = validate_spec(
        {
            "meta": {"name": "e1-steer-rigor"},
            "nodes": [{"id": "v", "type": "verify", "finding": "claim", "skeptics": 1}],
        }
    )
    try:
        result = _engine(core).run(spec, {})
        assert result.engine_faults == 0
        assert "v: leaf error: bad gateway" in _faults(result)
        assert "TypeError" not in _faults(result)
    finally:
        core.shutdown()


# --- 7. the terminal class is OPT-IN (Q1) -----------------------------------
#
# The default ``retries: 1`` was authored for the empty-output case and predates
# E1 by a whole campaign; letting it silently start paying for provider deaths
# too would double the bill of every already-written spec without its author
# ever asking. So the terminal class is bought EXPLICITLY — the same predicate
# ``max_iterations`` uses for its cell identity: the field is in ``node.fields``
# or the knob was never asked for.


def test_an_undeclared_retries_never_buys_a_terminal_respawn(db):
    seen, make = _prompts()
    core = _core(db, make(_raises(lambda: _DuckError("boom", status_code=500))))
    try:
        result = _engine(core).run(_spec(), {})  # no `retries` field at all
        assert len(seen) == 1
        assert result.outputs["a"] is None
        assert result.faults == ["a: leaf error: boom"]  # no series, no verdict
    finally:
        core.shutdown()


def test_the_default_still_covers_an_empty_answer_with_no_retries_declared(db):
    """The default is untouched for the class it was written for."""
    seen, make = _prompts()
    core = _core(db, make(lambda _prompt: ""))
    try:
        _engine(core).run(_spec(), {})
        assert len(seen) == 2  # default retries: 1, exactly as before E1
    finally:
        core.shutdown()


def test_declaring_the_same_value_the_default_already_had_buys_the_respawn(db):
    """`retries: 1` and an unset field resolve to the same NUMBER — what differs
    is that one of them is an author asking for it."""
    seen, make = _prompts()
    core = _core(db, make(_raises(lambda: _DuckError("boom", status_code=500))))
    try:
        _engine(core).run(_spec(retries=1), {})
        assert len(seen) == 2
    finally:
        core.shutdown()
