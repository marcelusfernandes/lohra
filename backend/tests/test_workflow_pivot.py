"""SUP-04: adapted-spec resume and content-addressed pivot evidence.

No real provider is called. ``ControlledProvider`` observes the model selected by
the real workflow configure hook and refuses one slug deterministically. Every
successful leaf costs exactly 8 fixture tokens (5 input + 3 output).

No node spec here declares ``retries`` — with ONE named exception at the end of
the file — so E1's same-route re-spawn on a terminal provider failure (#43)
never fires: that class is opt-in. Adding the field to a fixture below would
double its bad-route call counts.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import library
from lohra.workflow.runstate_store import RunStateStore
from lohra.workflow.service import WorkflowService
from tests.test_loop import _text_response

LEAF_COST = 8
BAD_MODEL = "missing-model"
GOOD_MODEL = "qualified-model"
DEFAULT_MODEL = "qualified-default"
AUTH_MODEL = "auth-rejected"


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


class ControlledProvider:
    """One fixed route whose catalog changes; only the model slug varies."""

    def __init__(self, calls: list[tuple[str, str]], *, chat_completions: bool = False) -> None:
        self._calls = calls
        self._chat_completions = chat_completions

    @staticmethod
    def _prompt(kwargs) -> str:
        messages = kwargs.get("messages") or []
        return " ".join(
            message.get("content", "")
            for message in messages
            if isinstance(message.get("content"), str)
        )

    def create(self, **kwargs):
        model = kwargs["model"]
        prompt = self._prompt(kwargs)
        self._calls.append((model, prompt))
        if model == BAD_MODEL:
            raise RuntimeError("controlled provider rejected unknown model")
        if model == AUTH_MODEL:
            error = RuntimeError("controlled provider rejected credentials")
            error.status_code = 401
            raise error
        if kwargs.get("reasoning_effort") is not None:
            raise TypeError("controlled provider rejected unsupported reasoning_effort")
        text = (
            '{"refuted": false, "reason": "survives"}'
            if "Try hard to REFUTE" in prompt
            else f"ok:{model}"
        )
        if self._chat_completions:
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            }
        return _text_response(text)

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)

    def close(self) -> None:
        return None


def _service(db, home, calls, *, provider="anthropic"):
    client = ControlledProvider(calls, chat_completions=provider != "anthropic")

    def factory():
        return Agent(
            model=DEFAULT_MODEL,
            provider=get_provider_profile(provider),
            client=client,
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


def _spec(*, pivot_model: str, version: int = 1) -> dict:
    return {
        "meta": {"name": "sup04-controlled-pivot", "version": version},
        "nodes": [
            {"id": "stable", "type": "agent", "prompt": "independent stable work"},
            {
                "id": "target",
                "type": "agent",
                "prompt": "independent routed work",
                "model": pivot_model,
            },
        ],
    }


def _finish(service, run_id):
    return service.status(run_id, wait=True, timeout=10)


def test_explicit_adapted_resume_reuses_unchanged_cell_on_the_same_route(db, tmp_path):
    """Existing full-spec resume is already the narrow pivot mechanism.

    The status supplies the diagnosis; the adapted spec changes only the bad
    model on the same controlled provider/credential route. No steering or new
    helper participates.
    """
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        original = _spec(pivot_model=BAD_MODEL)
        run_id = service.start(original, {}, token_budget=3 * LEAF_COST)["run_id"]
        failed = _finish(service, run_id)

        assert failed["status"] == "degraded"
        assert any("target" in fault for fault in failed["faults"])
        assert calls == [
            (DEFAULT_MODEL, "independent stable work"),
            (BAD_MODEL, "independent routed work"),
        ]

        adapted = deepcopy(original)
        adapted["nodes"][1]["model"] = GOOD_MODEL
        launched = service.start(adapted, resume_run_id=run_id)
        # This fixture's two nodes are DELIBERATELY independent (the pivot only
        # touches 'target'; 'stable' must replay untouched) — issue #49's lint
        # correctly warns on that shape, so assert on the pivot outcome
        # (run_id/status) rather than the full dict.
        assert launched["run_id"] == run_id
        assert launched["status"] == "started"
        recovered = _finish(service, run_id)

        assert recovered["status"] == "complete"  # this stretch recovered cleanly
        assert any("target" in fault for fault in recovered["faults_total"])
        assert recovered["outputs"] == {
            "stable": f"ok:{DEFAULT_MODEL}",
            "target": f"ok:{GOOD_MODEL}",
        }
        assert calls == [
            (DEFAULT_MODEL, "independent stable work"),
            (BAD_MODEL, "independent routed work"),
            (GOOD_MODEL, "independent routed work"),
        ]
        # One cache hit, one re-executed cell; the original ceiling is inherited.
        assert recovered["token_budget"] == {
            "total": 3 * LEAF_COST,
            "spent": 2 * LEAF_COST,
            "remaining": LEAF_COST,
            "overrun": 0,
            "overrun_max": 0,
        }
    finally:
        service.shutdown()


def test_changing_spec_identity_rekeys_even_untouched_cells(db, tmp_path):
    """Negative result: bumping meta.version turns every cell into a new cell."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        original = _spec(pivot_model=BAD_MODEL)
        run_id = service.start(original, {}, token_budget=5 * LEAF_COST)["run_id"]
        assert _finish(service, run_id)["status"] == "degraded"

        adapted = _spec(pivot_model=GOOD_MODEL, version=2)
        service.start(adapted, resume_run_id=run_id)
        recovered = _finish(service, run_id)

        assert recovered["outputs"]["target"] == f"ok:{GOOD_MODEL}"
        assert calls == [
            (DEFAULT_MODEL, "independent stable work"),
            (BAD_MODEL, "independent routed work"),
            (DEFAULT_MODEL, "independent stable work"),
            (GOOD_MODEL, "independent routed work"),
        ]
        assert recovered["token_budget"]["spent"] == 3 * LEAF_COST
    finally:
        service.shutdown()



