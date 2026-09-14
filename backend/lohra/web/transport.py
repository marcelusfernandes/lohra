"""Fetch-owned HTTP/1 transport using only public HTTPX/HTTPCore interfaces.

Logical origins remain unchanged for pooling, Host, SNI and certificates. DNS
pinning is in the backend; there is no IP-keyed pool or post-connect SSRF check.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpcore
import httpx

from lohra.web.network import PublicNetworkBackend
from lohra.web.safety import Resolver

# Most-specific classes first; iteration and closing need the same translation
# as request establishment. No private HTTPX exception adapter is imported.
_ERRORS = tuple(
    (getattr(httpcore, name), getattr(httpx, name))
    for name in (
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectError",
        "ReadError",
        "WriteError",
        "ProxyError",
        "LocalProtocolError",
        "RemoteProtocolError",
        "UnsupportedProtocol",
        "TimeoutException",
        "NetworkError",
        "ProtocolError",
    )
)


@contextmanager
def map_errors():
    try:
        yield
    except Exception as exc:
        for source, target in _ERRORS:
            if isinstance(exc, source):
                raise target(str(exc)) from exc
        raise


class ResponseStream(httpx.SyncByteStream):
    def __init__(self, response: httpcore.Response):
        self._response = response

    def __iter__(self):
        with map_errors():
            yield from self._response.iter_stream()

    def close(self):
        with map_errors():
            self._response.close()


class PublicTransport(httpx.BaseTransport):
    def __init__(self, *, ssl_context, resolver: Resolver | None = None):
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            network_backend=PublicNetworkBackend(resolver),
            http1=True,
            http2=False,
            retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.raw_host.decode("ascii")},
        )
        with map_errors():
            response = self._pool.handle_request(core_request)
        try:
            return httpx.Response(
                response.status,
                headers=response.headers,
                stream=ResponseStream(response),
                extensions=response.extensions,
            )
        except BaseException:
            response.close()
            raise

    def close(self):
        with map_errors():
            self._pool.close()
