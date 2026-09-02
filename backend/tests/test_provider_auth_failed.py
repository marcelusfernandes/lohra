"""Credential/permission rejection is its own failure kind (#43 Q3).

A 401/403 is not "the leaf had bad luck". The client is built once per route and
cached for the life of the pool, so the credential that was refused is the exact
credential every later attempt on that route will present: within one run the
rejection is DETERMINISTIC. E1's same-route re-spawn would therefore buy the
identical refusal again, at full price, and the remedy is not a retry at all —
it belongs to the operator (a key, a scope, an enabled subscription).

So it gets classified STRUCTURALLY, the same discipline as quota and timeout: an
SDK class or an HTTP status, never a regex over the message. Two consequences,
both tested here:
- the leaf fault names the credential instead of quoting an opaque SDK string;
- ``auth_failed`` joins ``NO_RESPAWN_KINDS``, so ``retries`` never touches it.

Deliberately NOT a pause reason: a pause is a promise the run will come back on
its own, and nothing about a refused credential fixes itself with time.
"""

import anthropic
import httpx
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.providers.errors import (
    AUTH_FAILED,
    QUOTA_EXHAUSTED,
    TIMEOUT,
    classify_provider_error,
)
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.leaf_retry import NO_RESPAWN_KINDS
from lohra.workflow.schema import validate_spec
from tests.test_workflow_pipeline import ScriptedClient


def _http_error(cls, status):
    request = httpx.Request("POST", "https://api.example.test/v1/messages")
    response = httpx.Response(status, request=request)
    return cls("refused", response=response, body=None)


class _DuckError(Exception):
    def __init__(self, message, *, status_code=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


# --- 1. classification -------------------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
    ],
)
def test_every_sdk_auth_class_is_auth_failed(cls):
    status = 403 if "Permission" in cls.__name__ else 401
    assert classify_provider_error(_http_error(cls, status)) == AUTH_FAILED


@pytest.mark.parametrize("status", [401, 403])
def test_duck_typed_401_and_403_are_auth_failed(status):
    assert classify_provider_error(_DuckError("nope", status_code=status)) == AUTH_FAILED


def test_prose_mentioning_credentials_is_not_classified():
    # Structural only, same rule as quota and timeout: a tool result quoting
    # "401 invalid api key" back at us must not be read as our own rejection.
    assert classify_provider_error(RuntimeError("the API returned 401 unauthorized")) is None


def test_quota_still_wins_over_auth_for_a_429():
    assert classify_provider_error(_DuckError("slow down", status_code=429)) == QUOTA_EXHAUSTED


def test_a_timeout_is_still_a_timeout():
    request = httpx.Request("POST", "https://api.example.test/v1/messages")
    assert classify_provider_error(httpx.ReadTimeout("silent", request=request)) == TIMEOUT


def test_auth_failed_is_refused_a_respawn():
    assert AUTH_FAILED in NO_RESPAWN_KINDS


# --- 2. what the run does with it -------------------------------------------


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

    return OrchestrationCore(db, factory, max_concurrent=4)


def test_a_refused_credential_is_never_respawned_and_says_whose_problem_it_is(db):
    calls: list[str] = []

    def responder(prompt):
        calls.append(prompt)
        raise _DuckError("invalid x-api-key", status_code=401)

    spec = validate_spec(
        {
            "meta": {"name": "auth"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "go", "retries": 3}],
        }
    )
    core = _core(db, responder)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert len(calls) == 1  # ``retries: 3`` buys nothing here
        assert result.outputs["a"] is None
        assert result.status != "paused"  # nothing about this fixes itself with time
        assert len(result.faults) == 1
        fault = result.faults[0]
        assert fault.startswith("a: leaf error: provider refused this route's credential")
        assert "invalid x-api-key" in fault  # the provider's own words, kept
        assert "operator" in fault  # ...and whose problem it is
    finally:
        core.shutdown()