def test_fresh_run_repays_every_cell_instead_of_pivoting(db, tmp_path):
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        original_id = service.start(
            _spec(pivot_model=BAD_MODEL), {}, token_budget=5 * LEAF_COST
        )["run_id"]
        assert _finish(service, original_id)["status"] == "degraded"

        fresh_id = service.start(
            _spec(pivot_model=GOOD_MODEL), {}, token_budget=5 * LEAF_COST
        )["run_id"]
        fresh = _finish(service, fresh_id)

        assert fresh_id != original_id
        assert fresh["status"] == "complete"
        assert [model for model, _prompt in calls] == [
            DEFAULT_MODEL,
            BAD_MODEL,
            DEFAULT_MODEL,
            GOOD_MODEL,
        ]
        assert fresh["token_budget"]["spent"] == 2 * LEAF_COST
        # The first run's successful cell is still paid there; a new run cannot see it.
        assert service.status(original_id)["token_budget"]["spent"] == LEAF_COST
    finally:
        service.shutdown()


def test_rigor_fanout_pivot_reruns_the_whole_incomplete_panel(db, tmp_path):
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    original = {
        "meta": {"name": "sup04-fanout", "version": 1},
        "nodes": [
            {"id": "stable", "type": "agent", "prompt": "stable"},
            {
                "id": "jury",
                "type": "verify",
                "finding": "claim",
                "skeptics": 2,
                "model": BAD_MODEL,
            },
        ],
    }
    try:
        run_id = service.start(original, {}, token_budget=10_000)["run_id"]
        assert _finish(service, run_id)["status"] == "degraded"

        adapted = deepcopy(original)
        adapted["nodes"][1]["model"] = GOOD_MODEL
        service.start(adapted, resume_run_id=run_id)
        recovered = _finish(service, run_id)

        assert recovered["outputs"]["jury"]["skeptics"] == 2
        assert recovered["outputs"]["jury"]["survived"] is True
        # stable hit; the failed whole-panel cell had no cache row, so both
        # skeptics execute on the qualified model.
        assert [model for model, _prompt in calls].count(DEFAULT_MODEL) == 1
        assert [model for model, _prompt in calls].count(BAD_MODEL) == 2
        assert [model for model, _prompt in calls].count(GOOD_MODEL) == 2
        assert recovered["token_budget"]["spent"] == 3 * LEAF_COST
    finally:
        service.shutdown()


def test_nested_pivot_reuses_unchanged_child_cell_and_reruns_only_changed_child(
    db, tmp_path, monkeypatch
):
    calls: list[tuple[str, str]] = []
    child = {
        "meta": {"name": "child", "version": 1},
        "nodes": [
            {"id": "stable", "type": "agent", "prompt": "nested stable"},
            {"id": "target", "type": "agent", "prompt": "nested target", "model": BAD_MODEL},
        ],
    }
    templates = {"child": child}
    monkeypatch.setattr(library, "get_template", lambda _home, ref: templates.get(ref))
    service = _service(db, tmp_path, calls)
    parent = {
        "meta": {"name": "sup04-nested", "version": 1},
        "nodes": [
            {"id": "parent_stable", "type": "agent", "prompt": "parent stable"},
            {"id": "nested", "type": "workflow", "ref": "child"},
        ],
    }
    try:
        run_id = service.start(parent, {}, token_budget=5 * LEAF_COST)["run_id"]
        assert _finish(service, run_id)["status"] == "degraded"

        adapted_child = deepcopy(child)
        adapted_child["nodes"][1]["model"] = GOOD_MODEL
        templates["child"] = adapted_child
        service.start(parent, resume_run_id=run_id)
        recovered = _finish(service, run_id)

        assert recovered["outputs"]["nested"] == {
            "stable": f"ok:{DEFAULT_MODEL}",
            "target": f"ok:{GOOD_MODEL}",
        }
        assert calls.count((DEFAULT_MODEL, "parent stable")) == 1
        assert calls.count((DEFAULT_MODEL, "nested stable")) == 1
        assert calls.count((BAD_MODEL, "nested target")) == 1
        assert calls.count((GOOD_MODEL, "nested target")) == 1
        assert recovered["token_budget"]["spent"] == 3 * LEAF_COST
    finally:
        service.shutdown()


