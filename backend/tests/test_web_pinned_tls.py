"""Default owned fetch and public transport preserve real TLS/HTTP identities."""

import json
import ssl
import threading
import time
import urllib.request

import httpcore
import httpx
import pytest

from lohra.web.environment import verified_context
from lohra.web.fetch import fetch_url
from lohra.web.safety import WebError
from lohra.web.transport import PublicTransport
from tests.web_socket_lab import PUBLIC
from tests.web_tls_lab import LOCAL, Server, certificates, openssl


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    root = tmp_path_factory.mktemp("ephemeral-web-ca")
    contexts = certificates(root)
    return root, contexts


@pytest.fixture
def tls(pki, monkeypatch):
    root, contexts = pki
    server = Server(contexts)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    monkeypatch.setenv("SSL_CERT_FILE", str(root / "ca.pem"))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    original = httpcore.SyncBackend.connect_tcp
    dials = []

    def local_tcp(self, host, port, **kwargs):
        # Only this trusted test boundary maps a validated public pin to loopback.
        assert host == PUBLIC
        dials.append((host, port))
        return original(self, LOCAL, port, **kwargs)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", local_tcp)
    yield server, dials
    server.shutdown()
    server.server_close()
    thread.join(3)
    assert not thread.is_alive()
    deadline = time.monotonic() + 3
    while server.active and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert not server.active


def public_resolver(host, port):
    assert host in {"first.test", "second.test", "wrong.test"}
    return [(2, 1, 6, "", (PUBLIC, 0))]


@pytest.mark.parametrize(
    "path,expected",
    [("/ok", "first.test"), ("/relative", "first.test"), ("/absolute", "second.test")],
)
def test_owned_fetch_preserves_hostname_port_and_redirect_identity(tls, path, expected):
    server, dials = tls
    result = json.loads(
        fetch_url(
            f"https://first.test:{server.server_port}{path}", resolver=public_resolver, timeout=3
        )
    )
    assert result["host"] == f"{expected}:{server.server_port}"
    assert result["sni"] == expected and result["tls"].startswith("TLSv1.")
    assert result["path"] == ("/ok" if path == "/ok" else "/final")
    assert dials == [(PUBLIC, server.server_port)] * (1 if path == "/ok" else 2)


def test_origin_pool_reuses_only_the_same_authenticated_hostname(tls):
    server, dials = tls
    transport = PublicTransport(ssl_context=verified_context(), resolver=public_resolver)
    with httpx.Client(transport=transport, trust_env=False, timeout=3) as client:
        for host in ("first.test", "first.test", "second.test"):
            request = client.build_request("GET", f"https://{host}:{server.server_port}/ok")
            request.extensions["sni_hostname"] = (
                "wrong.test"  # cannot override logical TLS identity
            )
            response = client.send(request)
            assert response.json()["sni"] == host
            assert response.url.host == host
            assert request.extensions["sni_hostname"] == "wrong.test"
    assert server.snis == ["first.test", "second.test"] and len(dials) == 2


@pytest.mark.parametrize("failure", ["mismatch", "untrusted"])
def test_real_certificate_failure_never_sends_http(tls, monkeypatch, failure):
    server, dials = tls
    if failure == "untrusted":
        monkeypatch.delenv("SSL_CERT_FILE")
    host = "wrong.test" if failure == "mismatch" else "first.test"
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        fetch_url(
            f"https://{host}:{server.server_port}/secret", resolver=public_resolver, timeout=3
        )
    assert len(dials) == 1 and server.requests == []


def test_ca_directory_and_file_precedence_are_verified_by_real_tls(tls, pki, monkeypatch):
    root, _ = pki
    server, _ = tls
    # x509 -hash works with both macOS LibreSSL and OpenSSL (rehash does not).
    digest = openssl(root, "x509", "-in", "ca.pem", "-hash", "-noout").stdout.decode().strip()
    assert len(digest) == 8 and int(digest, 16)
    (root / f"{digest}.0").symlink_to("ca.pem")
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.setenv("SSL_CERT_DIR", str(root))
    assert "first.test" in fetch_url(
        f"https://first.test:{server.server_port}/ok", resolver=public_resolver, timeout=3
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(root / "ca.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(root / "not-a-directory"))
    assert "first.test" in fetch_url(
        f"https://first.test:{server.server_port}/ok", resolver=public_resolver, timeout=3
    )
    context = verified_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_invalid_ca_configuration_is_explicit_and_does_not_dial(tmp_path, monkeypatch, variable):
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(variable, str(tmp_path / "PRIVATE-CA-PATH"))
    with pytest.raises(WebError) as caught:
        fetch_url("https://first.test", resolver=public_resolver)
    assert "PRIVATE-CA-PATH" not in str(caught.value)
