"""SUP-03 exact-occurrence and first-turn steering contracts."""

import threading
from dataclasses import replace

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.causality import CausalContext
from tests.test_loop import FakeClient, _text_response


def test_first_turn_steer_checks_occurrence_atomically_and_settles_read():
    started = threading.Event()
    release = threading.Event()
    settled = []
    clients = []

    class FirstCallGated(FakeClient):
        def create(self, **kwargs):
            if not self.calls:
                started.set()
                release.wait(5)
            return super().create(**kwargs)

    def factory():
        client = FirstCallGated([_text_response("first"), _text_response("corrected")])
        clients.append(client)
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=client,
        )

    causal = CausalContext(
        run_id="run", segment_id="seg", node_path=("leaf",), cell_id="cell", role="leaf"
    )
    db = SessionDB(":memory:")
    core = OrchestrationCore(db, factory)
    try:
        sub_id = core.spawn("start", causal_context=causal)
        assert started.wait(5)  # occupied in the FIRST provider call

        stale = replace(causal, turn=1)
        refused = core.steer_active(sub_id, "wrong turn", expected_causal=stale)
        assert "causal occurrence changed" in refused["error"]

        accepted = core.steer_active(
            sub_id, "first-turn correction", expected_causal=causal, on_settle=settled.append
        )
        assert accepted == {"ok": True, "queued": True}
        assert settled == []  # acceptance is not read and cannot preempt the call

        release.set()
        result = core.collect(sub_id, wait=True, timeout=5)
        assert result["status"] == "complete"
        assert result["output"] == "corrected"
        assert settled == ["read"]
        assert len(clients[0].calls) == 2
        assert "first-turn correction" in str(clients[0].calls[1]["messages"])
        assert "wrong turn" not in str(clients[0].calls[1]["messages"])
    finally:
        release.set()
        core.shutdown()
        db.close()
