"""``classify_provider_error`` must not import anthropic/openai to classify
(issue #80/H10).

The old implementation called ``__import__("anthropic")``/``__import__("openai")``
unconditionally on EVERY classification, before ever looking at the exception —
paying the SDK's full import cost (~0.3s cold, measured directly for this
issue) on the first exception a process ever classified, even an ordinary
``RuntimeError`` with nothing to do with either SDK. A pipeline's barrier
timeout could then race that import: the leaf's death got classified AFTER the
barrier had already given up on it, and the death fault silently dropped out
of the run's accounting (reproduced with a 0.3s ``PIPELINE_TIMEOUT`` in
``tests/test_workflow_pipeline_hardening.py``'s mixed-faults test).

Two properties pinned here:
- the FIRST classification in a brand-new process costs microseconds, never
  SDK import time, and imports neither SDK as a side effect;
- a duck-typed exception that merely LOOKS like an SDK exception (same
  ``__module__``/class name, none of the real inheritance) classifies exactly
  like the real SDK instance — the classifier matches structurally
  (``__module__``, ``__name__``), never by ``isinstance`` against an imported
  class.
"""

from __future__ import annotations

import subprocess
import sys

import anthropic
import httpx
import openai
import pytest

from lohra.providers.errors import (
    AUTH_FAILED,
    MODEL_NOT_FOUND,
    QUOTA_EXHAUSTED,
    TIMEOUT,
    classify_provider_error,
)

_PROBE = """
import sys, time
from lohra.providers.errors import classify_provider_error
t0 = time.perf_counter()
classify_provider_error(RuntimeError("x"))
elapsed_ms = (time.perf_counter() - t0) * 1000
print(elapsed_ms)
print("anthropic" in sys.modules)
print("openai" in sys.modules)
"""


