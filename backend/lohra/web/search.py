"""Web search behind a pluggable backend; ships a keyless DuckDuckGo default.

The ``SearchBackend`` protocol is the seam: swap in an API-key backend later
without touching the tool. A *blocked/unavailable* backend raises
``SearchUnavailable`` (distinct from a genuine empty result set, which returns
``[]``) so the agent never narrates "no results" when it was actually throttled.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from lohra.web.safety import WebError

_USER_AGENT = "lohra-web/0.1 (+https://github.com/lohra)"
_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
_DEFAULT_MAX_RESULTS = 5


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchUnavailable(WebError):
    """The search backend was unreachable, throttled, or refused the request."""


class SearchBackend(Protocol):
    def search(self, query: str, *, max_results: int = _DEFAULT_MAX_RESULTS) -> list[SearchResult]:
        ...


def _decode_ddg_href(href: str) -> str:
    """DDG wraps result links as ``//duckduckgo.com/l/?uddg=<encoded>``."""
    if not href:
        return ""
    query = parse_qs(urlparse(href).query)
    if "uddg" in query:
        return query["uddg"][0]
    return href if href.startswith("http") else ""


class _DDGResultParser(HTMLParser):
    """Pull (title, url, snippet) triples out of DDG's HTML results page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._mode: str | None = None
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._url = ""

    def _flush(self) -> None:
        title = " ".join(self._title).strip()
        if title and self._url:
            self.results.append(SearchResult(title, self._url, " ".join(self._snippet).strip()))
        self._title, self._snippet, self._url = [], [], ""

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        classes = dict(attrs).get("class", "") or ""
        if "result__a" in classes:
            self._flush()  # a new result begins; commit the previous one
            self._mode = "title"
            self._url = _decode_ddg_href(dict(attrs).get("href", ""))
        elif "result__snippet" in classes:
            self._mode = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._mode = None

    def handle_data(self, data: str) -> None:
        if self._mode == "title":
            self._title.append(data.strip())
        elif self._mode == "snippet":
            self._snippet.append(data.strip())

    def close(self) -> None:
        super().close()
        self._flush()


def parse_ddg_html(html: str, max_results: int) -> list[SearchResult]:
    parser = _DDGResultParser()
    parser.feed(html)
    parser.close()
    return parser.results[:max_results]


class DuckDuckGoBackend:
    """Keyless search via DuckDuckGo's HTML endpoint (no API key required)."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def search(self, query: str, *, max_results: int = _DEFAULT_MAX_RESULTS) -> list[SearchResult]:
        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=10.0, headers={"User-Agent": _USER_AGENT}
        )
        try:
            try:
                response = client.post(_DDG_ENDPOINT, data={"q": query})
            except httpx.HTTPError as exc:
                raise SearchUnavailable(f"search request failed: {exc}") from exc
            if response.status_code != 200:
                raise SearchUnavailable(f"search backend returned HTTP {response.status_code}")
            return parse_ddg_html(response.text, max_results)
        finally:
            if owns_client:
                client.close()
