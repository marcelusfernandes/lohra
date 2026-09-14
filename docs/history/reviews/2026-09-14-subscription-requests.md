# Issue #9 — subscription snapshots and coordinated refresh

Author implementation report, 2026-09-14. Baseline:
`b3e36847cca96ab8b3f3733fdf7d142f9a6f4c49`, branch `codex/task-9`.
This is implementation evidence, not the independent review or CI verdict.
All auth files, tokens, JWTs, SSE, HTTP responses and databases below are synthetic.
No provider, personal credential/store, or real external token family was used.

## Finding and alternative

**Confirmed:** a long-lived Responses client froze its initial bearer/account.
Falsification condition: the same instance must send a fresh coherent pair after
expiry or an external Codex update, without reconstruction. Actual SDK requests
captured by MockTransport disproved that on the baseline.

Rebuilding clients can adopt a new token, but moves refresh/ownership races into
the shared pool and borrowed client lifecycle; mutating a shared SDK key/header
cannot preserve concurrent snapshots. Request-local `extra_headers` provides the
needed public seam with no per-request client replacement. The callback and
store transaction ship together: adding only a callback would increase the
existing refresh race. `with_options` is another possible seam, but is unnecessary
for these request-local fields; this change keeps the existing client owner.

## RED to GREEN

The first request tests ran against unchanged production source:

- **9 failed, 1 passed** on Python 3.11: unchanged bearer in create/stream,
  account removal and caller header precedence, live opt-in/preference changes,
  refresh failure detection, and dynamic SDK retry count. The static client
  control already passed: default retries=2 means three HTTP attempts.
- Own-store tests exposed duplicate thread refresh and the shared temporary-file
  rename failure, plus acceptance of NaN expiry (**2 failed, 1 passed**). The
  initial process control could pass without overlap; it was strengthened to
  observe actual native lock contention before releasing the first refresh.
- Typed local SubscriptionError was `None` in the real error classifier and
  eligible for leaf retry (**1 failed**), then became `auth_failed`.
- Coordinator counterexamples were reproduced (**3 failed**): conflicting SDK
  constructor defaults and time advancing during refresh/persistence. Dynamic
  defaults are now filtered; post-I/O expiry uses the current clock.
- Further parser/header counterexamples were reproduced before their fixes:
  numeric overflow, illegal surrounding header whitespace, and deeply nested
  auth JSON. These now refuse locally without exposing credentials or entering
  the generic retry path.

The former mocked "losing refresh adopts another writer" test was replaced
explicitly: non-cooperating writer state cannot turn a failed refresh into
success. Actual thread/process tests cover the coordinated winner instead.
The constructor wiring test now verifies that account identity is supplied by
the request callback, not frozen in SDK defaults.

## Final behavior and discriminators

- Same client crosses own expiry, persists the rotated family, and adopts Codex
  updates without refresh/write. Account omission during OAuth refresh retains
  the existing account contract; account `None` in a new stored snapshot removes
  the request header.
- Concurrent create/stream calls carry coherent independent snapshots, preserving
  caller dictionaries and SDK key/default state. Model HTTP overlaps while auth
  changes proceed. An open SSE stream retains its original pair; its successor
  reads the new snapshot.
- Borrowed and owned pool clients retain identity/close ownership. A constructed
  client keeps its profile/Codex source despite cwd/environment changes and
  rechecks current human gates before each request.
- Threads and independently spawned processes observe the same native lock,
  reread the persisted winner, and perform one refresh. Other profiles progress
  during a blocked refresh. Canonical directory aliases contend on the same lock.
- Login commits, logout and config/preference changes use the same transaction;
  a delayed refresh cannot overwrite a newer serialized login or resurrect a
  completed logout. Config merges retain unknown fields.
- Unique 0600 temporary files are fully written, flushed/fsynced and replaced.
  Failed fsync/replace preserves the old store, cleans the writer's temp and
  sends no model request. Expiry overflow/nonfinite values, malformed/deep JSON,
  invalid header values, symlinked auth files and oversized family commits refuse.
- Native locks release after process death and KeyboardInterrupt; contention and
  OS failures are bounded/token-free. No lease, lock registry or journal was added.
- Real WorkflowService → Agent → subscription callback refusal pauses as
  `route_fault`; authored `retries: 3` produces one leaf and zero model HTTP calls.
  Genuine HTTP 429 remains quota. HTTP 401/403 and local auth errors are sanitized.
- Actual SDK DEBUG logging does not expose the bearer canary in the tested paths.
  Dynamic subscription SDK failures make one HTTP attempt; static Responses keeps
  three. Neither subscription refresh nor the SDK automatically replays auth.

## Validation

Each runtime used the absolute `task-9/backend` PYTHONPATH, with no editable-install
source ambiguity. Python 3.11.15 / 3.13 supplied runtimes, SDK 3.13.0:

- **393 passed per runtime**: subscription/config/request/persistence/pool/error
  suites, route-fault behavior, client/aux/Responses transport, timeout and stream
  abort suites.
- **15 passed per runtime**: existing onboarding login/opt-in CLI contracts.
- **25 passed per runtime with SDK 1.66.0**: actual request and lifecycle suites
  under the isolated `/tmp/lohra-9-sdk166` dependency target.
- Ruff and `git diff --check` pass. Full-suite CI and fresh isolated review remain
  coordinator steps; no live-provider or native-Windows claim is made here.

Reproduction (from `backend`, substitute the runtime and optional SDK prefix):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" /tmp/lohra-wave10-py311/bin/python -m pytest \
  tests/test_subscription*.py tests/test_auth_preference*.py tests/test_client_pool.py \
  tests/test_provider_auth_failed.py tests/test_provider_errors_no_import.py \
  tests/test_workflow_route_fault_pause.py tests/test_client.py tests/test_aux_client.py \
  tests/test_provider_timeouts.py tests/test_stream_abort.py tests/test_responses_transport.py \
  tests/test_onboarding_login.py -q -p no:cacheprovider --no-cov
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="/tmp/lohra-9-sdk166:$PWD" /tmp/lohra-wave10-py311/bin/python -m pytest \
  tests/test_subscription_requests.py tests/test_subscription_request_lifecycle.py \
  -q -p no:cacheprovider --no-cov
python -m ruff check .
git diff --check
```

## SDK evidence and limits

Installed SDK 3.13.0 uses HTTPX2; the OAuth poster separately uses ordinary HTTPX.
SDK 1.66.0 uses HTTPX. Official sources show Responses absent in
[v1.65.5](https://github.com/openai/openai-python/blob/v1.65.5/src/openai/_client.py)
and present with request headers in
[v1.66.0](https://github.com/openai/openai-python/blob/v1.66.0/src/openai/resources/responses/responses.py).
The [current official API reference](https://developers.openai.com/api/reference/python)
documents the public request-options/retry surface, not subscription endpoint
stability or OAuth permission. The lower-version tests validate this seam only,
not all Lohra features or every SDK version. Unsupported pre-Responses SDKs get
a subscription-specific installation remedy; the global floor is unchanged.

A stream already opened cannot be revoked by a later auth-file change. A crash
between remote token rotation and local commit can still require re-login; there
is no distributed atomicity, quarantine or model replay. Locking assumes local
filesystem support and cooperating writers. Windows locking is implemented but
not natively exercised here, and 0600 is not a Windows ACL guarantee. Existing
explicit harness retry/routing policy for other failure kinds remains separate.
The normative contract is [spec 09](../../specs/09-subscription-auth.md).
