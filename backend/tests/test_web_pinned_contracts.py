"""Wire-level positive/negative controls for the owned fetcher's pinning contract."""

import httpx
import pytest

from lohra.web.fetch import fetch_url
from lohra.web.safety import WebError
from tests.web_socket_lab import BODY, PUBLIC, PUBLIC6, Lab


@pytest.mark.parametrize("ip", [PUBLIC, PUBLIC6, "::ffff:93.184.216.34", "100.128.0.1", "8.8.8.8"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_public_snapshot_is_pinned_with_logical_idna_host_and_port(ip, scheme):
    with Lab(lambda *_: [ip]) as lab:
        assert fetch_url(f"{scheme}://straße.test:8443/a?q=1") == "ok"
        assert [row[0] for row in lab.dns] == ["xn--strae-oqa.test", "xn--strae-oqa.test", ip]
        assert all(row[0] == ip and row[1] == 8443 for row in lab.connected)
        assert b"Host: xn--strae-oqa.test:8443" in b"".join(lab.wire)
        assert b"GET /a?q=1 HTTP/1.1" in b"".join(lab.wire)
        if scheme == "https":
            assert lab.tls == [["xn--strae-oqa.test", True, 2]]


@pytest.mark.parametrize(
    "ips",
    [
        [],
        ["bad-IP"],
        [PUBLIC, "127.0.0.1"],
        [PUBLIC6, "::ffff:169.254.169.254"],
        ["fe80::1%en0"],
        ["100.64.0.1"],
        ["100.127.255.254"],
        ["::ffff:100.64.0.1"],
        [PUBLIC, "100.64.0.1"],
    ],
)
@pytest.mark.parametrize("on_second_resolution", [False, True])
def test_entire_dns_answer_must_be_public_before_any_socket(ips, on_second_resolution):
    def addresses(host, count, port):
        return [PUBLIC] if on_second_resolution and count == 1 else ips

    with Lab(addresses) as lab:
        with pytest.raises(WebError):
            fetch_url("https://example.test")
        assert lab.connected == lab.wire == []


@pytest.mark.parametrize(
    "malformed", [None, [None], [()], [(2, 1, 6, "", ())], [(2, 1, 6, "", (None, 0))]]
)
def test_malformed_resolver_output_is_refused_without_dial(malformed):
    with Lab(lambda *_: [PUBLIC]) as lab:
        with pytest.raises(WebError):
            fetch_url("https://example.test", resolver=lambda *_: malformed)
        assert lab.connected == []


def test_public_dns_rotation_pins_new_validated_set_and_never_requeries_hostname():
    with Lab(lambda host, count, port: [PUBLIC if count == 1 else PUBLIC6]) as lab:
        assert fetch_url("https://rotating.test") == "ok"
        assert [row[0] for row in lab.dns] == ["rotating.test", "rotating.test", PUBLIC6]
        assert lab.connected[0][0] == PUBLIC6


@pytest.mark.parametrize(
    "response,kwargs,result,error",
    [
        (BODY, {"max_bytes": 1}, "o", None),
        (BODY.replace(b"text/plain", b"application/pdf"), {}, None, WebError),
        (BODY.replace(b"Content-Type: text/plain\r\n", b""), {}, "ok", None),
        (BODY.replace(b"200 OK", b"404 Not Found"), {}, "ok", None),
        (
            b"HTTP/1.1 302 Found\r\nLocation: /loop\r\nContent-Length: 0\r\n\r\n",
            {"max_redirects": 0},
            None,
            WebError,
        ),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nbad", {}, None, httpx.RemoteProtocolError),
    ],
)
def test_owned_streams_close_for_cap_type_redirect_and_protocol_errors(
    response, kwargs, result, error
):
    with Lab(lambda *_: [PUBLIC], responses=[response]) as lab:
        if error:
            with pytest.raises(error):
                fetch_url("https://example.test", **kwargs)
        else:
            assert fetch_url("https://example.test", **kwargs) == result
        assert lab.sockets and all(sock.closed for sock in lab.sockets)


def test_injected_client_is_explicitly_trusted_and_caller_owned(monkeypatch):
    import urllib.request

    with Lab(lambda *_: [PUBLIC]) as lab:
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://proxy.test"})
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="injected")),
            trust_env=False,
        ) as client:
            assert fetch_url("https://example.test", client=client) == "injected"
            assert not client.is_closed
        assert [row[0] for row in lab.dns] == ["example.test"]
        assert lab.connected == []  # injected client owns its own network policy
