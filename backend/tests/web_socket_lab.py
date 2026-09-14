"""Hermetic HTTPX/HTTPCore wire path: all DNS/socket/TLS endpoints are synthetic."""

from contextlib import ExitStack
from unittest.mock import patch
import ipaddress
import json
import os
import socket
import ssl

PUBLIC = "93.184.216.34"
PUBLIC6 = "2606:4700:4700::1111"
BODY = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"


class Lab:
    def __init__(self, addresses, responses=None, tls_error=False, env=None):
        self.addresses = addresses
        self.responses = list(responses or [BODY])
        self.tls_error = tls_error
        self.env = env or {}
        self.dns = []
        self.connected = []
        self.tls = []
        self.wire = []
        self.sockets = []
        self.counts = {}

    def resolve(self, host, port, *args, **kwargs):
        self.counts[host] = self.counts.get(host, 0) + 1
        try:
            ips = [str(ipaddress.ip_address(host))]
        except ValueError:
            ips = self.addresses(host, self.counts[host], port)
        self.dns.append([host, port, ips])
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (ip, port or 0, 0, 0) if ":" in ip else (ip, port or 0),
            )
            for ip in ips
        ]

    def make_socket(self, *args, **kwargs):
        s = FakeSocket(self)
        self.sockets.append(s)
        return s

    def wrap(self, context, sock, *, server_hostname=None, **kwargs):
        self.tls.append([server_hostname, context.check_hostname, int(context.verify_mode)])
        if self.tls_error:
            raise ssl.SSLCertVerificationError("synthetic mismatched certificate")
        return sock

    def __enter__(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        self.stack.enter_context(patch("urllib.request.getproxies", return_value={}))
        self.stack.enter_context(patch("socket.getaddrinfo", self.resolve))
        self.stack.enter_context(patch("socket.socket", self.make_socket))
        lab = self

        def wrap(context, sock, **kwargs):
            return lab.wrap(context, sock, **kwargs)

        self.stack.enter_context(patch.object(ssl.SSLContext, "wrap_socket", wrap))
        self.stack.enter_context(
            patch("httpcore._backends.sync.is_socket_readable", lambda _: False)
        )
        return self

    def __exit__(self, *args):
        self.stack.close()
        assert all(s.closed for s in self.sockets), "probe left socket resource open"

    def emit(self, case, **extra):
        hosts = [
            line.decode()
            for req in self.wire
            for line in req.split(b"\r\n")
            if line.lower().startswith(b"host:")
        ]
        print(
            json.dumps(
                dict(
                    case=case,
                    dns=self.dns,
                    connected=self.connected,
                    tls=self.tls,
                    host_headers=hosts,
                    **extra,
                ),
                sort_keys=True,
            )
        )


class FakeSocket:
    def __init__(self, lab):
        self.lab = lab
        self.closed = False
        self.peer = None
        self.response = b""

    def connect(self, sockaddr):
        self.peer = sockaddr
        self.lab.connected.append(list(sockaddr))

    def send(self, data):
        raw = bytes(data)
        self.lab.wire.append(raw)
        if b"\r\n\r\n" in raw:
            self.response += self.lab.responses.pop(0) if self.lab.responses else BODY
        return len(raw)

    def recv(self, n):
        out, self.response = self.response[:n], self.response[n:]
        return out

    def close(self):
        self.closed = True

    def settimeout(self, *args):
        pass

    def setsockopt(self, *args):
        pass

    def bind(self, *args):
        pass

    def getpeername(self):
        return self.peer

    def getsockname(self):
        return ("192.0.2.1", 12345)
