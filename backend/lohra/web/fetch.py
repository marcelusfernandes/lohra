"""Fetch a URL safely — manual redirects, each hop re-validated, body capped.

We do NOT use httpx ``follow_redirects=True``: it would chase a redirect from a
public host straight to an internal IP, defeating the SSRF guard. Instead we
follow hops by hand and call ``validate_public_url`` on every one. The body is
read incrementally and stopped at ``max_bytes`` so a huge response can't exhaust
memory or context.
"""

from __future__ import annotations

import httpx

from lohra.web.safety import Resolver, WebError, validate_public_url

_DEFAULT_TIMEOUT = 10.0
_MAX_BYTES = 2_000_000
_MAX_REDIRECTS = 4
_USER_AGENT = "lohra-web/0.1 (+https://github.com/lohra)"


def _is_textual(content_type: str) -> bool:
    """Whether a Content-Type is text we can usefully decode (else: binary)."""
    if not content_type:
        return True  # no hint — attempt to read as text
    ct = content_type.lower()
    return ct.startswith("text/") or any(
        token in ct for token in ("json", "xml", "html", "javascript", "csv")
    )


def _read_capped(response: httpx.Response, max_bytes: int) -> str:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    raw = b"".join(chunks)[:max_bytes]
    return raw.decode(response.encoding or "utf-8", errors="replace")


def fetch_url(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _MAX_BYTES,
    max_redirects: int = _MAX_REDIRECTS,
    resolver: Resolver | None = None,
) -> str:
    """Fetch ``url`` and return its body text. Raises ``WebError`` if unsafe.

    Network/HTTP errors propagate as ``httpx.HTTPError`` for the caller to map.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=timeout, follow_redirects=False, headers={"User-Agent": _USER_AGENT}
        )
    try:
        current = url
        for _ in range(max_redirects + 1):
            validate_public_url(current, resolver=resolver)
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise WebError("redirect response had no Location header")
                    current = str(httpx.URL(current).join(location))
                    continue
                content_type = response.headers.get("content-type", "")
                if not _is_textual(content_type):
                    raise WebError(
                        f"content is not text (Content-Type: {content_type or 'unknown'}); "
                        "web_fetch only reads text pages"
                    )
                return _read_capped(response, max_bytes)
        raise WebError(f"too many redirects (more than {max_redirects})")
    finally:
        if owns_client:
            client.close()
