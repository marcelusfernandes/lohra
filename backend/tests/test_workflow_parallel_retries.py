"""`parallel.retries` — opt-in fresh re-spawns of a DEAD branch (H7, #77).

#72 makes the fail-closed guard in ``test_workflow_failclosed.py`` correct — a
`parallel` with a dead branch must never let a reduce node read the hole. But
it left the author with no in-spec remedy for a transient blip: the ONLY way
to recover a dead branch was a full resume that re-spawns the ENTIRE fan-out.
H7 proposes `parallel.retries`, same opt-in doctrine as `agent.retries`
(`leaf_retry.py`): a dead branch is re-spawned fresh, up to N times, before
the guard ever sees a hole.

Split out of `test_workflow_failclosed.py` once this slice's tests pushed
that file past its 800-line budget (issue #77's adversarial review, L3) —
``_core``, ``_run``, ``_faults``, ``_parallel_with_a_dead_middle_branch``,
``_BRANCH_PROMPTS`` and ``_dying_middle`` are the SAME fixtures #72's own
tests use, imported rather than duplicated so the two files' shared scenario
can't drift apart.
"""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.parallel_retry import respawn_dead_branch
from lohra.workflow.schema import ValidationError, validate_spec
from tests.test_workflow_failclosed import (
    _BRANCH_PROMPTS,
    _core,
    _dying_middle,
    _parallel_with_a_dead_middle_branch,
    _faults,
    _run,
)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _dying_beta_once(seen: list[str]):
    """Branch beta dies on its FIRST attempt and answers on its second — the
    exact experiment issue #77 asks for. Keyed on prompt text (stateful
    counter), because a fresh re-spawn carries the SAME prompt verbatim
    (unlike the agent's empty-output correction, a dead branch gets no hint
    that it is being asked again)."""
    calls = {"beta": 0}

    def responder(prompt: str) -> str:
        seen.append(prompt)
        if "beta" in prompt:
            calls["beta"] += 1
            if calls["beta"] == 1:
                raise RuntimeError("branch beta died once")
        return f"answer to {prompt}"

    return responder


def test_h7_parallel_retries_respawns_a_dead_branch_fresh(db):
    """The experiment issue #77 asks for, BEFORE any implementation exists:
    `parallel` of 3 branches, the middle one dies on its 1st attempt and
    answers on its 2nd, a reduce node over `${p}`, and `retries: 1` declared
    on the `parallel` node.

    Desired end state (H7's proposed fix): the dead branch gets ONE fresh
    re-spawn, the panel comes back whole, and the reduce node runs normally —
    `leaf_respawns` counts the one extra leaf it cost.

    RED (today, main 0.0.25 / integration/wave10.1): `parallel` has no
    `retries` field in `NODE_SPECS` (only `branches` is registered), so
    `validate_spec` refuses the spec outright with an `unknown_field` issue —
    the branch never even gets a first attempt, let alone a second. See
    scratchpad/w101/red-77.txt for the captured failure."""
    seen: list[str] = []
    core = _core(db, _dying_beta_once(seen))
    try:
        spec_dict = _parallel_with_a_dead_middle_branch("${p}")
        spec_dict["nodes"][0]["retries"] = 1
        result = _run(core, spec_dict)
        assert result.outputs["p"] == [f"answer to {b}" for b in _BRANCH_PROMPTS]
        assert result.outputs["r"] is not None
        assert "upstream null" not in _faults(result)
        assert result.leaf_respawns == 1
        assert seen.count("branch beta") == 2  # first death + the re-spawn
        # Retired, never erased (Q2): the death is still readable in `faults`.
        assert any("branch beta died once" in f for f in result.faults), _faults(result)
        assert any("branch beta died once" in f for f in result.recovered_faults), (
            result.recovered_faults
        )
        # The headline claim: a RECOVERED series does not seal `degraded` (Q2)
        # — the sealed status must read `complete`, not just "no fault leaked".
        assert result.status == "complete"
    finally:
        core.shutdown()


