"""Owned synthetic subprocess held immediately before/after its final commit."""

import json
from pathlib import Path
import sys
from threading import Event

from dataclasses import replace
import pytest

from lohra.agent.types import Usage
from lohra.providers.transports.anthropic_messages import AnthropicMessagesTransport
from lohra.state import SessionDB
from lohra.workflow import quiescence
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.financial_settlement import SpendSnapshot
from tests.pipeline_deadlines import control_pipeline_deadlines
from tests.test_workflow_pipeline_accounting import _service, _spec
from tests.test_workflow_post_drain_accounting import VECTOR, meter


def main():
    directory, phase = Path(sys.argv[1]), sys.argv[2]

    def forbid(event, args):
        if event in {"socket.connect", "subprocess.Popen", "os.system"}:
            raise AssertionError("no network, nested subprocess or shell in crash child")

    sys.addaudithook(forbid)
    live, expire, sealed, release, stop = (Event() for _ in range(5))
    with pytest.MonkeyPatch.context() as patch:
        control_pipeline_deadlines(patch, {"a": expire})
        patch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.02)
        normalize = AnthropicMessagesTransport.normalize_response
        patch.setattr(AnthropicMessagesTransport, "normalize_response",
                      lambda transport, raw: replace(normalize(transport, raw), usage=Usage(*VECTOR)))
        seal = WorkflowEngine._seal

        def sealed_result(engine, result):
            seal(engine, result)
            sealed.set()

        patch.setattr(WorkflowEngine, "_seal", sealed_result)
        write = SpendSnapshot.write

        def stopped_at_commit(snapshot, db, run_id, fence):
            if phase == "after":
                assert write(snapshot, db, run_id, fence)
            print(json.dumps({"phase": phase, "run_id": run_id, "row": meter(db, run_id),
                              "sealed_input": state.engine._result.tokens_in,
                              "budget": state.engine.budget.tokens_spent}), flush=True)
            assert stop.wait(60), "parent did not terminate its owned crash child"
            return write(snapshot, db, run_id, fence)

        patch.setattr(SpendSnapshot, "write", stopped_at_commit)

        def respond(prompt):
            if prompt.startswith("late"):
                live.set()
                assert release.wait(5)
            return "BILLED"

        db = SessionDB(directory / "state.db")
        service = _service(db, directory, respond)
        spec = _spec(tail=False)
        spec["nodes"][0]["depends_on"] = ["prefix"]
        spec["nodes"].insert(0, {"id": "prefix", "type": "agent", "prompt": "prefix"})
        run_id = service.start(spec)["run_id"]
        state = service._runs[run_id]
        assert live.wait(5)
        expire.set()
        assert sealed.wait(5)
        release.set()
        state.future.result(timeout=60)


if __name__ == "__main__":
    main()
