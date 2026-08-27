"""Tests for fetch_url — redirect re-validation and the body cap (mocked httpx)."""

import httpx
import pytest

from lohra.web.fetch import fetch_url
from lohra.web.safety import WebError

def _PUBLIC(host, port):
    # Map every host to a public IP so the guard lets the mock transport answer.
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def _INTERNAL(host, port):
    return [(2, 1, 6, "", ("127.0.0.1", 0))]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_fetches_body_text():
    def handler(request):
        return httpx.Response(200, text="<h1>hello</h1>")

    out = fetch_url("https://example.com/", client=_client(handler), resolver=_PUBLIC)
    assert out == "<h1>hello</h1>"


def test_follows_redirects_revalidating_each_hop():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.com/final"})
        return httpx.Response(200, text="arrived")

    out = fetch_url("https://example.com/start", client=_client(handler), resolver=_PUBLIC)
    assert out == "arrived"


def test_redirect_to_internal_is_refused():
    # First hop public; the redirect target resolves internal -> must be refused.
    calls = {"n": 0}

    def resolver(host, port):
        calls["n"] += 1
        # first validation public, second (the redirect target) internal
        ip = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
        return [(2, 1, 6, "", (ip, 0))]

    def handler(request):
        return httpx.Response(302, headers={"location": "https://internal.test/secret"})

    with pytest.raises(WebError):
        fetch_url("https://example.com/start", client=_client(handler), resolver=resolver)


def test_body_is_capped():
    def handler(request):
        return httpx.Response(200, text="x" * 10_000)

    out = fetch_url(
        "https://example.com/", client=_client(handler), resolver=_PUBLIC, max_bytes=100
    )
    assert len(out) == 100


def test_non_text_content_is_refused():
    def handler(request):
        return httpx.Response(
            200, content=b"%PDF-1.7 binary", headers={"content-type": "application/pdf"}
        )

    with pytest.raises(WebError):
        fetch_url("https://example.com/doc.pdf", client=_client(handler), resolver=_PUBLIC)


def test_missing_content_type_is_read_as_text():
    def handler(request):
        return httpx.Response(200, content=b"plain", headers={})

    out = fetch_url("https://example.com/", client=_client(handler), resolver=_PUBLIC)
    assert out == "plain"


def test_too_many_redirects_raises():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    with pytest.raises(WebError):
        fetch_url(
            "https://example.com/loop", client=_client(handler), resolver=_PUBLIC, max_redirects=2
        )


def test_unsafe_url_is_refused_before_any_request():
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("should not have made a request")

    with pytest.raises(WebError):
        fetch_url("https://internal.test/", client=_client(handler), resolver=_INTERNAL)