def test_h7_parallel_retries_exhausted_still_refuses_the_reduce(db):
    """Both attempts of the middle branch die: `retries: 1` bought one
    re-spawn, not a guarantee. Today's #72 behaviour holds — the hole is
    never hidden from the reduce node — and NOTHING is retired as recovered,
    because there was no winner to retire it for."""

    def _dying_beta_always(prompt: str) -> str:
        if "beta" in prompt:
            raise RuntimeError("branch beta always dies")
        return f"answer to {prompt}"

    seen: list[str] = []
    core = _core(db, lambda p: seen.append(p) or _dying_beta_always(p))
    try:
        spec_dict = _parallel_with_a_dead_middle_branch("${p}")
        spec_dict["nodes"][0]["retries"] = 1
        result = _run(core, spec_dict)
        assert result.outputs["p"][1] is None
        assert result.outputs["r"] is None
        assert "r: upstream null inside ${p}[1]" in _faults(result), _faults(result)
        assert result.leaf_respawns == 1  # the ONE re-spawn `retries: 1` bought
        assert result.recovered_faults == []  # no winner: nothing to retire
        assert seen.count("branch beta") == 2  # first attempt + the re-spawn
    finally:
        core.shutdown()


def test_h7_parallel_without_retries_is_byte_identical_to_72(db):
    """`retries` absent (or `0`) must not change #72's own behaviour at all —
    same fault text, same leaf_respawns, same cell identity as before this
    field existed."""
    seen: list[str] = []
    core = _core(db, _dying_middle(seen))
    try:
        result = _run(core, _parallel_with_a_dead_middle_branch("${p}"))
        assert result.outputs["r"] is None
        assert "r: upstream null inside ${p}[1]" in _faults(result), _faults(result)
        assert result.leaf_respawns == 0
        assert seen.count("branch beta") == 1  # never re-spawned
    finally:
        core.shutdown()


def test_h7_parallel_cell_identity_unchanged_when_retries_is_absent():
    """A spec that never writes `retries` must hash EXACTLY like the formula
    `run_parallel` used before this field existed (`cell_hash(id, "parallel",
    prompts)`, no extra component) — a pre-existing persisted row still HITs
    on a resume after this upgrade. Writing the field at all — even `0` — is a
    declaration and DOES move the identity (mirrors `max_iterations` on
    `agent`, `_node_configure`): a resume that adds or removes the field must
    re-run the cell, never silently replay a row computed without it."""
    from lohra.workflow.strategies import _parallel_cell_extra

    assert _parallel_cell_extra({}) == ()  # byte-identical to the pre-#77 formula
    assert _parallel_cell_extra({"retries": 0}) == (0,)  # explicit, still moves it
    assert _parallel_cell_extra({"retries": 2}) == (2,)


def test_h7_medium1_leaf_count_stays_nominal_width_not_inflated_by_dead_respawns(db):
    """MEDIUM-1 (#77 adversarial review): a dead re-spawn is a zero-cost entry
    that must never inflate #71's ``leaves`` denominator (`spend.py`,
    `cache.NodeCache.cost_count`) — that pushes a resume's `est_leaf_cost`
    LOWER, the UNSAFE direction (`spend.seed_charges` deliberately biases the
    average HIGH, never low, so the token gate pauses early rather than
    late). One branch needed one re-spawn to recover; the cached cell's
    `leaves` column must still read 3 (the panel's AUTHORED width), never 4
    (3 originals + 1 re-spawn) — `leaves_cost` still prices the whole series
    honestly, only the denominator stays nominal."""
    from lohra.workflow.cache import NodeCache

    seen: list[str] = []
    core = _core(db, _dying_beta_once(seen))
    run_id = "run-medium1"
    cache = NodeCache(db, run_id)
    try:
        spec_dict = _parallel_with_a_dead_middle_branch("${p}")
        spec_dict["nodes"] = spec_dict["nodes"][:1]  # bare parallel, no reduce needed
        spec_dict["nodes"][0]["retries"] = 1
        spec = validate_spec(spec_dict)
        assert not hasattr(spec, "issues"), getattr(spec, "message", "")
        result = WorkflowEngine(
            core, budget=Budget(), cache=cache, run_id=run_id
        ).run(spec, {})
        assert result.outputs["p"] == [f"answer to {b}" for b in _BRANCH_PROMPTS]
        assert result.leaf_respawns == 1
        assert cache.cost_count() == 3  # nominal width — NOT 4
    finally:
        core.shutdown()