def test_pivot_inherits_cumulative_budget_and_refuses_an_exhausted_resume(db, tmp_path):
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    original = {
        "meta": {"name": "sup04-budget", "version": 1},
        "nodes": [
            {"id": "target", "type": "agent", "prompt": "target", "model": BAD_MODEL},
            {"id": "stable", "type": "agent", "prompt": "stable"},
        ],
    }
    try:
        run_id = service.start(original, {}, token_budget=LEAF_COST)["run_id"]
        first = _finish(service, run_id)
        assert first["status"] == "degraded"
        assert first["token_budget"] == {
            "total": LEAF_COST, "spent": LEAF_COST, "remaining": 0,
            "overrun": 0, "overrun_max": 0,
        }

        adapted = deepcopy(original)
        adapted["nodes"][0]["model"] = GOOD_MODEL
        refused = service.start(adapted, resume_run_id=run_id)

        assert "error" in refused
        assert "already spent 8 tokens" in refused["error"]
        assert "Only a human may authorize a larger cap" in refused["error"]
        unchanged = service.status(run_id)
        assert unchanged["status"] == "degraded"
        assert unchanged["token_budget"] == {
            "total": LEAF_COST, "spent": LEAF_COST, "remaining": 0,
            "overrun": 0, "overrun_max": 0,
        }
        assert (GOOD_MODEL, "target") not in calls
    finally:
        service.shutdown()


def test_unsupported_optional_parameter_can_be_dropped_on_the_same_route(db, tmp_path):
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls, provider="openai")
    original = {
        "meta": {"name": "sup04-optional-param", "version": 1},
        "nodes": [
            {"id": "stable", "type": "agent", "prompt": "stable"},
            {
                "id": "target",
                "type": "agent",
                "prompt": "optional effort",
                "model": GOOD_MODEL,
                "effort": "high",
            },
        ],
    }
    try:
        run_id = service.start(original, {}, token_budget=3 * LEAF_COST)["run_id"]
        assert _finish(service, run_id)["status"] == "degraded"

        adapted = deepcopy(original)
        del adapted["nodes"][1]["effort"]
        service.start(adapted, resume_run_id=run_id)
        recovered = _finish(service, run_id)

        assert recovered["outputs"]["target"] == f"ok:{GOOD_MODEL}"
        assert len(calls) == 3  # stable + refused parameter + corrected target
        assert recovered["token_budget"]["spent"] == 2 * LEAF_COST
    finally:
        service.shutdown()


def test_401_pauses_the_run_and_arms_no_auto_resume(db, tmp_path):
    """A refused credential is terminal provider evidence, not a retry signal.

    It now stops the run (#43, opção C) — but the auto-resume allow-list is
    UNTOUCHED: quota is still the only reason that schedules itself back. A
    ``route_fault`` with a ``resume_at`` would promise a comeback nobody can
    deliver, since no amount of waiting supplies a credential."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    try:
        run_id = service.start(
            _spec(pivot_model=AUTH_MODEL), {}, token_budget=3 * LEAF_COST
        )["run_id"]
        result = _finish(service, run_id)

        assert result["status"] == "paused"
        assert result["reason"] == "route_fault"
        assert result.get("resume_at") is None
        # The dead route, named where the agent reads it — no prose to parse.
        assert result["route"]["node_id"] == "target"
        assert result["route"]["model"] == AUTH_MODEL
        assert result["route"]["error_kind"] == "auth_failed"
        assert "credential/billing route" in result["hint"]
        assert any("credentials" in fault for fault in result["faults"])
        # Nothing is scheduled to bring this run back: the quota allow-list does
        # not admit a route fault, and a restart re-arms nothing either.
        assert service.rearm_pending_resumes() == 0
        # Credential repair or route crossing is intentionally absent: SUP-01
        # classifies both as a human decision.
        assert calls == [
            (DEFAULT_MODEL, "independent stable work"),
            (AUTH_MODEL, "independent routed work"),
        ]
    finally:
        service.shutdown()


def test_a_route_fault_resume_with_an_adapted_spec_replays_every_paid_cell(db, tmp_path):
    """The pause is only worth having if the remedy is cheap (SUP-04).

    Resuming the SAME ``run_id`` with the adapted spec — one node's route
    changed, everything else byte-identical — replays the cells the run already
    paid for and spawns only what died. That is the whole argument for stopping
    instead of degrading: the work survives the pause."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    original = _spec(pivot_model=AUTH_MODEL)
    try:
        run_id = service.start(original, {}, token_budget=3 * LEAF_COST)["run_id"]
        assert _finish(service, run_id)["reason"] == "route_fault"
        assert len(calls) == 2

        adapted = deepcopy(original)
        adapted["nodes"][1]["model"] = GOOD_MODEL  # same provider, same billing route
        service.start(adapted, resume_run_id=run_id)
        recovered = _finish(service, run_id)

        assert recovered["status"] == "complete"
        assert recovered.get("reason") is None
        assert "route" not in recovered  # the pause, and its dead route, are gone
        assert recovered["outputs"]["stable"] == f"ok:{DEFAULT_MODEL}"
        assert recovered["outputs"]["target"] == f"ok:{GOOD_MODEL}"
        # ONE new call: 'stable' replayed from the cache instead of re-billing.
        assert len(calls) == 3
        assert calls[2] == (GOOD_MODEL, "independent routed work")
        assert recovered["token_budget"]["spent"] == 2 * LEAF_COST
    finally:
        service.shutdown()


