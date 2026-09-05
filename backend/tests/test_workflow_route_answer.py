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

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lohra.state import SessionDB
from lohra.workflow.launch import route_answer
from lohra.workflow.nodes import ROUTING_FIELDS
from lohra.workflow.route_fault import (
    ROUTE_FAULT,
    ROUTE_FAULT_HINT,
    abort_fault,
    apply_route_answer,
    looks_like_route_answer,
    parse_route_answer,
    same_dead_route,
)
from lohra.workflow.runstate_store import DurableRun, RunStateStore
from lohra.workflow.strategies import _ROUTING_FIELDS, _leaf_config
from tests.test_workflow_pivot import (
    AUTH_MODEL,
    DEFAULT_MODEL,
    GOOD_MODEL,
    LEAF_COST,
    _finish,
    _service,
    _spec,
)


class _Node:
    """The two attributes ``_leaf_config`` reads off a validated node."""

    def __init__(self, fields: dict) -> None:
        self.id = fields["id"]
        self.fields = fields


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
        # THROUGH WHAT the route moved, and from where to where — the CHANNEL,
        # never an author: the harness observes a resume, not who typed it.
        assert any(
            "re-routed after a route_fault pause" in fault
            and "answered through checkpoint_answers (the command channel)" in fault
            and "never chosen by the harness" in fault
            and "anthropic/auth-rejected -> " in fault
            for fault in recovered["faults_total"]
        )
        reroute = next(
            f for f in recovered["faults_total"] if "re-routed after a route_fault" in f
        )
        assert "human" not in reroute.lower()  # the CHANNEL, never an author
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
        assert any(
            'route_fault answered "abort" through checkpoint_answers' in f
            and "the command channel" in f
            for f in row.prior_faults
        )
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


# --- 6. the review's gates ----------------------------------------------------


def test_a_second_abort_never_resurrects_the_run(db, tmp_path):
    """F1 (review adversarial): a cancel is not a thing you can say twice.

    After the first ``abort`` the run is ``cancelled``, and resuming a cancelled
    run is allowed by design. A repeated ``{"target": "abort"}`` used to sail
    through as an ordinary checkpoint answer: the run relaunched, re-spawned the
    route already known to be dead (+1 provider call), and the line went back to
    ``paused`` still carrying the fault that says it was cancelled — a false
    fact about a live run."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT
        assert service.start(
            resume_run_id=run_id, checkpoint_answers={"target": "abort"}
        )["status"] == "cancelled"
        spent = len(calls)

        again = service.start(resume_run_id=run_id, checkpoint_answers={"target": "abort"})

        assert "error" in again and "route_fault" in again["error"]
        assert len(calls) == spent  # the dead route was NOT re-spawned
        row = RunStateStore(db, holder="reader").load(run_id)
        assert row.status == "cancelled" and row.pause_reason is None
        # ...and a repeated ROUTE answer is refused on the same grounds.
        assert "error" in service.start(
            resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}}
        )
        assert len(calls) == spent
    finally:
        service.shutdown()


def test_an_abort_shaped_answer_to_a_nested_checkpoint_still_gets_through():
    """The other side of F1: a checkpoint one level down is answered under a
    namespaced key (#78) that names no node of the parent spec, so its type is
    unknown there — and a human answering that gate "abort" must not be refused
    as a misplaced route.

    The pause is written in the shape the engine really produces now: the key is
    ``sub[<workflow node>]:<id>`` and the payload names the template."""
    prior = DurableRun(
        run_id="r1", status="paused", pause_reason="checkpoint",
        checkpoint={
            "node_id": "sub[deploy]:approve",
            "prompt": "ok?",
            "template": "deployer",
        },
        spec=SPEC,
    )
    out = route_answer("r1", {"sub[deploy]:approve": "abort"}, False, prior)
    assert out.error is None and out.answers == {"sub[deploy]:approve": "abort"}


def test_the_acceptance_echoes_the_route_it_actually_applied(db, tmp_path):
    """F3: two words in, the resolved route back — so "did it take the model I
    meant, or the provider I forgot to change?" is answered at the acceptance
    instead of inferred out of fault prose (or out of a second pause)."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT

        launched = service.start(
            resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}}
        )
        assert launched["rerouted"] == {
            "node_id": "target",
            "from": f"anthropic/{AUTH_MODEL}",
            "to": f"anthropic/{GOOD_MODEL}",  # the provider it KEPT, said out loud
        }
        _finish(service, run_id)
    finally:
        service.shutdown()


