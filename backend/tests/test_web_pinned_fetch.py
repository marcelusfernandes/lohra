"""Default fetch must bind every physical connection to a validated DNS answer."""

import ipaddress

import pytest

from lohra.web.fetch import fetch_url
from lohra.web.safety import WebError
from tests.web_socket_lab import BODY, PUBLIC, PUBLIC6, Lab


@pytest.mark.parametrize(
    "scheme,public,private",
    [
        ("http", PUBLIC, "127.0.0.1"),
        ("http", PUBLIC6, "::1"),
        ("https", PUBLIC, "169.254.169.254"),
        ("http", PUBLIC6, "::ffff:127.0.0.1"),
    ],
)
def test_default_fetch_never_dials_changed_private_answer(scheme, public, private):
    with Lab(lambda host, count, port: [public if count == 1 else private]) as lab:
        try:
            fetch_url(f"{scheme}://rebind.test/path")
        except WebError:
            pass
        assert all(ipaddress.ip_address(target[0]).is_global for target in lab.connected)


def test_redirect_never_dials_changed_private_answer():
    redirect = (
        b"HTTP/1.1 302 Found\r\nLocation: https://second.test/final\r\nContent-Length: 0\r\n\r\n"
    )
    with Lab(
        lambda host, count, port: [PUBLIC if host == "first.test" or count == 1 else "127.0.0.1"],
        responses=[redirect, BODY],
    ) as lab:
        try:
            fetch_url("https://first.test/start")
        except WebError:
            pass
        assert lab.connected and all(target[0] == PUBLIC for target in lab.connected)