class _AuthDuckError(Exception):
    """A provider error carrying the one structured signal the classifier reads
    as an auth failure (`providers/errors.py`'s `status_code` reader) — the
    same shape `test_workflow_route_fault_pause.py`'s `_DuckError` uses."""

    def __init__(self, message, *, status_code=401):
        super().__init__(message)
        self.status_code = status_code


def test_h7_parallel_retries_pause_retires_the_first_deaths_fault(db):
    """The RE-SPAWN itself dies on an auth-refused route: `note_leaf_failure`
    latches a `route_fault` pause on the `parallel` node UNCONDITIONALLY
    (`should_pause_on_route_fault`'s `auth_failed` branch never even looks at
    `retries` — see `route_fault.py`), independent of anything this feature's
    own bookkeeping decides. The FIRST attempt's numbered fault — retryable,
    so it was stamped `(attempt 1/2)` and remembered by sub_id — must be
    retired into that pause (`mark_route_fault_caused`) rather than left to
    count as an ordinary, un-paused degradation the pause did not cause."""
    calls = {"beta": 0}

    def responder(prompt: str) -> str:
        if "beta" in prompt:
            calls["beta"] += 1
            if calls["beta"] == 1:
                raise RuntimeError("branch beta died once")
            raise _AuthDuckError("invalid x-api-key")
        return f"answer to {prompt}"

    core = _core(db, responder)
    try:
        spec_dict = _parallel_with_a_dead_middle_branch("${p}")
        spec_dict["nodes"][0]["retries"] = 1
        result = _run(core, spec_dict)
        assert result.status == "paused"
        assert result.pause_reason == "route_fault"
        assert result.route_fault["node_id"] == "p"
        assert any("branch beta died once" in f for f in result.pause_faults), (
            result.pause_faults
        )
    finally:
        core.shutdown()


@pytest.mark.parametrize("bad_retries", [4, -1, True, "1", 1.5])
def test_h7_parallel_retries_out_of_range_refuses_the_spec(bad_retries):
    """Same syntax/validation as `agent.retries` (`_validate_lifecycle` is
    generic over node type already) — `4` exceeds `MAX_NODE_RETRIES`, `-1` is
    negative, `True` is a bool (not an int, even though `isinstance(True, int)`
    is true in Python), and a string/float never was an int to begin with."""
    spec_dict = _parallel_with_a_dead_middle_branch("${p}")
    spec_dict["nodes"][0]["retries"] = bad_retries
    result = validate_spec(spec_dict)
    assert isinstance(result, ValidationError), result
    assert any(
        issue.field == "retries" and issue.node_id == "p" for issue in result.issues
    ), result.message


# --- L1 (#77 adversarial review): unit-test the two early-exit doors -------
#
# Both doors are exercised by a MINIMAL fake engine — the exact surface
# `respawn_dead_branch` reads — rather than a full orchestration core: the
# question here is purely "does this door fire and refuse to spawn", which a
# real leaf's timing/threading would only make slower and flakier to pin.

_NODE = type("Node", (), {"id": "p"})()