def test_first_classification_in_a_fresh_process_costs_no_sdk_import():
    """A brand-new interpreter that never touched anthropic/openai classifies
    an ordinary exception in well under the SDK's own import time (~0.3s
    measured cold on this machine), and imports neither SDK to do it. 50ms is
    a generous bar — the measured cost is ~0.01ms — chosen to stay robust
    against a slow/loaded CI runner without hiding a regression back to the
    ~300ms+ the lazy ``__import__`` used to cost."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    elapsed_ms_line, anthropic_imported, openai_imported = result.stdout.splitlines()
    elapsed_ms = float(elapsed_ms_line)
    assert elapsed_ms < 50, f"first classification took {elapsed_ms:.3f}ms (want < 50ms)"
    assert anthropic_imported == "False", "classify_provider_error must not import anthropic"
    assert openai_imported == "False", "classify_provider_error must not import openai"


def _fake(module: str, name: str) -> Exception:
    """A duck-typed exception with NONE of the real SDK's inheritance — only
    the ``(__module__, __name__)`` shape the classifier matches on."""
    cls = type(name, (Exception,), {"__module__": module})
    return cls("duck-typed")


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.example.test/v1/messages")


def test_fake_rate_limit_error_classifies_like_the_real_sdk_class():
    real = anthropic.RateLimitError(
        "rate limited", response=httpx.Response(429, request=_request()), body=None
    )
    fake = _fake("anthropic", "RateLimitError")  # no status_code at all — pure duck-type
    assert classify_provider_error(real) == QUOTA_EXHAUSTED
    assert classify_provider_error(fake) == QUOTA_EXHAUSTED


def test_fake_auth_error_classifies_like_the_real_sdk_class():
    real = openai.AuthenticationError(
        "nope", response=httpx.Response(401, request=_request()), body=None
    )
    fake = _fake("openai", "AuthenticationError")
    assert classify_provider_error(real) == AUTH_FAILED
    assert classify_provider_error(fake) == AUTH_FAILED


def test_fake_timeout_error_classifies_like_the_real_sdk_class():
    real = anthropic.APITimeoutError(request=_request())
    fake = _fake("anthropic", "APITimeoutError")  # no status_code, same as the real one
    assert classify_provider_error(real) == TIMEOUT
    assert classify_provider_error(fake) == TIMEOUT


def test_the_sdk_module_alone_is_not_enough_without_the_right_class_name():
    # Guards the new structural match against being too loose: sharing the
    # SDK's module string on some OTHER anthropic-namespaced exception must
    # not be read as one of the three classified kinds.
    fake = _fake("anthropic", "SomeUnrelatedError")
    assert classify_provider_error(fake) is None


def test_the_right_class_name_alone_is_not_enough_from_an_unrelated_module():
    # And the converse: the class name alone, from neither SDK's module, must
    # not be enough — otherwise any library's own RateLimitError would
    # incorrectly pause a perfectly healthy run.
    fake = _fake("some_other_library", "RateLimitError")
    assert classify_provider_error(fake) is None


# --- SDK-CONSTRUCTED twins: what the SDK really hands us, per category --------
#
# Dogfood T17 (2026-09-05) shipped a classification that passed every unit test
# and was dead in production, because the tests hand-built ``.body`` as the RAW
# HTTP body while the openai SDK UNWRAPS the ``error`` layer before the
# exception ever reaches us (``_make_status_error``: ``data = body.get("error",
# body)``). Anthropic does NOT unwrap. Two different shapes, and a hand-built
# fixture can silently pick the wrong one.
#
# So: every category is pinned against an exception built by the SDK's OWN
# constructor from a RAW body, the same call path a live 4xx takes. These are
# the tests a hand-built fixture cannot fake.

_RAW_BODIES = {
    # (label, client, url, status, raw HTTP body, expected kind)
    "anthropic-429": ("anthropic", 429,
                      {"type": "error", "error": {"type": "rate_limit_error",
                                                  "message": "slow down"}},
                      QUOTA_EXHAUSTED),
    "openai-429": ("openai", 429,
                   {"error": {"message": "slow down", "type": "rate_limit_error",
                              "code": "rate_limit_exceeded"}},
                   QUOTA_EXHAUSTED),
    "anthropic-401": ("anthropic", 401,
                      {"type": "error", "error": {"type": "authentication_error",
                                                  "message": "bad key"}},
                      AUTH_FAILED),
    "openai-401": ("openai", 401,
                   {"error": {"message": "bad key", "type": "invalid_request_error",
                              "code": "invalid_api_key"}},
                   AUTH_FAILED),
    "anthropic-404-model": ("anthropic", 404,
                            {"type": "error", "error": {"type": "not_found_error",
                                                        "message": "model: bogus"}},
                            MODEL_NOT_FOUND),
    "openai-404-model": ("openai", 404,
                         {"error": {"message": "The model `x` does not exist",
                                    "type": "invalid_request_error", "param": None,
                                    "code": "model_not_found"}},
                         MODEL_NOT_FOUND),
    # The body OpenRouter really returns, captured live by dogfood T17 —
    # scratchpad/w9/dogfood/T17b-a-stderr.txt. No structural code at all: the
    # gateway echoes the HTTP status back as ``code``.
    "openrouter-400-model": ("openai", 400,
                             {"error": {"message": "nonexistent-vendor/e8b-xyz is not a "
                                                   "valid model ID", "code": 400},
                              "user_id": "user_2vUa21XXyB8B3uLDkFAsGmMOwVh"},
                             MODEL_NOT_FOUND),
}


def _sdk_error(sdk: str, status: int, raw_body: dict) -> Exception:
    """The exception the SDK ITSELF builds from a raw HTTP response.

    Not a hand-built duck and not a hand-built ``body=`` either: this goes
    through ``_make_status_error_from_response``, the same private path the
    real client takes on a 4xx, so whatever unwrapping the SDK does to the
    payload has already happened by the time the classifier sees it."""
    client = (
        anthropic.Anthropic(api_key="test-key")
        if sdk == "anthropic"
        else openai.OpenAI(api_key="test-key")
    )
    response = httpx.Response(
        status, request=_request(), json=raw_body,
        headers={"content-type": "application/json"},
    )
    return client._make_status_error_from_response(response)


@pytest.mark.parametrize("label", sorted(_RAW_BODIES))
def test_the_classifier_reads_what_the_SDK_actually_hands_it(label):
    sdk, status, raw_body, expected = _RAW_BODIES[label]
    exc = _sdk_error(sdk, status, raw_body)
    assert classify_provider_error(exc) == expected


def test_the_two_SDKs_really_do_disagree_about_unwrapping():
    """The fact that makes the twins above necessary, pinned so nobody has to
    re-derive it: openai strips the ``error`` layer off ``.body``, anthropic
    keeps it. Any payload accessor must tolerate BOTH."""
    openai_exc = _sdk_error("openai", 400, {"error": {"message": "m", "code": 400}})
    anthropic_exc = _sdk_error(
        "anthropic", 404, {"type": "error", "error": {"type": "not_found_error"}}
    )
    assert "error" not in openai_exc.body  # unwrapped by the SDK
    assert openai_exc.body == {"message": "m", "code": 400}
    assert "error" in anthropic_exc.body  # NOT unwrapped
