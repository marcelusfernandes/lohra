# Owned web fetch DNS pinning — issue #13

Base: public main `75547e9dd49fe6acb2974c5f66624b25618ea93e`.
Implementation worktree: `task-13`, branch `codex/task-13`.

## RED and discriminators

Before production changes, `test_web_pinned_fetch.py` ran the default owned
fetcher through HTTPX and HTTPCore, with instrumented synthetic DNS, sockets
and TLS. The first lookup returned a public address; a later hostname lookup
returned loopback, metadata, IPv6 loopback or mapped loopback. The private
address reached `socket.connect`. The same failure occurred after a redirect.
All five desired no-private-dial assertions failed on the base in Python 3.11
(**5 failed**). Positive wire controls were then added so denying everything
cannot satisfy the suite.

A second RED found shared address space accepted by the old classifier:
`100.64.0.1` passed on both preflight and the connection lookup (**2 failed,
28 passed** in `test_web_pinned_contracts.py`). `not is_global` now complements
the existing refusals. Final cases cover the range's upper end, mapped IPv4,
a mixed public/shared answer, and permitted `100.128.0.1`/`8.8.8.8` controls.
Python documents why `is_private` alone misses shared address space in its
[`ipaddress` reference](https://docs.python.org/3.13/library/ipaddress.html#ipaddress.IPv4Address.is_global).

The provenance regression initially failed twice: a real owned-fetch leaf's
stored hash lacked `egress_dns: "pinned_public"`. Its final test reopens SQLite
with the exact pre-#13 hash (already containing search and all-hops semantics),
preserves the entire cache row, spawns zero leaves, performs zero DNS/network
work, and records `policy_changed`. The NULL control remains unknown. A new
acquisition through the actual fetcher carries the new stamp. No old output is
claimed to have been re-fetched or pinned retroactively.

## Implemented slice

`web/safety.py` validates an entire DNS answer and returns normalized, ordered,
deduplicated public IPs. HTTPX's canonical ASCII hostname is used for DNS as
well as the logical URL, avoiding stdlib IDNA2003's `ß`/`ss` conflation.
`web/network.py` implements the public HTTPCore backend and stream interfaces:
only snapshot IPs reach the OS TCP backend, with one monotonic budget across
TCP fallback and TLS. No hostname fallback or request/read replay is possible.

`web/transport.py` adapts public HTTPX requests/responses to a public HTTPCore
ConnectionPool. Logical origins remain intact for pooling, Host/port, SNI and
certificate verification. A conflicting request SNI extension cannot replace
the URL identity; the caller's extension map remains unchanged. HTTPcore
exceptions retain their HTTPX categories through streaming and close.
`web/fetch.py` keeps manual redirects, body/type/status handling and ownership.

An IP-rewritten request with a request-owned ordinary transport was considered:
it pins addresses but does not by itself bound TCP plus TLS, since HTTPCore
can give both phases the full timeout. A shared pool keyed by rewritten IP was
also rejected because two hostnames can reuse the first TLS identity. The
public backend/stream approach addresses both without private APIs or global
DNS monkeypatching in production. The public extension points are documented
by [HTTPX](https://www.python-httpx.org/advanced/transports/) and
[HTTPCore](https://www.encode.io/httpcore/network-backends/).

`web/environment.py` explicitly rejects an effective proxy before hop DNS,
including a proxy introduced between hops. It uses the environment/system map
and HTTPX-compatible route priority/NO_PROXY matching, rather than the differing
stdlib bypass helper. Invalid configuration fails explicitly; credentials and
proxy URLs are not echoed. CA FILE > DIR > certifi default is handled separately,
with certificate and hostname verification enabled. Valid CA environment
configuration is retained even though the owned HTTPX client has `trust_env=False`.

HTTPCore is a direct dependency at `>=1.0.9,<2`: the
[1.0.9 changelog](https://github.com/encode/httpcore/blob/1.0.9/CHANGELOG.md)
records the h11 security update. No dependency upgrade was installed into the
shared runtimes; the minimum-HTTPX target only shadows HTTPX, using HTTPCore
1.0.9/h11 0.16.0 already present.

## Verification

Run from `task-13/backend`, with absolute worktree `PYTHONPATH`, the matching
runtime's `bin` first in PATH, and no bytecode/cacheprovider output:

```sh
python -m pytest tests/test_web*.py tests/test_workflow_fetch*.py tests/test_workflow_dns_stamp.py tests/test_workflow_cache_policy.py tests/test_workflow_search_policy.py tests/test_workflow_sandbox.py tests/test_workflow_sandbox_denials.py tests/test_sandbox_denial_metadata.py tests/test_workflow_taint.py tests/test_workflow_tools.py tests/test_agent_delegate_scope.py -q --no-cov -p no:cacheprovider
```

| Python | HTTPX | HTTPCore | Result |
| --- | --- | --- | --- |
| 3.11.15 | 0.28.1 | 1.0.9 | 437 passed, 9.99 s |
| 3.13.5 | 0.28.1 | 1.0.9 | 437 passed, 10.33 s |
| 3.11.15 | 0.27.0 | 1.0.9 | 437 passed, 9.84 s |
| 3.13.5 | 0.27.0 | 1.0.9 | 437 passed, 10.34 s |

Runtimes: `/tmp/lohra-wave10-py311/bin/python` and
`/tmp/lohra-wave10-py313/bin/python`. The minimum matrix prepends
`/tmp/lohra-13-httpx027` to the worktree's absolute PYTHONPATH. This verifies the
fetch/policy slice on that minimum, not every Lohra feature on all dependency
versions.

The integration discriminator runs operator JSON → WorkflowService → canonical
nested child → registry → tool → owned transport, with outside-host/tainted
controls, private second DNS refusal, and allowed redirects. Forged client,
resolver, transport and policy fields cannot alter that route. Existing #56
MockTransport tests now patch the transport construction boundary explicitly;
their off-list, IDNA and typed denial assertions remain intact.

TLS tests use an ephemeral local CA and actual loopback TLS/HTTP with a trusted
test-only mapping of the already validated public IP to the local server. They
verify Host/port/SNI, relative and external-host redirects, correct origin pool
reuse, mismatch/untrusted failures before HTTP, FILE/DIR trust and invalid CA
configuration. Python 3.13 initially rejected fixture certificates without an
Authority Key Identifier; the certificates were corrected, never verification.
LibreSSL lacks `rehash`, so the hashed-directory fixture uses `x509 -hash`.

Clock-controlled tests prove shared TCP/TLS budget, successful bounded fallback,
no attempt after exhaustion, refusal of a late backend success and unchanged
read/write timeouts. Wire/stream cases verify body caps, content type, HTTP
status behavior, truncated protocol errors, exception categories and cleanup,
including KeyboardInterrupt/SystemExit. A 34-case differential proxy oracle
runs against both supported HTTPX versions; private imports exist only in test
oracles and socket instrumentation.

A targeted coverage run of web, workflow-fetch, DNS-stamp and cache-policy tests
passed **289 tests** under Python 3.11: aggregate **97%** for `lohra.web` and
`cell_stamp`; new environment **100%**, network **93%**, transport **92%**.
Ruff across `backend/` and `git diff --check` pass. Builtin workflow-authoring
remains unchanged at **799 lines**. System prompt construction is untouched.

## Limits and pending integration gates

Blocking OS DNS is outside the connect/TLS deadline; read/write/pool budgets are
separate. Pinning classifies destination IPs, not downstream routing, NAT or OS
firewall policy. No general proxy tunnel or provider/search transport rewrite is
included. Effective proxy configurations that formerly worked now fail with an
explicit configuration remedy; compatibility is not claimed for those routes.
An arbitrary injected Python client controls its own transport/verification and
is outside the owned pinning guarantee, though existing hop preflight remains.
Tool JSON cannot supply that seam.

All DNS/socket endpoints were synthetic except the explicitly controlled local
TLS server. No external provider, public/private remote endpoint, personal
profile or personal database was used. Native Windows proxy sources were not
exercised; system-map behavior was injected. Full backend CI and independent
review of the final integrated SHA remain coordinator-owned; no publication or
merge was performed by the implementer.
