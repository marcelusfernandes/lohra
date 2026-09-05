"""``node.rerouted``: the re-route as a TYPED ledger fact (issue #64).

The dogfood T10 of the command channel (#43) found the gap this closes: the
audit showed the old route in one ``leaf.started`` and the new one in another,
and the sentence that named the MOVE existed only as prose in
``workflow_status.faults_total``. The ledger is what exists to answer "who/when
moved this route" by machine, so the move itself has to be an event.

Two properties carry the whole file:

- ``provider``/``model``/``effort``/``node_id`` are CONFIGURATION IDENTITY, not
  content — they survive the sanitizer verbatim (bounded), exactly like the
  ``model``/``provider`` a ``leaf.started`` already carries. Anything else in
  the payload still dies.
- ``channel`` is a CLOSED vocabulary and names only the surface the answer
  arrived through — never an author the harness never observed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lohra.state import SessionDB
from lohra.workflow.audit import (
    CHANNEL_CATALOG,
    CHANNEL_CHECKPOINT_ANSWERS,
    CHANNEL_ROUTE_ENVELOPE,
    NODE_REROUTED,
    REROUTE_CHANNELS,
    rerouted_event,
    sanitize_audit_event,
)
from lohra.workflow.causality import CausalContext
from lohra.workflow.route_fault import ROUTE_FAULT, route_change
from tests.test_workflow_pivot import (
    AUTH_MODEL,
    BAD_MODEL,
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


def _context() -> CausalContext:
    return CausalContext(
        run_id="run-reroute", segment_id="seg-2", node_path=("target",),
        cell_id="cell-1", role="run.reroute",
    )


def _reroutes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["event_type"] == NODE_REROUTED]


# --- 1. the shape, and what the channel is allowed to say ---------------------


def test_the_event_carries_the_move_and_the_channel_only() -> None:
    event = rerouted_event(
        _context(),
        node_id="target",
        before={"provider": "openrouter", "model": "no-such-model", "effort": None},
        after={"provider": "openrouter", "model": "deepseek/deepseek-chat",
               "effort": "high"},
        channel=CHANNEL_CHECKPOINT_ANSWERS,
    )
    assert event["event_type"] == NODE_REROUTED
    assert event["provenance"] == "observed"
    assert event["data"] == {
        "node_id": "target",
        # An absent half is simply absent: the leaf ran on the run's default and
        # saying `null` would claim the harness observed one.
        "from": {"provider": "openrouter", "model": "no-such-model"},
        "to": {"provider": "openrouter", "model": "deepseek/deepseek-chat",
               "effort": "high"},
        "channel": "checkpoint_answers",
    }


def test_the_channel_vocabulary_is_closed_and_holds_every_surface() -> None:
    """#63's route envelope and #85's catalog substitution emit the SAME event
    through their own channels — three surfaces, three authorities, one act."""
    assert REROUTE_CHANNELS == {"checkpoint_answers", "route_envelope", "catalog"}
    assert CHANNEL_CHECKPOINT_ANSWERS in REROUTE_CHANNELS
    assert CHANNEL_ROUTE_ENVELOPE in REROUTE_CHANNELS
    assert CHANNEL_CATALOG in REROUTE_CHANNELS


def test_a_channel_the_ledger_cannot_name_degrades_to_unavailable() -> None:
    """Never prose: a caller that invents a surface gets the canonical marker,
    not its own word smuggled into a durable, shared record."""
    event = rerouted_event(
        _context(), node_id="target", before={}, after={"model": "m"},
        channel="a human told me so",
    )
    assert event["data"]["channel"] == "unavailable"
    assert sanitize_audit_event(event)["data"]["channel"] == "unavailable"


def test_an_end_nobody_can_name_is_empty_rather_than_invented() -> None:
    """A node that declared no route ran on the RUN's default, and the pause
    payload may name neither half. ``{}`` says exactly that; ``null`` halves
    would claim the harness measured something it never observed — and a
    non-mapping end (a caller bug, #63's envelope included) degrades the same
    way instead of dragging its repr into a durable record."""
    event = rerouted_event(
        _context(), node_id="", before=None, after=("openrouter", "m"),
        channel=CHANNEL_ROUTE_ENVELOPE,
    )
    assert event["data"]["from"] == {}
    assert event["data"]["to"] == {}
    assert event["data"]["node_id"] == "$unavailable"
    assert sanitize_audit_event(event)["data"]["from"] == {}


def test_route_change_says_what_moved_and_what_stayed() -> None:
    """The two facts ``reroute_fault`` says in prose, as data — one derivation
    shared by the command channel and (#63) the envelope."""
    before, after = route_change(
        {"node_id": "target", "provider": "openrouter", "model": "dead"},
        {"model": "alive", "effort": "high"},
    )
    assert before == {"provider": "openrouter", "model": "dead", "effort": None}
    # The provider the answer did NOT move is carried forward, not dropped.
    assert after == {"provider": "openrouter", "model": "alive", "effort": "high"}


# --- 2. the sanitizer: identifiers survive, everything else dies --------------


def test_the_sanitizer_keeps_the_identifiers_and_redacts_any_other_string() -> None:
    """Both passes (producer and ``audit_append``) must agree: a value legible
    once and not twice is still lost."""
    event = rerouted_event(
        _context(),
        node_id="target",
        before={"provider": "openrouter", "model": "nonexistent-vendor/no-such-model-xyz"},
        after={"provider": "openrouter", "model": "deepseek/deepseek-chat",
               "effort": "high"},
        channel=CHANNEL_CHECKPOINT_ANSWERS,
    )
    once = sanitize_audit_event(event)
    twice = sanitize_audit_event(once)
    assert once == twice
    assert twice["data"] == {
        "node_id": "target",
        "from": {"provider": "openrouter", "model": "nonexistent-vendor/no-such-model-xyz"},
        "to": {"provider": "openrouter", "model": "deepseek/deepseek-chat",
               "effort": "high"},
        "channel": "checkpoint_answers",
    }


def test_prose_smuggled_into_the_route_dies_like_every_other_string() -> None:
    """The allow-list is per KEY, not per event: naming `from`/`to` does not open
    a hole a payload can pour a reason, a prompt or a stack trace through."""
    forged = {
        "schema_version": 1,
        "event_type": NODE_REROUTED,
        "provenance": "observed",
        "identity": {"run_id": "run-reroute", "node_path": ["target"]},
        "data": {
            "node_id": "target",
            "from": {"provider": "openrouter", "cause": "SECRET-PROSE-64"},
            "to": {"model": "m", "note": "SECRET-PROSE-64"},
            "channel": "checkpoint_answers",
            "who": "SECRET-PROSE-64",
        },
    }
    safe = sanitize_audit_event(forged)
    assert "SECRET-PROSE-64" not in json.dumps(safe)
    assert safe["data"]["from"] == {
        "provider": "openrouter",
        "cause": {"state": "excluded_by_policy", "size": {
            "state": "observed", "unit": "characters", "value": 15}},
    }
    assert safe["data"]["to"]["model"] == "m"
    assert safe["data"]["to"]["note"]["state"] == "excluded_by_policy"
    assert safe["data"]["who"]["state"] == "excluded_by_policy"


def test_the_identifiers_stay_bounded_so_the_channel_cannot_grow() -> None:
    """Authored/answered identity is kept verbatim, never unbounded."""
    safe = sanitize_audit_event(
        rerouted_event(
            _context(), node_id="n" * 200,
            before={"model": "m" * 500}, after={"model": "M" * 500},
            channel=CHANNEL_ROUTE_ENVELOPE,
        )
    )
    assert len(safe["data"]["node_id"]) == 64  # the ``node_path`` ceiling
    assert len(safe["data"]["from"]["model"]) == 128
    assert len(safe["data"]["to"]["model"]) == 128


# --- 3. the real path: a command-answered reroute writes exactly one event ----


def test_a_command_answered_reroute_writes_exactly_one_typed_event(db, tmp_path):
    """(i) The whole issue: the ledger says the route MOVED, from where to
    where, and through which surface — no inference across two leaf.started."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT

        service.start(
            resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}}
        )
        assert _finish(service, run_id)["status"] == "complete"
    finally:
        service.shutdown()

    events = _reroutes(db.audit_events(run_id))
    assert len(events) == 1
    event = events[0]
    assert event["identity"]["node_path"] == ["target"]
    assert event["data"] == {
        "node_id": "target",
        "from": {"provider": "anthropic", "model": AUTH_MODEL},
        "to": {"provider": "anthropic", "model": GOOD_MODEL},
        "channel": "checkpoint_answers",
    }


def test_a_run_that_was_never_re_routed_records_nothing(db, tmp_path):
    """The event is a FACT, not a field: a clean run's trail is unchanged."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(_spec(pivot_model=GOOD_MODEL), {})["run_id"]
        assert _finish(service, run_id)["status"] == "complete"
    finally:
        service.shutdown()
    assert _reroutes(db.audit_events(run_id)) == []
    assert db.audit_query(run_id)["routing"] == {
        "rerouted": 0, "reroutes": [], "reroutes_truncated": False
    }


def test_an_abort_is_not_a_reroute(db, tmp_path):
    """Nothing moved, so nothing may claim it did."""
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
    finally:
        service.shutdown()
    assert _reroutes(db.audit_events(run_id)) == []


# --- 4. the readers: audit_query counts, the CLI prints the line --------------


def test_audit_query_counts_and_filters_the_reroute(db, tmp_path):
    """(iii) A count that survives a filter and a page: a reroute is rare and
    run-wide, so a node filter must never be able to hide that one happened."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT
        service.start(
            resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}}
        )
        assert _finish(service, run_id)["status"] == "complete"
    finally:
        service.shutdown()

    filtered = db.audit_query(run_id, event_type=NODE_REROUTED)
    assert [event["event_type"] for event in filtered["events"]] == [NODE_REROUTED]
    assert filtered["filters"] == {"event_type": NODE_REROUTED}

    # ...and the run-wide summary answers "was anything re-routed?" without
    # paging the whole trail — even under a filter that excludes the event.
    for scope in (db.audit_query(run_id), db.audit_query(run_id, node_id="stable")):
        assert scope["routing"]["rerouted"] == 1
        assert scope["routing"]["reroutes_truncated"] is False
        (only,) = scope["routing"]["reroutes"]
        assert only["node_id"] == "target"
        assert only["from"] == {"provider": "anthropic", "model": AUTH_MODEL}
        assert only["to"] == {"provider": "anthropic", "model": GOOD_MODEL}
        assert only["channel"] == "checkpoint_answers"
        assert only["seq"] > 0


def test_the_cli_audit_prints_the_reroute_line(db, tmp_path, monkeypatch, capsys):
    """(iv) ``lohra workflow audit <run>`` shows from -> to and the channel —
    zero LLM, zero tokens, a human at a terminal."""
    from lohra import cli
    from lohra.memory.paths import state_db_path

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    database = SessionDB(str(state_db_path()))
    calls: list[tuple[str, str]] = []
    service = _service(database, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=4 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["reason"] == ROUTE_FAULT
        service.start(
            resume_run_id=run_id, checkpoint_answers={"target": {"model": GOOD_MODEL}}
        )
        assert _finish(service, run_id)["status"] == "complete"
    finally:
        service.shutdown()
        database.close()

    assert cli.run_workflow_cmd("audit", run_id=run_id, event_type=NODE_REROUTED) == 0
    printed = json.loads(capsys.readouterr().out)
    (line,) = printed["events"]
    assert line["event_type"] == NODE_REROUTED
    assert line["data"]["from"]["model"] == AUTH_MODEL
    assert line["data"]["to"]["model"] == GOOD_MODEL
    assert line["data"]["channel"] == "checkpoint_answers"
    assert printed["routing"]["rerouted"] == 1


# --- 5. the T10 shape, offline ------------------------------------------------


def _retry_spec(model: str) -> dict[str, Any]:
    """T10's DAG: one cell that survives, one routed cell that declares a
    bounded series — the evidence branch (b) of the pause needs."""
    return {
        "meta": {"name": "t10-dead-route", "version": 1},
        "nodes": [
            {"id": "ok", "type": "agent", "prompt": "independent stable work"},
            {"id": "doomed", "type": "agent", "prompt": "independent routed work",
             "model": model, "retries": 1},
        ],
    }


def test_the_dogfood_t10_scenario_records_one_reroute(db, tmp_path):
    """(vi) The report's expectation (e), offline: a dead route with
    ``retries: 1`` exhausts its declared series, the run pauses, the human
    answers by command — and the ledger now names the move once."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _retry_spec(BAD_MODEL), {}, token_budget=8 * LEAF_COST
        )["run_id"]
        paused = _finish(service, run_id)
        assert paused["status"] == "paused" and paused["reason"] == ROUTE_FAULT

        accepted = service.start(
            resume_run_id=run_id, checkpoint_answers={"doomed": {"model": GOOD_MODEL}}
        )
        assert accepted["rerouted"]["node_id"] == "doomed"
        assert _finish(service, run_id)["status"] == "complete"
    finally:
        service.shutdown()

    trail = db.audit_events(run_id)
    # The old route really did run twice (the authored series) — the inference
    # T10 had to make by hand is still available...
    old = [
        event for event in trail
        if event["event_type"] == "leaf.started"
        and event["data"].get("model") == BAD_MODEL
    ]
    assert len(old) == 2
    # ...and now it does not have to be made at all.
    (event,) = _reroutes(trail)
    assert event["data"] == {
        "node_id": "doomed",
        "from": {"provider": "anthropic", "model": BAD_MODEL},
        "to": {"provider": "anthropic", "model": GOOD_MODEL},
        "channel": "checkpoint_answers",
    }
    assert calls[-1] == (GOOD_MODEL, "independent routed work")
    # The completed cell replayed: only the node that died was paid for again.
    assert [model for model, _ in calls].count(DEFAULT_MODEL) == 1
