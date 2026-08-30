"""Reproduce SUP-02 deterministic status/audit cadence measurements.

Run from ``backend/`` with tiktoken 0.9.0 available:
``python ../docs/history/evidence/sup-02-monitor-bench.py``.
The captured 2026-08-30 output is the adjacent JSON file. Timing varies by host.
"""

from __future__ import annotations
import json
import statistics
import threading
import time
from pathlib import Path
from uuid import uuid4
import tiktoken
from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService


class ScriptedClient(ModelClient):
    """Deterministic no-network client used only by this benchmark."""

    def __init__(self, responder):
        self._responder = responder

    @staticmethod
    def _prompt(kwargs):
        messages = kwargs.get("messages") or []
        return " ".join(
            message.get("content", "")
            for message in messages
            if isinstance(message.get("content"), str)
        )

    def create(self, **kwargs):
        text = self._responder(self._prompt(kwargs))
        return {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


ENC = tiktoken.get_encoding("o200k_base")
SPEC = {
    "meta": {"name": "sup02-monitor"},
    "nodes": [{"id": "slow", "type": "agent", "prompt": "go"}],
}


def tokens(obj):
    return len(ENC.encode(json.dumps(obj, separators=(",", ":"), ensure_ascii=False)))


def run(cadence: float, duration: float = 6.2):
    root = Path("/tmp") / f"sup02-{cadence}-{uuid4().hex}"
    root.mkdir()
    db = SessionDB(str(root / "state.db"))
    entered = threading.Event()
    release = threading.Event()
    provider_entered = [None]

    def responder(_prompt):
        provider_entered[0] = time.perf_counter()
        entered.set()
        release.wait(15)
        return "R"

    def factory():
        return Agent(
            model="audit-test",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    event_rows = []

    def sink(run_id, kind, payload):
        event_rows.append((time.perf_counter(), kind, payload))

    svc = WorkflowService(
        base_child_factory=factory,
        db=db,
        home=root,
        on_event=sink,
        run_concurrency=1,
        max_runs=1,
    )
    started = time.perf_counter()
    out = svc.start(SPEC, {}, token_budget=1000)
    rid = out["run_id"]
    assert entered.wait(5)
    timer = threading.Timer(duration, release.set)
    timer.start()
    status_rows = []
    audit_rows = []
    cursor = 0
    query_ms = []
    next_at = provider_entered[0] + cadence
    while next_at <= provider_entered[0] + duration:
        time.sleep(max(0, next_at - time.perf_counter()))
        q0 = time.perf_counter()
        status = svc.status(rid)
        q1 = time.perf_counter()
        a0 = time.perf_counter()
        audit = db.audit_query(rid, after_seq=cursor, limit=100)
        a1 = time.perf_counter()
        cursor = audit["page"]["next_after_seq"]
        observed = time.perf_counter()
        status_rows.append((observed, status, tokens(status)))
        audit_rows.append((observed, audit, tokens(audit)))
        query_ms.append(((q1 - q0) * 1000, (a1 - a0) * 1000))
        next_at += cadence
    release.set()
    terminal = svc.status(rid, wait=True, timeout=5)
    time.sleep(0.1)
    aq0 = time.perf_counter()
    terminal_audit = db.audit_query(rid, after_seq=cursor, limit=100)
    aq1 = time.perf_counter()
    svc.shutdown()
    db.close()
    timer.cancel()
    leaf_seen = []
    for at, a, _ in audit_rows:
        if any(e["event_type"] == "leaf.started" for e in a["events"]):
            leaf_seen.append(at)
    node_running = [
        at for at, k, p in event_rows if k == "node" and p.get("state") == "running"
    ]
    return {
        "cadence_s": cadence,
        "duration_s": duration,
        "polls": len(status_rows),
        "provider_tokens_spent_by_monitor_queries": 0,
        "status_payload_estimated_o200k_tokens": sum(r[2] for r in status_rows),
        "audit_payload_estimated_o200k_tokens": sum(r[2] for r in audit_rows),
        "status_query_ms": {
            "mean": statistics.mean(x[0] for x in query_ms),
            "max": max(x[0] for x in query_ms),
        },
        "audit_query_ms": {
            "mean": statistics.mean(x[1] for x in query_ms),
            "max": max(x[1] for x in query_ms),
        },
        "first_status_latency_from_provider_entered_s": status_rows[0][0]
        - provider_entered[0],
        "first_leaf_audit_latency_from_provider_entered_s": (
            leaf_seen[0] - provider_entered[0]
        )
        if leaf_seen
        else None,
        "node_push_latency_from_start_s": node_running[0] - started
        if node_running
        else None,
        "terminal_status": terminal["status"],
        "terminal_audit_events": len(terminal_audit["events"]),
        "terminal_audit_query_ms": (aq1 - aq0) * 1000,
        "status_states": [r[1]["status"] for r in status_rows],
        "audit_events_per_poll": [len(r[1]["events"]) for r in audit_rows],
    }


print(json.dumps([run(1.0), run(3.0)], indent=2))
