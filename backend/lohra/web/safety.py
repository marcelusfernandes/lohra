"""URL safety — refuse non-public targets before any request (SSRF guard).

``validate_public_url`` resolves the host and rejects loopback, private,
link-local (incl. cloud metadata 169.254.169.254), reserved, and multicast
addresses, plus any non-http(s) scheme. It is a pure function over an injectable
resolver, so it is unit-testable with literal IPs and no real network.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable
from urllib.parse import urlparse

# (host, port) -> getaddrinfo-style list; injectable for tests.
Resolver = Callable[..., list]

_ALLOWED_SCHEMES = ("http", "https")


class WebError(ValueError):
    """A web request that is unsafe, malformed, or failed."""


def _resolved_ips(host: str, resolver: Resolver) -> list[str]:
    try:
        infos = resolver(host, None)
    except socket.gaierror as exc:
        raise WebError(f"could not resolve host {host!r}: {exc}") from exc
    return [info[4][0] for info in infos]


def _is_non_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    # An IPv4-mapped IPv6 (::ffff:127.0.0.1) is NOT is_loopback on Python <3.13;
    # classify the embedded IPv4 so a mapped internal target can't slip through.
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def validate_public_url(url: str, *, resolver: Resolver | None = None) -> None:
    """Raise ``WebError`` unless ``url`` is an http(s) URL to a public host."""
    resolver = resolver or socket.getaddrinfo
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise WebError(f"unsupported URL scheme: {parsed.scheme or '(none)'!r} (http/https only)")
    host = parsed.hostname
    if not host:
        raise WebError("URL has no host")
    ips = _resolved_ips(host, resolver)
    if not ips:
        raise WebError(f"could not resolve host {host!r}")
    for ip in ips:
        if _is_non_public(ip):
            raise WebError(f"refusing to fetch a non-public address: {ip} (host {host!r})")