class _DoorFakeEngine:
    """Answers only what `respawn_dead_branch` asks; asserts if it is ever
    asked to actually spawn or collect a leaf — the whole point of both doors
    below is that neither one ever reaches that far."""

    def __init__(self, *, retryable: bool, stopped: bool = False):
        self._retryable = retryable
        self.stopped = stopped
        self.faults: list[str] = []
        self.route_fault_calls: list[tuple] = []

    def leaf_retryable(self, sub_id):
        return self._retryable

    def record_fault(self, message):
        self.faults.append(message)

    def mark_route_fault_caused(self, node_id, dead):
        self.route_fault_calls.append((node_id, list(dead)))

    def spawn_leaf(self, *a, **k):  # pragma: no cover — must never fire
        raise AssertionError("a refused door must never spawn a leaf")

    def count_leaf_respawn(self):  # pragma: no cover — must never fire
        raise AssertionError("a refused door must never count a re-spawn")

    def collect_with_schema(self, *a, **k):  # pragma: no cover — must never fire
        raise AssertionError("a refused door must never collect a leaf")

    def causal_context(self, **k):
        return None


def test_h7_parallel_first_death_non_retryable_never_respawns():
    """A branch dying with a status a re-spawn could never fix (quota/auth/
    timeout/token-budget, `leaf_retry.is_retryable_failure`) is fed straight
    to `mark_route_fault_caused` and never re-spawned — `respawn_dead_branch`'s
    FIRST door, symmetrical with the agent's own terminal class."""
    engine = _DoorFakeEngine(retryable=False)
    output, dead, spawned = respawn_dead_branch(
        engine, _NODE, "chash", "branch beta", 1, "sub-1", attempts_total=2
    )
    assert output is None
    assert dead == ["sub-1"]
    assert spawned == []
    assert engine.route_fault_calls == [("p", ["sub-1"])]
    # No NEW fault here — `note_leaf_failure` already wrote this death's own
    # fault before `respawn_dead_branch` was ever called.
    assert engine.faults == []


def test_h7_parallel_stop_mid_series_never_respawns_and_says_why():
    """A pause or a cancel landing between the first death and the re-spawn
    stops the series — a numbered `stopped_fault`, never a silent drop and
    never another leaf bought after the run said stop."""
    engine = _DoorFakeEngine(retryable=True, stopped=True)
    output, dead, spawned = respawn_dead_branch(
        engine, _NODE, "chash", "branch beta", 1, "sub-1", attempts_total=3
    )
    assert output is None
    assert dead == ["sub-1"]
    assert spawned == []
    assert any("run stopped after attempt" in f for f in engine.faults), engine.faults


# --- HIGH-1 (#77 adversarial review): a budget refusal mid-retry must not ---
# --- abort the barrier and orphan the branches it already spawned ----------


def test_h7_lifetime_exhausted_mid_retry_collects_and_charges_every_branch(db):
    """3 branches claim the WHOLE lifetime on their first (concurrent) spawn;
    the middle one dies once and needs a re-spawn, which finds no lifetime
    left (`Budget(lifetime=3)`, a pure per-run SPAWN COUNT — unaffected by
    what anything costs). Before the fix, `spawn_leaf`'s `LifetimeExhausted`
    escaped `respawn_dead_branch` uncaught, aborting `run_parallel`'s collect
    loop — the THIRD branch (already spawned, running independently) was
    never collected or charged. After the fix: alpha and gamma both answer
    and are collected, the re-spawn is refused with a fault + a cap trip,
    `leaf_respawns` stays 0 (nothing was ever spawned for the refused
    re-spawn), and the branch's own slot in `outputs["p"]` is the only hole —
    never a run-ending crash."""
    calls = {"beta": 0}

    def responder(prompt: str) -> str:
        if "beta" in prompt:
            calls["beta"] += 1
            if calls["beta"] == 1:
                raise RuntimeError("branch beta died once")
        return f"answer to {prompt}"

    core = _core(db, responder)
    try:
        raw = {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "p", "type": "parallel", "retries": 1, "branches": [
                    {"type": "agent", "prompt": "branch alpha"},
                    {"type": "agent", "prompt": "branch beta"},
                    {"type": "agent", "prompt": "branch gamma"},
                ]},
            ],
        }
        spec = validate_spec(raw)
        assert not hasattr(spec, "issues"), getattr(spec, "message", "")
        result = WorkflowEngine(core, budget=Budget(lifetime=3)).run(spec, {})
        # All THREE original branches were spawned and none is orphaned:
        # alpha and gamma answered, beta's slot is the only hole.
        assert result.outputs["p"][0] == "answer to branch alpha"
        assert result.outputs["p"][1] is None
        assert result.outputs["p"][2] == "answer to branch gamma"
        assert any("lifetime" in f for f in result.faults), _faults(result)
        assert result.cap_trips == 1
        assert result.leaf_respawns == 0  # the refused re-spawn never existed
        assert result.status == "degraded"  # never a clean "complete"
        # gamma's leaf actually ran and got accounted — the HIGH-1 bug left it
        # running unattended with nobody charging it.
        assert result.tokens_in + result.tokens_out > 0
    finally:
        core.shutdown()


