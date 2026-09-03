"""Contract tests the review found missing: closed lists and direct canaries.

The repo's convention (``NODE_SPECS ≡ STRATEGIES ≡ skill``) is to pin a closed
list with a test rather than trust it to stay in sync by hand.  ``_SAFE_TOOL_NAMES``
had no such test, and the OBS-04 canary reached the tool and CLI surfaces only
transitively — through the layer it actually asserts on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lohra.agent.equip import register_all_tools
from lohra.state import SessionDB
from lohra.tools import registry
from lohra.workflow.audit import AuditTrail, _SAFE_TOOL_NAMES, sanitize_audit_event
from lohra.workflow.causality import CausalContext


def _builtin_tool_names() -> set[str]:
    register_all_tools()
    with registry._lock:
        return {
            name
            for name, entry in registry._entries.items()
            if not entry.toolset.startswith("mcp-")
        }


def test_safe_tool_names_matches_the_builtin_registry_exactly() -> None:
    """A literal tool name may be persisted only from a closed builtin vocabulary.

    Drift is safe in direction (an unlisted name degrades to a size marker) but
    it is silent: a tool added later would be redacted in every audit trail with
    nothing pointing at why.
    """
    names = _builtin_tool_names()
    assert names - _SAFE_TOOL_NAMES == set(), "new builtin tool missing from the audit vocabulary"
    assert _SAFE_TOOL_NAMES - names == set(), "audit vocabulary names a tool that no longer exists"


def test_mcp_tool_names_are_redacted_on_purpose() -> None:
    """MCP tools are registered at runtime from an operator's servers, so their
    names can never be in a static list — and a server name is not ours to
    persist.  The runtime gate still records that the call was a known tool."""
    event = {
        "schema_version": 1,
        "event_type": "tool.started",
        "provenance": "observed",
        "identity": {"run_id": "run-1"},
        "data": {"tool_name": "mcp_acme_secret_tool", "tool_name_state": "known_tool"},
    }
    safe = sanitize_audit_event(event)
    assert safe["data"]["tool_name"] == {"state": "observed", "characters": 20}
    assert safe["data"]["tool_name_state"] == "known_tool"
    assert "acme" not in json.dumps(safe)


def _canary_event(run_id: str, canary: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_type": "tool.completed",
        "provenance": "observed",
        "identity": {"run_id": run_id, "node_path": ["fetch"], "sub_id": "sub-1"},
        "data": {
            "tool_name": "web_fetch",
            "status": "success",
            "arguments": {"state": "redacted", "url": canary},
            "result": {"state": "redacted", "text": canary},
            "prompt": canary,
        },
    }


def test_the_agent_tool_output_carries_its_own_canary(tmp_path: Path) -> None:
    from lohra.workflow.audit_query import WorkflowAuditTool

    canary = "CANARY-TOOL-9f3a"
    assert canary in json.dumps(_canary_event("run-tool", canary)), "canary must be planted"
    db = SessionDB(str(tmp_path / "state.db"))
    trail = AuditTrail(db, queue_limit=8)
    trail.record(_canary_event("run-tool", canary))
    assert trail.flush(timeout=2)
    trail.shutdown()

    output = WorkflowAuditTool(db).handle({"run_id": "run-tool"})
    assert canary not in output
    payload = json.loads(output)
    assert payload["policy"]["mode"] == "metadata_only"
    assert payload["events"], "the canary event must still be visible as metadata"
    db.close()


def test_the_cli_output_carries_its_own_canary(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    from lohra import cli
    from lohra.memory.paths import state_db_path

    canary = "CANARY-CLI-71bd"
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    db = SessionDB(str(state_db_path()))
    trail = AuditTrail(db, queue_limit=8)
    trail.record(_canary_event("run-cli", canary))
    assert trail.flush(timeout=2)
    trail.shutdown()
    db.close()

    assert cli.run_workflow_cmd("audit", run_id="run-cli") == 0
    out = capsys.readouterr().out
    assert canary not in out
    assert json.loads(out)["events"]


def test_authored_node_ids_are_persisted_verbatim_by_design(tmp_path: Path) -> None:
    """Accepted residual risk, pinned so the decision stays visible.

    ``node_path`` is the one identity field whose content the agent authors (the
    spec is written by an LLM).  It is bounded (8 elements x 64 chars) and it is
    a query column by contract (§11.2), so it is stored verbatim — the ledger
    cannot report "which node" without it.  Everything the policy actually
    forbids (prompt/args/results/URL/reasoning) is still excluded.
    """
    db = SessionDB(str(tmp_path / "state.db"))
    trail = AuditTrail(db, queue_limit=8)
    trail.record(
        {
            "schema_version": 1,
            "event_type": "node.started",
            "provenance": "observed",
            "identity": {"run_id": "run-node", "node_path": ["author_chosen_name"]},
            "data": {"state": "running", "prompt": "SECRET-PROMPT"},
        }
    )
    assert trail.flush(timeout=2)
    trail.shutdown()
    page = db.audit_query("run-node")
    encoded = json.dumps(page)
    assert "author_chosen_name" in encoded, "node identity is by contract, not a leak"
    assert "SECRET-PROMPT" not in encoded
    node_ids = [
        row[0]
        for row in db._audit_connection.execute(
            "SELECT DISTINCT node_id FROM workflow_audit_events WHERE run_id = ?",
            ("run-node",),
        ).fetchall()
    ]
    assert node_ids == ["author_chosen_name"]
    db.close()


def test_node_ids_are_bounded_so_the_channel_cannot_grow(tmp_path: Path) -> None:
    context = CausalContext(
        run_id="run-1",
        segment_id="segment-1",
        node_path=tuple(f"{index}" + "x" * 200 for index in range(20)),
        cell_id="cell-1",
        role="leaf",
    )
    from lohra.workflow.audit import causal_audit_event

    safe = sanitize_audit_event(causal_audit_event("node.started", context))
    path = safe["identity"]["node_path"]
    assert len(path) == 8
    assert all(len(part) == 64 for part in path)


# --- operator switch (the audit had no off ramp; the benchmark's "no audit"
# baseline existed only by monkeypatching a private attribute) ---------------


def test_audit_is_disabled_by_the_operator_switch(monkeypatch: Any, tmp_path: Path) -> None:
    from lohra.workflow.audit import resolve_audit_settings

    monkeypatch.setenv("LOHRA_AUDIT", "off")
    settings = resolve_audit_settings()
    assert settings["enabled"] is False

    db = SessionDB(str(tmp_path / "state.db"))
    trail = AuditTrail(db, enabled=False)
    assert trail.record(_canary_event("run-off", "x")) is False
    assert trail.record_gap("run-off", "sink_failure", count=1) is False
    assert trail.flush(timeout=0.1) is True
    assert trail.shutdown() is True
    # No writer thread at all: 20 wakeups/second per chat session, for a feature
    # the operator turned off.
    assert not any(
        thread.name == "workflow-audit" and thread.is_alive()
        for thread in __import__("threading").enumerate()
    )
    assert db.audit_query("run-off")["availability"] == "unavailable"
    db.close()


def test_audit_stays_on_by_default_and_ignores_garbage(monkeypatch: Any) -> None:
    from lohra.workflow.audit import DEFAULT_MAX_EVENTS_PER_RUN, resolve_audit_settings

    monkeypatch.delenv("LOHRA_AUDIT", raising=False)
    monkeypatch.delenv("LOHRA_AUDIT_MAX_EVENTS", raising=False)
    default = resolve_audit_settings()
    assert default == {"enabled": True, "max_events_per_run": DEFAULT_MAX_EVENTS_PER_RUN}

    monkeypatch.setenv("LOHRA_AUDIT", "banana")
    monkeypatch.setenv("LOHRA_AUDIT_MAX_EVENTS", "-3")
    assert resolve_audit_settings() == default


def test_the_service_honours_the_switch(monkeypatch: Any, tmp_path: Path) -> None:
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient

    monkeypatch.setenv("LOHRA_AUDIT", "off")
    db = SessionDB(str(tmp_path / "state.db"))
    spec = {
        "meta": {"name": "quiet"},
        "nodes": [{"id": "one", "type": "agent", "prompt": "hi"}],
    }

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient(
                [{"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                  "usage": {"input_tokens": 1, "output_tokens": 1}}]
            ),
        )

    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        run_id = service.start(spec)["run_id"]
        assert service.status(run_id, wait=True)["status"] == "complete"
    finally:
        service.shutdown()
    page = db.audit_query(run_id)
    assert page["availability"] == "unavailable"
    assert page["events"] == []
    # The byte-identical no-audit path is restored, not merely short-circuited
    # one layer up: the core never builds a safe frame, the engine never hooks.
    assert service._audit_enabled is False
    db.close()


def test_the_ledger_carries_no_index_the_reader_cannot_use(tmp_path: Path) -> None:
    """The only reader is ``audit_query``, which scans a run and filters in
    Python; an index on ``node_id``/``sub_id`` is pure write cost."""
    db = SessionDB(str(tmp_path / "state.db"))
    with db._lock:
        names = {
            row[0]
            for row in db._audit_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'workflow_audit_events' AND name IS NOT NULL"
            ).fetchall()
        }
        plan = db._audit_connection.execute(
            "EXPLAIN QUERY PLAN SELECT seq, payload_json, created_at "
            "FROM workflow_audit_events WHERE run_id = ? ORDER BY seq",
            ("run-1",),
        ).fetchall()
    assert not {name for name in names if name.startswith("idx_wae_")}
    assert "sqlite_autoindex_workflow_audit_events_1" in " ".join(str(row[-1]) for row in plan)
    db.close()


def test_the_switch_leaves_no_segment_marker_for_a_later_gap(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """``off`` must not plant a marker only an audit event could ever clear.

    ``workflow_run_state.audit_segment_id`` is the discriminator for "the
    closing ``segment.completed`` append never landed".  With the trail off no
    append ever happens, so persisting the marker anyway means the FIRST run
    made after auditing is turned back on is born declaring a gap that never
    occurred — the opposite of the "off = byte-identical" contract.
    """
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from lohra.workflow.service import WorkflowService
    from tests.test_loop import FakeClient

    db = SessionDB(str(tmp_path / "state.db"))
    spec = {
        "meta": {"name": "quiet-marker"},
        "nodes": [{"id": "one", "type": "agent", "prompt": "hi"}],
    }

    def factory() -> Agent:
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=FakeClient(
                [{"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                  "usage": {"input_tokens": 1, "output_tokens": 1}}]
            ),
        )

    monkeypatch.setenv("LOHRA_AUDIT", "off")
    quiet = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        run_id = quiet.start(spec)["run_id"]
        assert quiet.status(run_id, wait=True)["status"] == "complete"
    finally:
        quiet.shutdown()
    assert db.run_state_get(run_id)["audit_segment_id"] is None

    # ...and the operator turning the trail back on resumes a clean history.
    monkeypatch.delenv("LOHRA_AUDIT", raising=False)
    loud = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        assert loud.start(resume_run_id=run_id)["run_id"] == run_id
        assert loud.status(run_id, wait=True)["status"] == "complete"
    finally:
        loud.shutdown()
    assert not [
        event for event in db.audit_events(run_id) if event["event_type"] == "audit.gap"
    ]
    db.close()


# --- the closed vocabularies vs. what the producers actually emit ------------
#
# ``_SAFE_STRING_VALUES`` is an allow-list of ENUMS of the harness (outcome,
# cause, authorship), not of content: a value the harness itself emits and the
# allow-list does not know degrades to ``excluded_by_policy``, and the trail
# silently loses exactly the semantics §11.2 promises to keep.  Reviewing the
# two lists by hand is how they drifted apart in the first place, so the scan
# below reads the producers and fails structurally.

_AUDIT_BUILDERS = frozenset(
    {"causal_audit_event", "_audit_control", "_audit_cache", "audit_segment"}
)
# The gateway frame producers: the ``status`` they put on a ``message.complete``
# payload is what ``gateway_audit_event`` copies into ``data["status"]``.
_FRAME_BUILDERS = frozenset({"_observe"})

_PRODUCERS: tuple[tuple[str, frozenset[str]], ...] = (
    ("lohra/workflow/engine.py", _AUDIT_BUILDERS),
    ("lohra/workflow/service.py", _AUDIT_BUILDERS | frozenset({"record_gap"})),
    ("lohra/orchestration/core.py", _FRAME_BUILDERS),
    ("lohra/gateway/session.py", frozenset()),
)


def _called_name(node: Any) -> str | None:
    import ast

    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _string_constants(node: Any) -> list[str]:
    import ast

    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _emitted_pairs(path: Path, calls: frozenset[str]) -> set[tuple[str, str]]:
    """(field, literal) pairs this module hands to an audit/frame producer.

    Deliberately scoped to the producer CALL SITES rather than the whole
    module: ``{"status": "started"}`` in a tool return value never reaches the
    ledger, and widening the allow-list to keep such a scan quiet would be the
    opposite of minimizing the vocabulary.
    """
    import ast

    from lohra.workflow.audit import _SAFE_STRING_VALUES

    fields = set(_SAFE_STRING_VALUES)
    pairs: set[tuple[str, str]] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.targets and isinstance(
            node.targets[0], ast.Name
        ):
            # ``status = "interrupted"`` in the gateway: a turn outcome that
            # becomes a frame payload a few lines later.
            target = node.targets[0].id
            if target in fields and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    pairs.add((target, node.value.value))
        if not isinstance(node, ast.Call) or _called_name(node) not in calls:
            continue
        if _called_name(node) == "record_gap" and len(node.args) > 1:
            pairs.update(("reason", value) for value in _string_constants(node.args[1]))
        for child in ast.walk(node):
            if not isinstance(child, ast.Dict):
                continue
            for key, value in zip(child.keys, child.values):
                if not isinstance(key, ast.Constant) or key.value not in fields:
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    pairs.add((key.value, value.value))
    return pairs


def test_every_value_the_producers_emit_is_in_the_allow_list() -> None:
    from lohra import workflow
    from lohra.workflow.audit import _SAFE_STRING_VALUES

    root = Path(workflow.__file__).resolve().parent.parent.parent
    unknown: list[str] = []
    for relative, calls in _PRODUCERS:
        pairs = _emitted_pairs(root / relative, calls)
        # A scan that finds nothing would pass forever: pin that each producer
        # is still reachable by it.
        assert pairs, f"no audited literal found in {relative}"
        unknown.extend(
            f"{relative}: {field}={value!r}"
            for field, value in sorted(pairs)
            if value not in _SAFE_STRING_VALUES[field]
        )
    assert not unknown, (
        "these values reach the audit sink but are not allow-listed, so the "
        "trail records `excluded_by_policy` instead of the outcome/cause/"
        "authorship the spec promises: " + "; ".join(unknown)
    )


def test_the_newly_allowed_values_survive_both_sanitization_passes() -> None:
    """The structural scan proves the lists agree; this proves what that buys.

    Every event is sanitized twice (``AuditTrail.record`` and, defensively,
    ``SessionDB.audit_append``), so a value that is legible once but not twice
    is still lost.
    """
    from lohra.workflow.audit import gateway_audit_event

    context = CausalContext(
        run_id="run-vocab", segment_id="seg-1", node_path=("cell",),
        cell_id="hash-1", role="leaf",
    )
    interrupted = gateway_audit_event(
        {
            "method": "event",
            "params": {"type": "message.complete", "payload": {"status": "interrupted"}},
        },
        context,
        sub_id="sub-1",
    )
    cases = (
        (interrupted, "status", "interrupted"),
        (
            {
                "schema_version": 1, "event_type": "cache.unavailable",
                "provenance": "unavailable",
                "identity": {"run_id": "run-vocab", "node_path": ["cell"]},
                "data": {"reason": "lookup_failed"},
            },
            "reason", "lookup_failed",
        ),
        (
            {
                "schema_version": 1, "event_type": "cache.unavailable",
                "provenance": "unavailable",
                "identity": {"run_id": "run-vocab", "node_path": ["cell"]},
                "data": {"reason": "store_failed"},
            },
            "reason", "store_failed",
        ),
        (
            {
                "schema_version": 1, "event_type": "cache.stored",
                "provenance": "observed",
                "identity": {"run_id": "run-vocab", "node_path": ["cell"]},
                "data": {"source": "human_checkpoint"},
            },
            "source", "human_checkpoint",
        ),
    )
    for event, field, expected in cases:
        once = sanitize_audit_event(event)
        twice = sanitize_audit_event(once)
        assert once["data"][field] == expected
        assert twice["data"][field] == expected


# --- the closed event-type set vs. the spec that publishes it ----------------


SPEC_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "specs" / "08-workflow-node-audit.md"
)
_VOCABULARY_ANCHOR = "Vocabulário fechado de `event_type`"


def _spec_event_types() -> set[str]:
    """The list §11.2 publishes, read out of the fenced block that follows the
    anchor — so a type added to the code and not to the spec fails here."""
    text = SPEC_PATH.read_text(encoding="utf-8")
    match = re.search(
        re.escape(_VOCABULARY_ANCHOR) + r".*?```text\n(.*?)```", text, re.DOTALL
    )
    assert match is not None, "spec §11.2 event-type block not found — renamed/moved?"
    return {line.strip() for line in match.group(1).splitlines() if line.strip()}


def test_the_event_type_vocabulary_matches_the_spec_exactly() -> None:
    """``NODE_SPECS ≡ STRATEGIES ≡ skill``, applied to the ledger (issue #64).

    The producer's set is closed: a type outside it is persisted as
    ``audit.unavailable``, silently. The spec is where an operator reads what
    the trail can contain, so the two drifting apart makes the document a lie in
    exactly the direction nobody notices.
    """
    from lohra.workflow.audit import _EVENT_TYPES

    published = _spec_event_types()
    assert published - _EVENT_TYPES == set(), "spec names an event type the code never emits"
    assert _EVENT_TYPES - published == set(), "new audit event type missing from spec §11.2"


def test_the_reroute_type_and_its_channel_are_both_published() -> None:
    """The one this issue added, pinned by name: a reader of §11.2 has to be
    able to learn that a re-route is a typed event and which surfaces can carry
    one, without reading ``audit.py``."""
    from lohra.workflow.audit import NODE_REROUTED, REROUTE_CHANNELS

    text = SPEC_PATH.read_text(encoding="utf-8")
    assert NODE_REROUTED in _spec_event_types()
    for channel in REROUTE_CHANNELS:
        assert f"`{channel}`" in text, f"channel {channel!r} is not published in the spec"
