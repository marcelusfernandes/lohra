# Workflow fetch redirect allowlist — issue #56

Base: public main `84f62efcf77bc8f495f5d547c08f99a3a8f119db`.

## RED before implementation

The initial `test_workflow_fetch_egress.py` used a real operator JSON file,
`WorkflowService`, sandboxed `Agent`, `subagent_dispatch`, registry handler,
`web_fetch` and `fetch_url`. Only the model client, resolver and HTTP transport
were synthetic. With `egress_allow: ["api.test"]`, a 302 redirected to
`outside-canary.test`. Both recorded lists were
`["api.test", "outside-canary.test"]`: the off-list host was resolved and
connected. The desired assertion required only `["api.test"]` and failed
under Python 3.11 (**1 failed**). This was the real bypass, not a missing
parameter or mocked policy-helper failure.

A second RED covered replay compatibility. After SQLite reopen, the exact
pre-#56 fingerprint (already containing #55's `allow_search`) replayed without
an advisory under the same operator JSON. The new regression required one
`policy_changed` advisory and failed; its NULL-stamp control passed
(**1 failed, 1 passed**).

## Implemented contract

The sandbox creates a fresh `RestrictedFetchArgs` mapping. Its host tuple is
internal metadata outside the JSON keys; neither a forged `allowed_hosts` key
nor handler kwargs can widen or revoke it. The existing subagent dispatcher
and registry preserve this object. The tool recognizes its type and forwards
the trusted tuple to `fetch_url`. The caller's original mapping is unchanged.

The initial sandbox gate and fetcher's per-hop check share exact,
case-insensitive host matching. Subdomains require explicit entries; wildcard
and suffix matching are not supported. Initial and redirect destinations are
checked before the resolver and HTTP request. Relative/scheme-relative
redirects are resolved before the next check. Per-request
`follow_redirects=False` preserves manual checking even if an injected client
would otherwise auto-follow. Allowed hosts still pass the existing SSRF
guard. `allowed_hosts=None` preserves unrestricted public-web use outside
the sandbox; `()` denies all hosts. Taint still refuses before base dispatch.
Compatibility for the normal default client retains its body/error behavior.
An injected client with `follow_redirects=True` is a deliberate exception:
it previously bypassed manual hop checks, and now obeys them even without a
host policy. Identical behavior is not claimed for that unsafe configuration.

A typed `EgressDenied` becomes the existing trusted denial marker, with
`egress_redirect_not_allowed` for redirects and `egress_not_allowed` for the
initial fetch. Redirect error text names the host and hop, excluding path,
query and credentials. Metadata/advisories/audit only receive the closed
reason. The real leaf tests return `done` without refusal prose and still
produce the advisory, including with audit disabled and inside a nested
workflow. The successful node remains complete.

The canonical policy fingerprint includes fixed harness semantics
`egress_scope: "all_hops"`. This is not an operator setting. Known old stamps
replay with `policy_changed` even with the same JSON; the message does not
attribute an edit to the user. No cache is invalidated or recomputed, and NULL
still means unknown. Both paths are tested through a reopened SQLite file
with zero new leaves.

## Verification

From this worktree's `backend/`, with absolute
`PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-56/backend`
and the matching `/tmp/lohra-wave10-py311/bin` or `py313/bin` first in PATH:

```sh
python -m pytest tests/test_workflow_fetch_egress.py tests/test_web_fetch_egress.py tests/test_web_fetch.py tests/test_web_tool.py tests/test_web_safety.py tests/test_web_search.py tests/test_workflow_sandbox.py tests/test_workflow_sandbox_denials.py tests/test_sandbox_denial_metadata.py tests/test_workflow_cache_policy.py tests/test_workflow_search_policy.py tests/test_workflow_taint.py tests/test_workflow_tools.py tests/test_agent_delegate_scope.py tests/test_workflow_supervision_reading.py -q --no-cov
```

Python 3.11: **280 passed** (7.45 s). Python 3.13: **280 passed** (7.52 s).
The tests include all redirect codes, third-hop denial, relative redirects,
an injected auto-follow client, exact host matching, forged agent fields,
operator JSON's empty/blocked/allowed/tainted legs, nested leaves, audit off,
SSRF rejection for allowlisted internal hosts, and unchanged unsandboxed
fetch output. Existing search, delegation, taint and other sandbox gates pass.

A targeted coverage run of the two new test files plus `test_web_fetch.py`
and `test_web_tool.py` passed **62 tests**. Coverage: `web/egress.py` **96%**,
`web/fetch.py` **98%**, `web/tool.py` **96%**; aggregate **97%**.
Ruff across `backend/` and `git diff --check` pass. The builtin skill remains
**799 lines**, including the tested 800-line cap.

No real network, provider, operator configuration or personal database was
used. The frozen prompt and SSRF classifier are unchanged. This change does
not turn the leaf sandbox into an OS/network sandbox, and does not address
the separately scoped filesystem issues. Full backend CI, independent review
and integration remain coordinator-owned gates; no push/publication here.

## Independent-review correction: IDNA host identity

The independent review of `46861811264e4703538479f1dedd6204afa753ef`
identified a P2 false denial: `éxample.test` or `straße.test` passed the initial
Unicode host gate, but a relative `/final` redirect became ASCII IDNA in HTTPX
and no longer matched the Unicode policy. Re-running the independent probe
on that SHA gave **2 failed, 1 passed** (ASCII control passed). The new
`test_workflow_fetch_idna.py` independently recorded **17 failed, 23 passed**
before the correction, including cross-spelling initial refusals and unequal
fingerprints for equivalent grants.

The correction uses the public `httpx.URL(host=...).raw_host` API, inspected
in local HTTPX 0.28.1. Its host encoder uses `idna.encode(host.lower())` with
IDNA2008, preserving `straße.test → xn--strae-oqa.test` versus the distinct
`strasse.test`. No stdlib IDNA2003 codec or private HTTPX API is called.
Policy entries are passed as hosts, not parsed as complete URLs. Malformed
entries cannot turn userinfo, port, path or query into a grant; invalid IDNA
fails closed. Matching remains exact, with no subdomain/wildcard expansion.
IPv6 host grants, the SSRF classifier and resolver path are unchanged.

The fingerprint shares this canonical identity and deduplicates equivalent
Unicode/ASCII spellings; ASCII grants and the `all_hops` marker are stable.
Replay still follows the existing advisory-only contract.

After correction, the original focused command above **plus
`tests/test_workflow_fetch_idna.py`** passed **320 tests** on Python 3.11
(8.38 s) and **320 tests** on Python 3.13 (8.45 s). The 40 new cases exercise
operator JSON through the canonical child factory, registry and real fetcher,
relative and absolute redirects, Unicode/ASCII equivalence in both directions,
external refusal before DNS, `straße ≠ strasse`, malformed policy entries,
IPv6 and fingerprints. Only MockTransport, synthetic DNS and temporary SQLite
are used; socket connections are explicitly forbidden in the new tests.

The unmodified independent probe file
`/tmp/review56-probes/test_independent.py` now passes **27 tests** on both
Python versions (3.11: 0.50 s; 3.13: 0.54 s). Ruff across `backend/` and
`git diff --check` pass. This is a new commit after the published reviewed
head, with no rebase, amend, push or publication. Independent re-review of the
new SHA is still required; these passing probes are implementation evidence,
not an independent approval.