def test_an_exhausted_series_pauses_and_the_adapted_resume_seals_clean(db, tmp_path):
    """#43 x Q2, end to end: the declared series is the ONE ``retries`` in this file.

    A node with ``retries: 1`` on a dead route spends both attempts there and
    pauses (`route_fault`). The adapted resume — same provider, same billing
    route, one model slug changed — finishes, and the run seals **`complete`**:
    the numbered attempts were the pause's evidence, not a verdict about the
    spec, so ``prior_degraded`` never latches. What survives the discount is the
    price: ``leaf_respawns`` still reports the extra leaf the dead route cost,
    cumulative across both stretches."""
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    original = _spec(pivot_model=BAD_MODEL)
    original["nodes"][1]["retries"] = 1  # N = 2 attempts, both on the dead route
    try:
        run_id = service.start(original, {}, token_budget=4 * LEAF_COST)["run_id"]
        paused = _finish(service, run_id)
        assert paused["status"] == "paused"
        assert paused["reason"] == "route_fault"
        assert paused["route"]["model"] == BAD_MODEL
        assert len(calls) == 3  # 'stable', then both attempts of 'target'
        assert paused["leaf_respawns"] == 1  # N - 1

        adapted = deepcopy(original)
        adapted["nodes"][1]["model"] = GOOD_MODEL
        service.start(adapted, resume_run_id=run_id)
        recovered = _finish(service, run_id)

        assert recovered["status"] == "complete"
        assert recovered["outputs"]["target"] == f"ok:{GOOD_MODEL}"
        # The whole point of the pause: the run comes back CLEAN. Every fault it
        # collected on the way is still reported, and none of them is a verdict.
        assert recovered["faults"] == []
        # 2 numbered attempts + the exhaustion verdict, which IS the pause fault.
        assert len(recovered["faults_total"]) == 3
        # The boolean that actually travels between stretches never latched.
        row = RunStateStore(db, holder="assertion").load(run_id)
        assert row.prior_degraded is False
        # ...and the price of the dead route is still on the bill, cumulative.
        assert recovered["leaf_respawns"] == 1
        assert len(calls) == 4  # 'stable' replayed from cache; only 'target' re-ran
    finally:
        service.shutdown()


def test_pivot_rehydrates_original_args_when_resume_omits_them(db, tmp_path):
    calls: list[tuple[str, str]] = []
    service = _service(db, tmp_path, calls)
    original = {
        "meta": {"name": "sup04-args", "version": 1},
        "nodes": [
            {"id": "stable", "type": "agent", "prompt": "stable"},
            {
                "id": "target",
                "type": "agent",
                "prompt": "route context: ${args.context}",
                "model": BAD_MODEL,
            },
        ],
    }
    try:
        run_id = service.start(
            original, {"context": "original-value"}, token_budget=3 * LEAF_COST
        )["run_id"]
        assert _finish(service, run_id)["status"] == "degraded"

        adapted = deepcopy(original)
        adapted["nodes"][1]["model"] = GOOD_MODEL
        service.start(adapted, resume_run_id=run_id)
        recovered = _finish(service, run_id)

        assert recovered["outputs"]["target"] == f"ok:{GOOD_MODEL}"
        assert calls.count((DEFAULT_MODEL, "stable")) == 1
        assert calls.count((GOOD_MODEL, "route context: original-value")) == 1
    finally:
        service.shutdown()
