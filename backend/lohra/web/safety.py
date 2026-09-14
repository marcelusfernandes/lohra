"""URL safety — refuse non-public targets before any request (SSRF guard).

``validate_public_url`` resolves the host and rejects loopback, private,
link-local (incl. cloud metadata 169.254.169.254), reserved, and multicast
addresses and shared address space, plus any non-http(s) scheme. Its resolver
can be injected, so it is testable with literal IPs and no real network.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable

import httpx

# (host, port) -> getaddrinfo-style list; injectable for tests.
Resolver = Callable[..., list]

_ALLOWED_SCHEMES = ("http", "https")


class WebError(ValueError):
    """A web request that is unsafe, malformed, or failed."""


def resolve_public_addresses(host: str, *, resolver: Resolver | None = None) -> tuple[str, ...]:
    """Validate the ENTIRE answer; return ordered, deduplicated numeric addresses.

    No fallback resolution is permitted after returning this snapshot. The same
    classifier is used by preflight and the physical connection backend.
    """
    try:
        infos = (resolver or socket.getaddrinfo)(host, None)
        addresses = []
        for info in infos:
            raw = info[4][0]
            if not isinstance(raw, str) or "%" in raw:
                raise ValueError
            address = ipaddress.ip_address(raw)
            if _is_non_public(str(address)):
                raise WebError(f"refusing to fetch a non-public address: {address} (host {host!r})")
            addresses.append(str(address))
        if not addresses:
            raise ValueError
        return tuple(dict.fromkeys(addresses))
    except WebError:
        raise
    except (OSError, ValueError, TypeError, IndexError, KeyError):
        raise WebError(f"could not resolve a valid public address for host {host!r}") from None


def _is_non_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    # An IPv4-mapped IPv6 (::ffff:127.0.0.1) is NOT is_loopback on Python <3.13;
    # classify the embedded IPv4 so a mapped internal target can't slip through.
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    # Shared address space (100.64/10) is neither private nor globally reachable.
    return (
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def validate_public_url(url: str, *, resolver: Resolver | None = None) -> None:
    """Raise ``WebError`` unless ``url`` is an http(s) URL to a public host."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise WebError("malformed web URL") from None
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise WebError(f"unsupported URL scheme: {parsed.scheme or '(none)'!r} (http/https only)")
    host = parsed.raw_host.decode("ascii")
    if not host:
        raise WebError("URL has no host")
    resolve_public_addresses(host, resolver=resolver)
