# Public web fetch transport — #13

`web_fetch` uses `fetch_url` with an owned, verified HTTP/1 client. The workflow
host gate (#56) applies to the logical initial URL and each resolved redirect
before DNS or connection. `allowed_hosts=None` has no host restriction; `()`
denies every host. Exact HTTPX IDNA2008 matching, taint, and the separate search
opt-in are unchanged. A host grant never grants its resolved IP as another host.

## Address snapshot and connection identity

Every hop retains preflight validation. Every new physical connection resolves
again and validates the **entire** answer before dialing: malformed/empty sets,
private, loopback, link-local, reserved, multicast, unspecified and non-global
addresses are rejected. IPv4-mapped IPv6 is classified by its embedded IPv4;
shared address space (`100.64.0.0/10`) is also refused. DNS uses the same ASCII
IDNA hostname as HTTPX, preserving `straße` versus `strasse`. Scoped IPv6 is
refused. Accepted numeric addresses are normalized and deduplicated in order.

The backend dials only numeric addresses from this snapshot. The OS may process
a numeric address through `getaddrinfo`; it never receives the unvalidated
logical hostname for a second, unchecked connection lookup. A mixed answer is
refused before its first TCP attempt. There is no fallback to the hostname,
an ordinary transport, a proxy, or a model/provider request.

The logical request URL remains intact. Host includes its original non-default
port; TLS uses and verifies its ASCII hostname, even if a caller supplies a
conflicting SNI extension. The extension dictionary is copied. Pooling remains
by logical origin: two hostnames on one public IP do not share TLS identity.
A reusable connection has already been pinned; a new physical connection gets
a new validated snapshot.

Implementation uses public `httpx.BaseTransport`/`SyncByteStream` and
`httpcore.ConnectionPool`, `NetworkBackend`, `NetworkStream`, `SyncBackend`
interfaces. See [HTTPX transports](https://www.python-httpx.org/advanced/transports/)
and [HTTPCore network backends](https://www.encode.io/httpcore/network-backends/).
Production neither imports nor mutates private HTTPX/HTTPCore APIs. HTTPCore is a
direct dependency (`>=1.0.9,<2`), alongside the existing HTTPX minimum 0.27.0.

## Budgets, streaming and ownership

Numeric TCP failures may try the next address in the same validated snapshot.
One monotonic connect deadline covers all TCP attempts **and TLS**. TLS receives
only the remaining budget; even an over-budget backend success is closed and
refused. Blocking OS DNS is outside this deadline. Read/write/pool timeouts keep
their own HTTPX meanings; this is not a whole-fetch wall-clock deadline.

There is no TLS, HTTP or read-error replay. Redirects remain manual; each gets
its own checks. Body caps, textual content checks, existing HTTP status/body
behavior and error envelopes remain. HTTPCore exceptions are translated to
HTTPX exceptions during establishment, body iteration and close. Responses and
the owned pool close on success, redirect, refusal, cap, protocol/timeout error
and `BaseException`; an already-failed TLS stream is closed as well.

## Environment and trusted injection

The owned transport cannot control a proxy's remote DNS. It evaluates the
current environment/system proxy map before each hop's DNS, following HTTPX
route priority and `NO_PROXY` behavior. An effective proxy is an explicit
`WebError` with an actionable configuration remedy; it is never silently
bypassed. Invalid proxy/NO_PROXY configuration is refused. Proxy URLs and
credentials are excluded from these errors. System proxy sources come through
`urllib.request.getproxies`, including environment precedence and CGI rules.

Differential tests compare supported HTTPX 0.27/0.28 routing for schemes, ports,
case, label boundaries, leading dots, IPv4/IPv6, IDNA and precedence. Compatibility
includes HTTPX quirks: bare `localhost` is exact; a leading dot excludes the base
domain; Unicode NO_PROXY patterns rejected by HTTPX are not reinterpreted as a
direct grant. This is not proxy support or a replacement for an OS firewall.

CA configuration is handled separately: valid `SSL_CERT_FILE` takes precedence
over `SSL_CERT_DIR`, then the certifi-backed default. Verification and hostname
checks remain enabled. Invalid CA paths produce a configuration error without
the path. [HTTPX environment variables](https://www.python-httpx.org/environment_variables/)
document the proxy and CA sources. Tests use an ephemeral CA and local TLS only.

`client=` and `resolver=` are trusted Python injection seams, never accepted
from tool JSON or handler kwargs. An arbitrary injected client retains logical
hop/SSRF preflight and manual redirects but owns its connection, proxy and TLS
behavior; physical pinning cannot be promised for that seam. Its caller closes
it. The default tool/registry/workflow path uses the owned transport. No changes
are made to search backends, provider transports, the frozen prompt or OS routing.

## Replay provenance

The policy fingerprint includes internal `egress_dns: "pinned_public"` as an
effective harness semantic, alongside `egress_scope: "all_hops"`. It is not an
operator option. A known earlier stamp can differ with identical policy JSON:
replay adds `policy_changed` advisory without invalidating, rewriting or
recomputing the cell. The message names operator settings **or harness
semantics**, not an alleged operator edit. NULL remains unknown. A historical
output is not retrospectively claimed to have been fetched through pinning.
