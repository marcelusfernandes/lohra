"""Tests for the SSRF guard — public URLs pass, everything internal is refused."""

import pytest

from lohra.web.safety import WebError, validate_public_url


def _resolver_to(ip):
    """A fake getaddrinfo that maps any host to a fixed IP."""
    return lambda host, port: [(2, 1, 6, "", (ip, 0))]


def test_public_url_passes():
    validate_public_url("https://example.com/page", resolver=_resolver_to("93.184.216.34"))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com",  # no scheme
    ],
)
def test_bad_scheme_is_refused(url):
    with pytest.raises(WebError):
        validate_public_url(url, resolver=_resolver_to("93.184.216.34"))


def test_missing_host_is_refused():
    with pytest.raises(WebError):
        validate_public_url("http://", resolver=_resolver_to("93.184.216.34"))


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "169.254.169.254",  # cloud metadata (link-local)
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "172.16.0.1",  # private
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",  # IPv4-mapped private
    ],
)
def test_internal_addresses_are_refused(ip):
    with pytest.raises(WebError):
        validate_public_url("http://target.test/", resolver=_resolver_to(ip))


def test_unresolvable_host_is_refused():
    import socket

    def boom(host, port):
        raise socket.gaierror("nope")

    with pytest.raises(WebError):
        validate_public_url("http://nope.test/", resolver=boom)


def test_a_public_host_resolving_to_internal_is_refused():
    # DNS-rebinding-lite: a public-looking name that resolves to a private IP.
    with pytest.raises(WebError):
        validate_public_url("http://sneaky.example/", resolver=_resolver_to("10.1.2.3"))
