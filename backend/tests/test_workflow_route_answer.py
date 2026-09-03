"""A ``route_fault`` pause answered by COMMAND (issue #43, decisão 1 do dono).

The pause used to demand that the agent re-author and re-send the WHOLE spec to
move one dead route. Here the human answers a route — or ``abort`` — through the
channel that already exists (``checkpoint_answers={node_id: answer}``), and the
harness edits that ONE node in the spec the run persisted.

Two halves, and the second is the load-bearing one: what the channel ACCEPTS
(one node's provider/model/effort, on the node the pause names) and everything
it REFUSES, each with the reason. A half-understood answer would re-pay a node
to die on a route nobody chose.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from lohra.state import SessionDB
from lohra.workflow.launch import route_answer
from lohra.workflow.nodes import ROUTING_FIELDS
from lohra.workflow.route_fault import (
    ROUTE_FAULT,
    apply_route_answer,
    looks_like_route_answer,
    parse_route_answer,
)
from lohra.workflow.runstate_store import DurableRun, RunStateStore
from lohra.workflow.strategies import _ROUTING_FIELDS
from tests.test_workflow_pivot import (
    AUTH_MODEL,
    DEFAULT_MODEL,
    GOOD_MODEL,
    LEAF_COST,
    _finish,
    _service,
    _spec,
)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _paused(spec: dict, *, payload: dict, run_id: str = "r1") -> DurableRun:
    """A run's line as a ``route_fault`` pause leaves it."""
    return DurableRun(
        run_id=run_id,
        status="paused",
        pause_reason=ROUTE_FAULT,
        route_fault=payload,
        spec=spec,
    )


DEAD_PAYLOAD = {
    "node_id": "target",
    "provider": "anthropic",
    "model": "opus",
    "error_kind": "auth_failed",
    "cause": "the provider refused this credential",
}

SPEC = {
    "meta": {"name": "answered-route", "version": 1},
    "nodes": [
        {"id": "stable", "type": "agent", "prompt": "stable work"},
        {"id": "target", "type": "agent", "prompt": "routed work", "model": "opus"},
    ],
}


# --- 1. the vocabulary is read off ONE declaration ----------------------------


def test_the_routing_vocabulary_has_exactly_one_source():
    """The cell identity and the route answer must agree on what "a route" IS.

    Two copies of the tuple is how a node ends up re-routed by an answer whose
    change the cache key never notices (or the reverse)."""
    assert _ROUTING_FIELDS == ROUTING_FIELDS


# --- 2. parse: what a human may answer ----------------------------------------


def test_abort_is_the_one_word_that_is_not_a_route():
    assert parse_route_answer("abort").abort is True
    assert parse_route_answer("  ABORT ").abort is True
    assert parse_route_answer("abort").route == {}


def test_a_route_answer_is_provider_model_and_optionally_effort():
    answer = parse_route_answer({"provider": "openai", "model": "gpt-x", "effort": "high"})
    assert answer.abort is False
    assert answer.route == {"provider": "openai", "model": "gpt-x", "effort": "high"}


@pytest.mark.parametrize(
    ("answer", "token"),
    [
        ({"prompt": "do it differently"}, "prompt"),
        ({"depends_on": ["stable"]}, "depends_on"),
        ({"model": "m", "retries": 3}, "retries"),
        # A tier would CHANGE the spec and NOT change the route wherever the node
        # already names a model (explicit model wins): a silent no-op is the one
        # answer shape this channel must never accept.
        ({"tier": "big"}, "tier"),
    ],
)
def test_an_answer_that_moves_anything_but_the_route_is_refused(answer, token):
    refusal = parse_route_answer(answer)
    assert isinstance(refusal, str)
    assert token in refusal
    assert "spec=" in refusal  # ...and says which channel DOES move it


def test_an_effort_alone_is_not_a_route():
    refusal = parse_route_answer({"effort": "high"})
    assert isinstance(refusal, str) and "provider" in refusal and "model" in refusal


@pytest.mark.parametrize("answer", [{"model": ""}, {"model": "   "}, {"provider": 7}, 7, None, []])
def test_a_route_answer_that_is_not_two_strings_is_refused(answer):
    assert isinstance(parse_route_answer(answer), str)


def test_a_word_that_is_not_abort_is_refused_rather_than_guessed():
    refusal = parse_route_answer("please stop")
    assert isinstance(refusal, str) and "abort" in refusal