def test_a_certified_template_says_it_needed_an_emergency_route(db, tmp_path):
    """F4: the spec being certified is the ADAPTED one, so without a stamp the
    template publishes the emergency route as if the author had chosen it."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT
        service.start(resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}})
        assert _finish(service, run_id)["status"] == "complete"

        template = json.loads(
            (tmp_path / "workflows" / "templates" / "sup04-controlled-pivot.json").read_text()
        )
        assert template["meta"]["rerouted_nodes"] == ["target"]
        assert template["nodes"][1]["model"] == GOOD_MODEL  # ...which IS the stamped route
    finally:
        service.shutdown()


def test_a_clean_run_is_certified_without_the_stamp(db, tmp_path):
    """...and a template nobody re-routed carries no empty list: every template
    written before this existed is such a run, and `[]` on all of them would be
    noise rather than information."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=GOOD_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["status"] == "complete"
        template = json.loads(
            (tmp_path / "workflows" / "templates" / "sup04-controlled-pivot.json").read_text()
        )
        assert "rerouted_nodes" not in template["meta"]
    finally:
        service.shutdown()


def test_a_refused_credential_is_refused_at_every_effort():
    """F5: an effort change is a different CALL, which is worth one more attempt
    for a death nobody could classify. It is not a different CREDENTIAL — so on
    ``auth_failed`` the same provider/model at another effort buys a second,
    certain death at full price."""
    auth = {**DEAD_PAYLOAD, "error_kind": "auth_failed"}
    assert same_dead_route({"model": "opus", "effort": "high"}, auth)
    # ...and the same answer on an UNCLASSIFIED death is still worth a try.
    unknown = {**DEAD_PAYLOAD, "error_kind": None}
    assert not same_dead_route({"model": "opus", "effort": "high"}, unknown)
    # A real route change is never blocked, whatever the kind.
    assert not same_dead_route({"model": "sonnet"}, auth)


def test_the_abort_record_of_a_nested_route_names_the_template():
    """F6: the node id is namespaced and points at nothing in the spec this run
    persists, so the cancelled line would otherwise leave the reader guessing
    where that route lived."""
    payload = {**DEAD_PAYLOAD, "node_id": "sub[inner]:leaf", "template": "inner-wf"}
    fault = abort_fault("sub[inner]:leaf", payload)
    assert "inside template 'inner-wf'" in fault
    assert "inside template" not in abort_fault("target", DEAD_PAYLOAD)


def test_the_remedy_warns_that_a_tier_needs_both_halves():
    """F8: a node routed by `tier` answered with a model alone keeps the tier's
    PROVIDER and dies on the same route."""
    assert "answer with BOTH 'provider' and 'model'" in ROUTE_FAULT_HINT


def test_a_tier_routed_node_needs_both_halves_of_the_answer():
    """The premise behind F8's advice, pinned rather than assumed.

    ``apply_route_answer`` merges the answer onto the node, so a node routing by
    ``tier`` keeps the tier NEXT TO the new fields — and `_leaf_config` resolves
    explicit-beats-tier per field. A ``model`` alone therefore leaves the TIER's
    provider in force and the node dies on the same route again; both halves
    move it. If this ever inverts, the hint and the tool schema are lying."""
    node = {"id": "t", "type": "agent", "prompt": "p", "tier": "big"}
    spec = {"meta": {"name": "n", "version": 1}, "nodes": [node]}
    tier = SimpleNamespace(model="tier-model", effort=None, provider="tier-provider")
    engine = SimpleNamespace(tiers={"big": tier})

    half = apply_route_answer(spec, "t", {"model": "answered-model"})
    model, _, provider, _ = _leaf_config(engine, _Node(half["nodes"][0]))
    assert (model, provider) == ("answered-model", "tier-provider")  # the footgun

    both = apply_route_answer(spec, "t", {"model": "answered-model", "provider": "answered"})
    model, _, provider, _ = _leaf_config(engine, _Node(both["nodes"][0]))
    assert (model, provider) == ("answered-model", "answered")
