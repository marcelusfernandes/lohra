"""Tests for the rigor nodes: verify / judge_panel / loop_until_dry (Milestone E)."""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


class ScriptedClient(ModelClient):
    """Replies based on the prompt text (a responder callable)."""

    def __init__(self, responder):
        self._responder = responder

    def _prompt(self, kwargs):
        msgs = kwargs.get("messages") or []
        return " ".join(m.get("content", "") for m in msgs if isinstance(m.get("content"), str))

    def create(self, **kwargs):
        return _text_response(self._responder(self._prompt(kwargs)))

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, responder):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory)


def _run(core, node):
    spec = validate_spec({"meta": {"name": "r"}, "nodes": [node]})
    return WorkflowEngine(core, budget=Budget()).run(spec, {"claim": "the sky is green"})


# --- verify (adversarial) ---


def test_verify_majority_refute_kills_finding(db):
    core = _core(db, lambda p: '{"refuted": true, "reason": "wrong"}')
    try:
        result = _run(core, {"id": "v", "type": "verify", "finding": "${args.claim}",
                             "skeptics": 3, "kill_if_majority_refute": True})
        out = result.outputs["v"]
        assert out["survived"] is False
        assert out["finding"] is None
        assert out["refuted"] == 3
    finally:
        core.shutdown()


def test_verify_minority_refute_survives(db):
    # 2 of 3 say not-refuted -> survives
    replies = iter(['{"refuted": true}', '{"refuted": false}', '{"refuted": false}'])
    core = _core(db, lambda p: next(replies))
    try:
        result = _run(core, {"id": "v", "type": "verify", "finding": "${args.claim}", "skeptics": 3})
        out = result.outputs["v"]
        assert out["survived"] is True
        assert out["finding"] == "the sky is green"
    finally:
        core.shutdown()


def test_verify_uses_distinct_lenses(db):
    seen_lenses = []

    def responder(prompt):
        for lens in ("security", "correctness", "performance"):
            if lens in prompt:
                seen_lenses.append(lens)
        return '{"refuted": false}'

    core = _core(db, responder)
    try:
        _run(core, {"id": "v", "type": "verify", "finding": "${args.claim}", "skeptics": 3,
                    "lenses": ["security", "correctness", "performance"]})
        assert set(seen_lenses) == {"security", "correctness", "performance"}
    finally:
        core.shutdown()


# --- judge_panel ---


def test_judge_panel_picks_highest_scored_and_synthesizes(db):
    def responder(prompt):
        if "Score this attempt" in prompt:
            # score the attempt that contains "GOOD" high, others low
            return '{"score": 9}' if "GOOD" in prompt else '{"score": 2}'
        if "synthesize" in prompt.lower() or "WINNER" in prompt:
            return "SYNTHESIZED from winner"
        return "GOOD attempt" if "good-angle" in prompt else "weak attempt"

    core = _core(db, responder)
    try:
        result = _run(core, {
            "id": "jp", "type": "judge_panel", "judges": 1,
            "attempts": [{"type": "agent", "prompt": "good-angle"},
                         {"type": "agent", "prompt": "weak-angle"}],
            "synthesize": {"type": "agent", "prompt": "synthesize ${winner}"},
        })
        assert result.outputs["jp"] == "SYNTHESIZED from winner"
    finally:
        core.shutdown()


# --- loop_until_dry ---


def test_loop_until_dry_stops_after_k_empty(db):
    rounds = iter(["found-1", "found-2", "", ""])  # two finds, then empty

    def responder(prompt):
        try:
            return next(rounds)
        except StopIteration:
            return ""

    core = _core(db, responder)
    try:
        result = _run(core, {"id": "loop", "type": "loop_until_dry", "body": {"type": "agent",
                       "prompt": "find more"}, "stop_after_k_empty": 1, "max_rounds": 10})
        assert result.outputs["loop"] == ["found-1", "found-2"]  # stopped at first empty
    finally:
        core.shutdown()


def test_loop_until_dry_respects_max_rounds(db):
    core = _core(db, lambda p: "always-something")  # never empty
    try:
        result = _run(core, {"id": "loop", "type": "loop_until_dry", "body": {"type": "agent",
                       "prompt": "go"}, "stop_after_k_empty": 2, "max_rounds": 3})
        assert len(result.outputs["loop"]) == 3  # capped at max_rounds
    finally:
        core.shutdown()