def test_only_a_routing_shaped_object_reads_as_a_route_answer():
    assert looks_like_route_answer({"provider": "x"})
    assert not looks_like_route_answer({})
    assert not looks_like_route_answer("abort")  # a checkpoint human may write it
    assert not looks_like_route_answer({"provider": "x", "prompt": "y"})


# --- 3. apply: the edit is one node, and immutable ----------------------------


def test_the_edit_touches_one_node_and_mutates_nothing():
    original = deepcopy(SPEC)
    adapted = apply_route_answer(SPEC, "target", {"provider": "openai", "model": "gpt-x"})

    assert SPEC == original  # the caller's document is untouched
    assert adapted is not SPEC and adapted["nodes"] is not SPEC["nodes"]
    assert adapted["nodes"][0] is SPEC["nodes"][0]  # unchanged nodes are shared, not copied
    assert adapted["nodes"][1] == {
        "id": "target", "type": "agent", "prompt": "routed work",
        "model": "gpt-x", "provider": "openai",
    }


def test_a_node_the_spec_does_not_carry_is_refused():
    refusal = apply_route_answer(SPEC, "ghost", {"model": "m"})
    assert isinstance(refusal, str) and "ghost" in refusal


@pytest.mark.parametrize(
    "node",
    [
        {"id": "p", "type": "pipeline", "items": [], "stages": []},
        {"id": "p", "type": "parallel", "branches": []},
        {"id": "p", "type": "checkpoint", "prompt": "ok?"},
    ],
)
def test_a_node_type_with_no_routing_at_all_is_refused(node):
    spec = {"meta": {"name": "n", "version": 1}, "nodes": [node]}
    refusal = apply_route_answer(spec, "p", {"model": "m"})
    assert isinstance(refusal, str)
    assert node["type"] in refusal and "adapted spec" in refusal


def test_a_rigor_node_that_declared_no_route_is_refused():
    """POLICY (#43): a rigor node with no routing field of its own ran on the
    RUN's default — the route the payload names is the session's, not something
    this spec ever chose, and re-routing this one node would leave every other
    default-routed node on the same dead route. Authoring the route is a spec."""
    spec = {
        "meta": {"name": "n", "version": 1},
        "nodes": [{"id": "v", "type": "verify", "finding": "x", "skeptics": 2}],
    }
    refusal = apply_route_answer(spec, "v", {"model": "m"})
    assert isinstance(refusal, str)
    assert "declares no route of its own" in refusal and "adapted spec" in refusal


def test_a_rigor_node_that_DID_declare_a_route_is_re_routed():
    spec = {
        "meta": {"name": "n", "version": 1},
        "nodes": [
            {"id": "v", "type": "verify", "finding": "x", "skeptics": 2, "model": "opus"}
        ],
    }
    adapted = apply_route_answer(spec, "v", {"model": "sonnet"})
    assert adapted["nodes"][0]["model"] == "sonnet"


# --- 4. the launch decision: order of refusals is part of the contract --------


def test_an_answer_for_a_node_that_is_not_the_dead_one_is_refused():
    out = route_answer("r1", {"stable": {"model": "m"}}, False, _paused(SPEC, payload=DEAD_PAYLOAD))
    assert out.error is not None
    assert "stable" in out.error and "target" in out.error
    assert out.node_id is None and out.abort_node is None


def test_a_nested_route_is_refused_before_the_node_is_even_looked_up():
    """The namespaced ``sub[ref]:node`` id is in no spec, so a not-found message
    would mask the real reason: the route lives in a TEMPLATE."""
    payload = {**DEAD_PAYLOAD, "node_id": "sub[inner]:leaf", "template": "inner-wf"}
    out = route_answer("r1", {"sub[inner]:leaf": {"model": "m"}}, False, _paused(SPEC, payload=payload))
    assert out.error is not None
    assert "the route lives in template 'inner-wf'; adapt the template" in out.error


def test_a_nested_route_can_still_be_answered_with_abort():
    """An abort edits nothing, so "adapt the template" is the WRONG remedy for a
    human who asked to stop. A nested route cannot be answered with a route; it
    can always be answered with a cancel."""
    payload = {**DEAD_PAYLOAD, "node_id": "sub[inner]:leaf", "template": "inner-wf"}
    out = route_answer("r1", {"sub[inner]:leaf": "abort"}, False, _paused(SPEC, payload=payload))
    assert out.error is None and out.abort_node == "sub[inner]:leaf"


