"""Exception translation and cleanup follow the public streaming transport contract."""

import httpcore
import httpx
import pytest

from lohra.web import network
from lohra.web.transport import PublicTransport
from tests.web_socket_lab import BODY, PUBLIC


def resolver(*_):
    return [(2, 1, 6, "", (PUBLIC, 0))]


@pytest.mark.parametrize(
    "name,phase",
    [
        ("ConnectError", "connect"),
        ("ConnectTimeout", "connect"),
        ("WriteTimeout", "write"),
        ("ReadError", "headers"),
        ("ReadTimeout", "headers"),
        ("RemoteProtocolError", "headers"),
        ("LocalProtocolError", "headers"),
        ("ReadError", "body"),
        ("ReadTimeout", "body"),
        ("RemoteProtocolError", "body"),
        ("ReadError", "close"),
    ],
)
def test_transport_maps_errors_during_establishment_iteration_and_close(monkeypatch, name, phase):
    calls = []
    failure = getattr(httpcore, name)("synthetic transport error")

    class Stream(httpcore.MockStream):
        def read(self, max_bytes, timeout=None):
            calls.append("read")
            if phase == "headers" or phase == "body" and calls.count("read") == 2:
                raise failure
            return super().read(max_bytes, timeout)

        def write(self, buffer, timeout=None):
            calls.append("write")
            if phase == "write":
                raise failure

        def close(self):
            calls.append("close")
            if phase == "close":
                raise failure

    def tcp(self, host, port, **kwargs):
        calls.append("connect")
        if phase == "connect":
            raise failure
        return Stream([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n", b"ok"])

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", tcp)
    with pytest.raises(getattr(httpx, name)) as caught:
        with httpx.Client(
            transport=PublicTransport(ssl_context=None, resolver=resolver), trust_env=False
        ) as client:
            client.get("http://public.test")
    assert caught.value.__cause__ is failure
    assert calls.count("connect") == 1
    if phase != "connect":
        assert "close" in calls


@pytest.mark.parametrize("phase", ["tls", "headers", "body"])
@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_baseexception_closes_stream_without_retry(monkeypatch, phase, exception):
    streams = []
    sentinel = exception("synthetic interruption")

    class Stream(httpcore.MockStream):
        closed = False
        reads = 0

        def start_tls(self, *args, **kwargs):
            if phase == "tls":
                raise sentinel
            return self

        def read(self, max_bytes, timeout=None):
            self.reads += 1
            if phase == "headers" or self.reads == 2:
                raise sentinel
            return super().read(max_bytes, timeout)

        def close(self):
            self.closed = True

    def tcp(self, host, port, **kwargs):
        stream = Stream([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n", b"ok"])
        streams.append(stream)
        return stream

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", tcp)
    with pytest.raises(exception) as caught:
        with httpx.Client(
            transport=PublicTransport(ssl_context=None, resolver=resolver), trust_env=False
        ) as client:
            client.get("https://public.test")
    assert caught.value is sentinel
    assert len(streams) == 1 and streams[0].closed


@pytest.mark.parametrize("phase", ["tcp", "tls"])
def test_even_late_backend_success_is_closed_and_refused(monkeypatch, phase):
    clock = [0]
    monkeypatch.setattr(network.time, "monotonic", lambda: clock[0])

    class Stream(httpcore.MockStream):
        closed = False

        def start_tls(self, *args, **kwargs):
            clock[0] = 11
            return self

        def close(self):
            self.closed = True

    stream = Stream([BODY])

    def tcp(self, host, port, **kwargs):
        if phase == "tcp":
            clock[0] = 11
        return stream

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", tcp)
    with httpx.Client(
        transport=PublicTransport(ssl_context=None, resolver=resolver), trust_env=False, timeout=10
    ) as client:
        with pytest.raises(httpx.ConnectTimeout):
            client.get("https://public.test")
    assert stream.closed


@pytest.mark.parametrize(
    "name",
    [
        "WriteError",
        "PoolTimeout",
        "ProxyError",
        "UnsupportedProtocol",
        "TimeoutException",
        "NetworkError",
        "ProtocolError",
    ],
)
def test_public_pool_exception_surface_is_preserved(monkeypatch, name):
    def refuse(self, request):
        raise getattr(httpcore, name)("synthetic pool failure")

    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", refuse)
    with httpx.Client(
        transport=PublicTransport(ssl_context=None, resolver=resolver), trust_env=False
    ) as client:
        with pytest.raises(getattr(httpx, name)):
            client.get("http://public.test")
