# Subscription credentials per request

Current runtime contract, issue #9. Subscription remains an explicit, per-profile
opt-in; this contract does not authorize an API-key fallback or change the frozen
system prompt. The public API-key clients retain their existing retry behavior.

## Resolution and lifetime

`build_subscription_client` binds the canonical Lohra home and the Codex auth
source directory at construction. A subsequent environment/cwd/profile change
does not change these identities. The final auth file still uses the bounded
reader's refusal of symlinks and nonregular files.

Construction validates credentials early. Every later `create` or `stream`
resolves again before opening its model request, including borrowed and cached
pool clients. It rereads the opt-in, acknowledgement and route preference; disabled,
unacknowledged or `api_key` preference refuses this subscription client locally.
Switching to an API-key session requires the operator to start that route.

Source precedence is unchanged:

1. A valid own `oauth.json` login can refresh. Refresh begins within the existing
   five-minute expiry window, under the profile transaction below. A response
   that omits account/refresh token preserves the prior values, as before.
2. When there is no own login, reread the bound Codex `auth.json`. Lohra never
   refreshes that family or writes that file. An expired/unreadable Codex login
   asks the operator to refresh through Codex or run `lohra auth login`.

A present but malformed/unreadable own store refuses locally; it cannot select a
different Codex account silently. Stored and refreshed expiries must be finite;
access/account fields must be valid HTTP header values. A refreshed family must
still be outside the expiry window after HTTP and after persistence, using the
current clock. The optional `now=` seam applies only to one explicit resolution;
the constructor's override never freezes later requests.

Each request obtains one immutable token/account snapshot. Account `None` in a
new store snapshot removes the account header. Request and constructor header
variants of Authorization, ChatGPT-Account-ID and originator cannot override the
dynamic snapshot. Other headers and the caller's dictionaries survive unchanged.
Neither SDK `api_key` nor shared default headers change while calls are running.

## One transaction per profile

Resolution, own refresh, login commit, logout, and config read/modify/write share
one native exclusive lock on a stable `.auth.lock` inode. Each operation opens a
fresh descriptor; native locks arbitrate threads and processes without a global
Python lock registry. Canonical directory aliases share it. Different profiles
remain independent. POSIX uses `flock`; Windows uses a byte-range lock.

Waiters reread config, token family and current time after acquiring the lock.
The lock is held through own refresh HTTP and persistence, then released before
model HTTP/stream iteration. Device login's human interaction/polling is outside
the transaction; only its commit is serialized. Public mutations acquire once;
internal `_..._locked` writers require an already-held transaction. No nested
acquisition, lease expiry, lock-file unlink, or speculative losing refresh.

Acquisition times out after 35 seconds; OAuth HTTP retains its 30-second timeout.
Timeout/error is explicit and token-free. Process exit releases the OS lock.
Auth files use unique sibling temporary files created at 0600, full buffered
write + flush + fsync, then atomic replacement. Cleanup touches only that writer's
temporary file. Refresh returns a snapshot only after its rotated family commits.
Config writes preserve unrelated JSON fields. Logout removes only the own token;
existing Codex reuse and opt-in are unchanged. Disable is the separate opt-out.

## Refusal, retries and limits

`SubscriptionError` is a lightweight typed `auth_failed` failure, not prose
classification or quota. The harness's existing route-fault policy handles it;
authored leaf `retries` cannot respawn that refusal. An HTTP 401/403 is sanitized
and asks for operator recovery. A genuine SDK 429 retains quota classification.
Refresh/parse/lock/store failures expose no response body, token or exception chain.

Dynamic subscription clients explicitly set SDK `max_retries=0`: no model request
is automatically replayed by the SDK after a transport/status failure that may
have started generation. This does not redefine the harness's independently
operator-authorized retry/routing policies for other failure kinds.

An already-open stream retains its original snapshot. Later expiry, logout or
disable cannot revoke an authorized request or rewrite its headers. The next
logical request rechecks the store/gates. No replay or stream restart is inferred.

A crash after remote rotation but before local commit can lose the rotated
family. Atomic replacement is not a distributed transaction, and a disk/power
failure is not covered by a journal. A subsequent auth failure requires explicit
`lohra auth login`; no expired-token or API-key fallback is attempted. Native
locking assumes a filesystem with working local advisory locks and cooperating
Lohra writers; older versions/external editors need not honor the lock. Windows
locking is implemented but not natively validated by this change; 0600 does not
provide a Windows ACL guarantee.

## SDK compatibility and evidence

The package's existing dependency floor also permits SDKs predating Responses.
The dynamic subscription seam gives a local installation remedy when the SDK has
no Responses surface (`openai>=1.66.0`); ordinary clients are not rejected by this
check. No dependency-floor migration accompanies this fix.

Hermetic actual-SDK tests cover the request seam with 1.66.0 (HTTPX) and 3.13.0
(HTTPX2), on Python 3.11 and 3.13. This is not validation of every Lohra feature or
every intervening SDK. OAuth uses ordinary HTTPX separately. See the
[implementation report](../history/reviews/2026-09-14-subscription-requests.md).