class _CostedClient(ModelClient):
    """Like `test_workflow_failclosed.ScriptedClient`, but the responder picks
    its OWN token usage per call — needed to engineer a token ceiling that
    survives the initial fan-out's generous STATIC per-leaf estimate
    (`EST_TOKENS_PER_LEAF`) yet starves a re-spawn once the run's own much
    larger MEASURED rate replaces it (`budget.est_leaf_cost`)."""

    def __init__(self, responder):
        self._responder = responder

    def _prompt(self, kwargs):
        msgs = kwargs.get("messages") or []
        return " ".join(m.get("content", "") for m in msgs if isinstance(m.get("content"), str))

    def create(self, **kwargs):
        text, usage = self._responder(self._prompt(kwargs))
        return {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": usage,
        }

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


def test_h7_token_budget_exhausted_mid_retry_pauses_without_orphaning(db):
    """Same shape, a TOKEN ceiling instead of a lifetime one. Each real leaf
    here costs 10,000 tokens — far above the pre-measurement static estimate
    (2,000/leaf, `budget.EST_TOKENS_PER_LEAF`) `gate_fanout` uses for the
    ORIGINAL width-3 fan-out, but the run's own measured rate the moment its
    first leaf is charged.

    `run_parallel` collects branches IN ORDER (alpha, beta, gamma) — by the
    time beta's death triggers a re-spawn, only ALPHA has been collected and
    charged; gamma is still spawned-but-uncollected. `token_budget=15000`
    clears `gate_fanout(3)` on the generous static guess (7 affordable
    leaves) but leaves only 5,000 once alpha's real 10,000-token rate is
    known — not enough for one more leaf at that rate, so beta's re-spawn is
    refused. `note_budget_exhausted` already latches the pause and writes its
    own fault before `_gate_tokens` raises: the branch settles as dead, no
    NEW fault is added for it, and the run PAUSES rather than crashing or
    losing gamma's leaf — collected normally on the NEXT loop iteration,
    after the pause already latched."""

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=_CostedClient(_costed_responder),
        )

    def _costed_responder(prompt: str):
        if "beta" in prompt:
            raise RuntimeError("branch beta died once")
        return prompt, {"input_tokens": 10_000, "output_tokens": 0}

    core = OrchestrationCore(db, factory, max_concurrent=4)
    try:
        raw = {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "p", "type": "parallel", "retries": 1, "branches": [
                    {"type": "agent", "prompt": "branch alpha"},
                    {"type": "agent", "prompt": "branch beta"},
                    {"type": "agent", "prompt": "branch gamma"},
                ]},
            ],
        }
        spec = validate_spec(raw)
        assert not hasattr(spec, "issues"), getattr(spec, "message", "")
        result = WorkflowEngine(core, budget=Budget(token_budget=15_000)).run(spec, {})
        assert result.outputs["p"][0] == "branch alpha"
        assert result.outputs["p"][1] is None
        assert result.outputs["p"][2] == "branch gamma"
        assert result.status == "paused"
        assert result.pause_reason == "token_budget_exhausted"
        assert result.leaf_respawns == 0  # the refused re-spawn never existed
        # alpha and gamma's real cost landed in the rollup — nothing orphaned.
        assert result.tokens_in >= 20_000
    finally:
        core.shutdown()
