"""Differential proxy-route contract; private imports belong ONLY to this oracle."""

import os
import urllib.request

import httpx
import httpx._client as client_module
import httpx._utils as utils_module
import pytest

from lohra.web.environment import require_direct
from lohra.web.safety import WebError


class RouteProbe(httpx.BaseTransport):
    def __init__(self, *args, proxy=None, **kwargs):
        self.route = None if proxy is None else proxy.url.host

    def handle_request(self, request):
        return httpx.Response(200, json={"route": self.route})


cases = [
    ("no_proxy_configuration", {}, "https://example.test"),
    ("http_route", {"HTTP_PROXY": "http://upper.test"}, "http://example.test"),
    ("http_does_not_proxy_https", {"HTTP_PROXY": "http://upper.test"}, "https://example.test"),
    ("https_route", {"HTTPS_PROXY": "http://upper.test"}, "https://example.test"),
    ("all_route", {"ALL_PROXY": "http://all.test"}, "https://example.test"),
    (
        "scheme_over_all",
        {"ALL_PROXY": "http://all.test", "HTTPS_PROXY": "http://scheme.test"},
        "https://example.test",
    ),
    (
        "lowercase_wins",
        {"HTTPS_PROXY": "http://upper.test", "https_proxy": "http://lower.test"},
        "https://example.test",
    ),
    (
        "empty_lowercase_disables",
        {"HTTPS_PROXY": "http://upper.test", "https_proxy": ""},
        "https://example.test",
    ),
    (
        "cgi_upper_http_ignored",
        {"HTTP_PROXY": "http://upper.test", "REQUEST_METHOD": "GET"},
        "http://example.test",
    ),
    (
        "cgi_lower_http_retained",
        {"http_proxy": "http://lower.test", "REQUEST_METHOD": "GET"},
        "http://example.test",
    ),
]
for name, bypass, urls in [
    ("wildcard", "*", ["https://example.test", "http://other.test"]),
    (
        "base",
        "example.test",
        ["https://example.test", "https://sub.example.test", "https://otherexample.test"],
    ),
    ("leading_dot", ".example.test", ["https://example.test", "https://sub.example.test"]),
    (
        "port",
        "example.test:8443",
        ["https://example.test:8443", "https://sub.example.test:8443", "https://example.test"],
    ),
    ("ipv4", "93.184.216.34", ["http://93.184.216.34", "http://93.184.216.35"]),
    (
        "ipv6",
        "2606:4700:4700::1111",
        ["https://[2606:4700:4700::1111]", "https://[2606:4700:4700::1001]"],
    ),
    ("localhost", "localhost", ["http://localhost", "http://sub.localhost"]),
    ("scheme_qualified", "https://example.test", ["https://example.test", "http://example.test"]),
    ("uppercase_no_proxy", "EXAMPLE.TEST", ["https://example.test", "https://sub.example.test"]),
    ("idna", "faß.de", ["https://faß.de", "https://fass.de"]),
    ("idna_ascii", "xn--fa-hia.de", ["https://faß.de", "https://fass.de"]),
]:
    cases.extend(
        (f"{name}_{i}", {"ALL_PROXY": "http://all.test", "NO_PROXY": bypass}, url)
        for i, url in enumerate(urls)
    )


@pytest.mark.parametrize("name,env,url", cases, ids=[case[0] for case in cases])
def test_effective_route_matches_supported_httpx(monkeypatch, name, env, url):
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    proxies = urllib.request.getproxies_environment()
    monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
    monkeypatch.setattr(utils_module, "getproxies", lambda: proxies)
    monkeypatch.setattr(client_module, "HTTPTransport", RouteProbe)
    try:
        with httpx.Client(trust_env=True) as client:
            active = client.get(url).json()["route"] is not None
    except (httpx.InvalidURL, ValueError):
        active = True  # invalid configuration must not become direct permission
    if active:
        with pytest.raises(WebError):
            require_direct(url)
    else:
        require_direct(url)


def test_system_proxy_is_also_refused_without_echoing_credentials(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {
            "https": "http://SYNTHETIC-PASSWORD@system-proxy.test:8888",
        },
    )
    with pytest.raises(WebError) as caught:
        require_direct("https://public.test")
    assert "SYNTHETIC-PASSWORD" not in str(caught.value)
    assert "system-proxy.test" not in str(caught.value)


@pytest.mark.parametrize("proxy", ["not a proxy", "ftp://unsupported.test", "http://host:invalid"])
def test_invalid_proxy_configuration_is_not_treated_as_direct(monkeypatch, proxy):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": proxy})
    with pytest.raises(WebError):
        require_direct("https://public.test")


@pytest.mark.parametrize(
    "proxy,exempt",
    [("http://proxy.test", False), ("http://proxy.test", True), ("ftp://bad.test", False)],
)
def test_owned_fetch_refuses_proxy_before_dns_or_uses_no_proxy(monkeypatch, proxy, exempt):
    from lohra.web.fetch import fetch_url
    from tests.web_socket_lab import PUBLIC, Lab

    with Lab(lambda *_: [PUBLIC]) as lab:
        monkeypatch.setattr(
            urllib.request,
            "getproxies",
            lambda: {
                "https": proxy,
                "no": "public.test" if exempt else "",
            },
        )
        if exempt:
            assert fetch_url("https://public.test") == "ok"
            assert len(lab.connected) == 1
        else:
            with pytest.raises(WebError):
                fetch_url("https://public.test")
            assert lab.dns == lab.connected == []


def test_redirect_checks_fresh_proxy_route_before_resolving_next_host(monkeypatch):
    from lohra.web.fetch import fetch_url
    from tests.web_socket_lab import PUBLIC, Lab

    redirect = b"HTTP/1.1 302 Found\r\nLocation: https://second.test\r\nContent-Length: 0\r\n\r\n"
    with Lab(lambda *_: [PUBLIC], responses=[redirect]) as lab:
        # First host is exempt; the next logical host requires a proxy.
        monkeypatch.setattr(
            urllib.request,
            "getproxies",
            lambda: {
                "https": "http://proxy.test",
                "no": "first.test",
            },
        )
        with pytest.raises(WebError, match="configured proxy"):
            fetch_url("https://first.test")
        assert [row[0] for row in lab.dns] == ["first.test", "first.test", PUBLIC]
        assert len(lab.connected) == 1


def test_proxy_added_between_hops_is_not_silently_bypassed(monkeypatch):
    from lohra.web.fetch import fetch_url
    from tests.web_socket_lab import PUBLIC, Lab

    calls = []

    def settings():
        calls.append(True)
        return {} if len(calls) == 1 else {"https": "http://proxy.test"}

    redirect = b"HTTP/1.1 302 Found\r\nLocation: /final\r\nContent-Length: 0\r\n\r\n"
    with Lab(lambda *_: [PUBLIC], responses=[redirect]) as lab:
        monkeypatch.setattr(urllib.request, "getproxies", settings)
        with pytest.raises(WebError, match="configured proxy"):
            fetch_url("https://first.test")
        assert len(calls) == 2
        assert [row[0] for row in lab.dns] == ["first.test", "first.test", PUBLIC]
        assert len(lab.connected) == 1