def test_an_answer_and_an_explicit_spec_together_are_refused():
    """One channel per resume. Both would be two different last words on where
    the run goes, and refusing costs nothing — the run stays paused."""
    out = route_answer("r1", {"target": {"model": "m"}}, True, _paused(SPEC, payload=DEAD_PAYLOAD))
    assert out.error is not None
    assert "one channel per resume" in out.error
    # ...including an abort: "run THIS" and "stop" cannot both be the answer.
    aborting = route_answer("r1", {"target": "abort"}, True, _paused(SPEC, payload=DEAD_PAYLOAD))
    assert aborting.abort_node is None and "one channel per resume" in (aborting.error or "")


def test_a_stranger_key_outranks_every_later_refusal():
    """Precedence, pinned: the FIRST thing wrong with a call is what it is told.
    A stranger key is refused even when the call is also double-channelled and
    the answer itself is malformed — the caller fixes one thing at a time."""
    out = route_answer(
        "r1", {"stable": {"prompt": "nope"}}, True, _paused(SPEC, payload=DEAD_PAYLOAD)
    )
    assert out.error is not None
    assert "stable" in out.error
    assert "one channel per resume" not in out.error and "prompt" not in out.error


def test_a_fresh_launch_is_not_a_pause_and_is_never_refused():
    """No ``resume_run_id`` means no pause to answer: a routing-shaped answer to
    a checkpoint node in a brand-new spec must not be refused as "not on file"."""
    out = route_answer(None, {"approve": {"provider": "openai"}}, True, None)
    assert out.error is None and out.answers == {"approve": {"provider": "openai"}}


def test_answering_with_the_route_that_just_died_is_refused():
    out = route_answer(
        "r1", {"target": {"provider": "anthropic", "model": "opus"}}, False,
        _paused(SPEC, payload=DEAD_PAYLOAD),
    )
    assert out.error is not None and "just died" in out.error


def test_a_default_routed_death_never_refuses_on_a_route_nobody_can_name():
    """The payload reports what the leaf REALLY ran on; a node on the run default
    may name neither half. Refusing there would be a guess."""
    payload = {**DEAD_PAYLOAD, "provider": None, "model": None}
    out = route_answer("r1", {"target": {"model": "opus"}}, False, _paused(SPEC, payload=payload))
    assert out.error is None and out.route == {"model": "opus"}


def test_a_route_shaped_answer_to_a_run_not_paused_on_a_route_is_refused():
    running = DurableRun(run_id="r1", status="running", spec=SPEC)
    out = route_answer("r1", {"target": {"model": "m"}}, False, running)
    assert out.error is not None and "route_fault" in out.error


def test_a_checkpoint_answer_is_never_read_as_a_route():
    """(viii) The checkpoint channel keeps its meaning, "abort" included."""
    spec = {
        "meta": {"name": "n", "version": 1},
        "nodes": [{"id": "approve", "type": "checkpoint", "prompt": "ok?"}],
    }
    prior = DurableRun(
        run_id="r1", status="paused", pause_reason="checkpoint",
        checkpoint={"node_id": "approve", "prompt": "ok?"}, spec=spec,
    )
    out = route_answer("r1", {"approve": "abort"}, False, prior)
    assert out.error is None and out.abort_node is None
    assert out.answers == {"approve": "abort"}


def test_the_route_answer_is_stripped_from_the_checkpoint_channel():
    out = route_answer("r1", {"target": {"model": "sonnet"}}, False, _paused(SPEC, payload=DEAD_PAYLOAD))
    assert out.error is None
    assert out.node_id == "target" and out.route == {"model": "sonnet"}
    assert out.answers == {}  # nothing routing-shaped reaches the engine's gates


def test_a_plain_resume_of_a_route_pause_is_left_exactly_as_it_was():
    out = route_answer("r1", None, False, _paused(SPEC, payload=DEAD_PAYLOAD))
    assert out == route_answer("r1", {}, False, _paused(SPEC, payload=DEAD_PAYLOAD))
    assert out.error is None and out.node_id is None and out.answers == {}


def test_abort_is_read_only_on_the_node_the_pause_names():
    out = route_answer("r1", {"target": "abort"}, False, _paused(SPEC, payload=DEAD_PAYLOAD))
    assert out.abort_node == "target" and out.error is None


# --- 5. end to end, through the real service ---------------------------------


