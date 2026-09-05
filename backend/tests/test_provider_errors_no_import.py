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

from lohra.providers.errors import (
    AUTH_FAILED,
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
