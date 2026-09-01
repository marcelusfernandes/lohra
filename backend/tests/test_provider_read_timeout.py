"""Provider READ timeout — classification + a didactic workflow fault (issue #48).

Companion to ``test_workflow_quota.py`` (same shape, different failure kind): a
read timeout is silence past the configured HTTP window, not a size problem —
leaves stream, so what actually starves is time-to-first-byte or a stalled
chunk. Two layers, bottom-up:
- classification: ``httpx.TimeoutException`` and both SDKs' ``APITimeoutError``
  classify as ``TIMEOUT``; an ordinary error still does not;
- the fault that reaches a workflow spec author names BOTH timeout knobs
  (``LOHRA_PROVIDER_READ_TIMEOUT`` for the HTTP window, the node's own
  ``timeout:``/``LEAF_TIMEOUT`` for the leaf deadline) instead of an opaque
  ``str(exc)``.
"""

import anthropic
import httpx
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.providers.errors import TIMEOUT, classify_provider_error
from lohra.providers.timeouts import ENV_VAR as READ_TIMEOUT_ENV_VAR
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from lohra.workflow.strategies import LEAF_TIMEOUT
from tests.test_workflow_pipeline import ScriptedClient


def _request():
    return httpx.Request("POST", "https://api.example.test/v1/messages")


# --- 1. classification --------------------------------------------------


def test_httpx_timeout_exception_is_timeout():
    assert classify_provider_error(httpx.ReadTimeout("timed out", request=_request())) == TIMEOUT


def test_httpx_connect_timeout_is_also_timeout():
    # ConnectTimeout/WriteTimeout/PoolTimeout all subclass TimeoutException —
    # the classifier catches the base, not just the read case.
    assert (
        classify_provider_error(httpx.ConnectTimeout("no connect", request=_request())) == TIMEOUT
    )


def test_openai_api_timeout_error_is_timeout():
    assert classify_provider_error(openai.APITimeoutError(request=_request())) == TIMEOUT


def test_anthropic_api_timeout_error_is_timeout():
    assert classify_provider_error(anthropic.APITimeoutError(request=_request())) == TIMEOUT


def test_ordinary_errors_are_not_timeout():
    assert classify_provider_error(RuntimeError("boom")) is None


def test_prose_mentioning_timeout_is_not_classified():
    # Structural only, same discipline as the quota classifier: a tool result
    # that happens to say "timed out" in its text must not be mistaken for a
    # real provider timeout.
    assert classify_provider_error(RuntimeError("the download timed out")) is None


# --- 2. the fault a workflow author actually sees -----------------------


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _timeout_responder(_prompt):
    raise httpx.ReadTimeout("timed out", request=_request())


def _core(db, responder):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=4)


def test_leaf_read_timeout_produces_a_didactic_fault(db, monkeypatch):
    monkeypatch.delenv(READ_TIMEOUT_ENV_VAR, raising=False)
    spec = validate_spec(
        {"meta": {"name": "t"}, "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]}
    )
    core = _core(db, _timeout_responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs.get("a") is None
        assert len(result.faults) == 1
        fault = result.faults[0]
        assert fault == (
            "a: leaf error: provider read timeout after ~600s (silence, not size "
            "— leaves stream); LOHRA_PROVIDER_READ_TIMEOUT raises the HTTP limit; "
            f"the node `timeout:` field controls the leaf-level limit "
            f"(default {LEAF_TIMEOUT:.0f}s)"
        )
    finally:
        core.shutdown()


def test_leaf_read_timeout_fault_names_the_configured_read_window(db, monkeypatch):
    monkeypatch.setenv(READ_TIMEOUT_ENV_VAR, "90")
    spec = validate_spec(
        {"meta": {"name": "t"}, "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]}
    )
    core = _core(db, _timeout_responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert "~90s" in result.faults[0]
    finally:
        core.shutdown()