def test_the_answer_re_routes_the_persisted_spec_and_the_rest_replays(db, tmp_path):
    """(i) The whole point: a paused run, one command, and only the dead node is
    paid for again — no spec re-sent, no other cell re-spawned."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        paused = _finish(service, run_id)
        assert paused["status"] == "paused" and paused["reason"] == ROUTE_FAULT

        launched = service.start(
            resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}}
        )
        assert launched["run_id"] == run_id and launched["status"] == "started"
        # The one cell that never completed is the only one re-spawned.
        assert launched["cache_preview"]["replay"] == 1
        assert launched["cache_preview"]["never_completed"] == 1
        recovered = _finish(service, run_id)

        assert recovered["status"] == "complete"
        assert recovered["outputs"] == {
            "stable": f"ok:{DEFAULT_MODEL}", "target": f"ok:{GOOD_MODEL}"
        }
        # 'stable' really did replay: it was called once, in the first stretch.
        assert calls == [
            (DEFAULT_MODEL, "independent stable work"),
            (AUTH_MODEL, "independent routed work"),
            (GOOD_MODEL, "independent routed work"),
        ]
        # WHO moved the route, and from where to where.
        assert any(
            "re-routed after a route_fault pause" in fault and "VERBATIM" in fault
            for fault in recovered["faults_total"]
        )
        # ...and the dead route is gone from the durable line.
        row = RunStateStore(db, holder="reader").load(run_id)
        assert row.route_fault is None and row.pause_reason is None
        assert row.spec["nodes"][1]["model"] == GOOD_MODEL  # the PERSISTED spec moved
    finally:
        service.shutdown()


def test_abort_cancels_the_run_and_names_who_decided(db, tmp_path):
    """(iv) ``cancelled``, not ``failed``: nothing about the spec was refuted."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT

        out = service.start(resume_run_id=run_id, checkpoint_answers={"target": "abort"})
        assert out == {"run_id": run_id, "status": "cancelled"}
        assert service.status(run_id)["status"] == "cancelled"
        # Nothing was spawned by the abort itself.
        assert len(calls) == 2

        row = RunStateStore(db, holder="reader").load(run_id)
        assert row.status == "cancelled"
        assert row.pause_reason is None and row.route_fault is None
        assert any("route_fault answered abort by human" in f for f in row.prior_faults)
    finally:
        service.shutdown()


def test_an_answer_for_the_wrong_node_leaves_the_run_paused(db, tmp_path):
    """(ii) A refusal costs the run nothing: no lease, no spawn, still paused."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT

        out = service.start(resume_run_id=run_id, checkpoint_answers={"stable": {"model": "x"}})
        assert "error" in out and "stable" in out["error"]
        after = service.status(run_id)
        assert after["status"] == "paused" and after["reason"] == ROUTE_FAULT
        assert len(calls) == 2  # nothing re-spawned
    finally:
        service.shutdown()


def test_an_answer_that_edits_the_prompt_is_refused_by_the_service(db, tmp_path):
    """(v) The channel moves a ROUTE. Everything else is a spec."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT

        out = service.start(
            resume_run_id=run_id,
            checkpoint_answers={"target": {"model": GOOD_MODEL, "prompt": "different work"}},
        )
        assert "error" in out and "prompt" in out["error"]
        assert service.status(run_id)["status"] == "paused"
    finally:
        service.shutdown()


def test_an_answer_plus_an_explicit_spec_is_refused_by_the_service(db, tmp_path):
    """(vi) Pinned: the combination is REFUSED, never silently ranked."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        original = _spec(pivot_model=AUTH_MODEL)
        run_id = service.start(original, {}, token_budget=4 * LEAF_COST)["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT

        adapted = deepcopy(original)
        adapted["nodes"][1]["model"] = GOOD_MODEL
        out = service.start(
            adapted, resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}}
        )
        assert "error" in out and "one channel per resume" in out["error"]
        assert service.status(run_id)["status"] == "paused"
    finally:
        service.shutdown()


def test_the_answer_crosses_a_process_boundary(db, tmp_path):
    """(ix) The pause is durable, so the answer must be too: pause in one
    service, answer in another over the same database."""
    calls: list[tuple[str, str]] = []
    first = _service(db, tmp_path, calls)
    try:
        run_id = first.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(first, run_id)["reason"] == ROUTE_FAULT
    finally:
        first.shutdown()

    second = _service(db, tmp_path, calls)
    try:
        launched = second.start(
            resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}}
        )
        assert launched["status"] == "started"
        recovered = _finish(second, run_id)
        assert recovered["status"] == "complete"
        assert recovered["outputs"]["target"] == f"ok:{GOOD_MODEL}"
    finally:
        second.shutdown()
