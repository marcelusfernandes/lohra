"""Actual pool/transport sequence with simulated clock/I/O: no sockets or sleeps."""

import httpcore
import httpx
import pytest

from lohra.web import network
from lohra.web.transport import PublicTransport
from tests.web_socket_lab import BODY, PUBLIC, PUBLIC6


def _resolver(*_):
    return [(2, 1, 6, "", (PUBLIC, 0)), (10, 1, 6, "", (PUBLIC6, 0))]


def test_one_budget_spans_ip_fallback_tcp_and_tls(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(network.time, "monotonic", lambda: clock[0])
    calls, streams = [], []

    class Stream(httpcore.MockStream):
        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            calls.append(("tls", server_hostname, timeout))
            clock[0] += timeout
            raise httpcore.ConnectTimeout("synthetic TLS consumed remainder")

        def close(self):
            self.closed = True

    def tcp(self, host, port, timeout=None, **kwargs):
        calls.append(("tcp", host, timeout))
        if host == PUBLIC:
            clock[0] += 3
            raise httpcore.ConnectError("first IP refused")
        clock[0] += 5
        stream = Stream([BODY])
        stream.closed = False
        streams.append(stream)
        return stream

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", tcp)
    with httpx.Client(
        transport=PublicTransport(ssl_context=None, resolver=_resolver), trust_env=False, timeout=10
    ) as client:
        with pytest.raises(httpx.ConnectTimeout):
            client.get("https://logical.test:8443")
    assert calls == [("tcp", PUBLIC, 10), ("tcp", PUBLIC6, 7), ("tls", "logical.test", 2)]
    assert clock[0] == 10 and all(stream.closed for stream in streams)


def test_budget_exhausted_before_second_ip_never_dials_again(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(network.time, "monotonic", lambda: clock[0])

    def tcp(self, host, port, **kwargs):
        calls.append(host)
        clock[0] += 10
        raise httpcore.ConnectTimeout("first timed out")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", tcp)
    backend = network.PublicNetworkBackend(_resolver)
    with pytest.raises(httpcore.ConnectTimeout):
        backend.connect_tcp("logical.test", 443, timeout=10)
    assert calls == [PUBLIC]


def test_read_and_write_have_separate_timeouts_and_no_ip_retry(monkeypatch):
    calls = []

    class Stream(httpcore.MockStream):
        def read(self, max_bytes, timeout=None):
            calls.append(("read", timeout))
            raise httpcore.ReadTimeout("synthetic read failed")

        def write(self, buffer, timeout=None):
            calls.append(("write", timeout))

        def close(self):
            calls.append(("close", None))

    def tcp(self, host, port, **kwargs):
        calls.append(("tcp", host))
        return Stream([])

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", tcp)
    with httpx.Client(
        transport=PublicTransport(ssl_context=None, resolver=_resolver),
        trust_env=False,
        timeout=httpx.Timeout(connect=10, read=3, write=4, pool=5),
    ) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.get("http://logical.test")
    assert [(kind, value) for kind, value in calls if kind == "tcp"] == [("tcp", PUBLIC)]
    assert ("write", 4) in calls and ("read", 3) in calls and ("close", None) in calls


def test_tcp_failure_can_fall_back_only_within_validated_snapshot(monkeypatch):
    calls, resolutions = [], []

    def resolve(host, port):
        resolutions.append(host)
        return _resolver()

    class Stream(httpcore.MockStream):
        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            calls.append(("tls", server_hostname))
            return self

    def tcp(self, host, port, **kwargs):
        calls.append(("tcp", host))
        if host == PUBLIC:
            raise httpcore.ConnectError("first refused")
        return Stream([BODY])

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", tcp)
    with httpx.Client(
        transport=PublicTransport(ssl_context=None, resolver=resolve), trust_env=False, timeout=10
    ) as client:
        assert client.get("https://logical.test").text == "ok"
    assert resolutions == ["logical.test"]
    assert calls == [("tcp", PUBLIC), ("tcp", PUBLIC6), ("tls", "logical.test")]
