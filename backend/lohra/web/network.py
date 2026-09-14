"""Public HTTPCore backend: validated numeric dials and one TCP/TLS deadline."""

from __future__ import annotations

import time

import httpcore

from lohra.web.safety import Resolver, WebError, resolve_public_addresses


def _remaining(deadline: float | None, timeout: float | None) -> float | None:
    if deadline is None:
        return timeout
    left = deadline - time.monotonic()
    if left <= 0:
        raise httpcore.ConnectTimeout("web connection establishment deadline exceeded")
    return left if timeout is None else min(left, timeout)


class DeadlineStream(httpcore.NetworkStream):
    """Carry the TCP deadline into TLS without changing read/write timeouts."""

    def __init__(self, stream: httpcore.NetworkStream, deadline: float | None):
        self._stream = stream
        self._deadline = deadline

    def read(self, max_bytes, timeout=None):
        return self._stream.read(max_bytes, timeout=timeout)

    def write(self, buffer, timeout=None):
        return self._stream.write(buffer, timeout=timeout)

    def get_extra_info(self, info):
        return self._stream.get_extra_info(info)

    def close(self):
        self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        try:
            self._stream = self._stream.start_tls(
                ssl_context,
                server_hostname=server_hostname,
                timeout=_remaining(self._deadline, timeout),
            )
            _remaining(self._deadline, timeout)  # refuse even an over-budget backend success
            return self
        except BaseException:
            self.close()
            raise


class PublicNetworkBackend(httpcore.NetworkBackend):
    """Resolve/validate once per physical connection, then use only numeric IPs.

    The OS resolver can block: the connect/TLS deadline starts AFTER DNS and is
    shared by numeric TCP attempts and TLS. No request/read replay is performed.
    ``resolver`` and the OS backend are trusted code seams, never tool arguments.
    """

    def __init__(self, resolver: Resolver | None = None):
        self._resolver = resolver
        self._backend = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = resolve_public_addresses(host, resolver=self._resolver)
        deadline = None if timeout is None else time.monotonic() + timeout
        for index, address in enumerate(addresses):
            try:
                stream = self._backend.connect_tcp(
                    address,
                    port,
                    timeout=_remaining(deadline, timeout),
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise
                continue
            try:
                _remaining(deadline, timeout)
            except BaseException:
                stream.close()
                raise
            return DeadlineStream(stream, deadline)
        raise httpcore.ConnectError(
            "no validated address available"
        )  # defensive; resolver rejects empty

    def connect_unix_socket(self, *args, **kwargs):
        raise WebError("web_fetch does not support Unix sockets")

    def sleep(self, seconds):
        self._backend.sleep(seconds)
