"""Tests for web search — DDG HTML parsing and blocked-vs-empty distinction."""

import httpx
import pytest

from lohra.web.search import (
    DuckDuckGoBackend,
    SearchResult,
    SearchUnavailable,
    parse_ddg_html,
)

_DDG_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=1">First Title</a>
  <a class="result__snippet" href="x">first snippet text</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fb">Second Title</a>
  <a class="result__snippet" href="y">second snippet</a>
</div>
"""


def test_parses_results_with_decoded_urls():
    results = parse_ddg_html(_DDG_HTML, max_results=10)
    assert results[0] == SearchResult(
        title="First Title", url="https://example.com/a", snippet="first snippet text"
    )
    assert results[1].url == "https://example.org/b"


def test_respects_max_results():
    assert len(parse_ddg_html(_DDG_HTML, max_results=1)) == 1


def test_parses_title_without_snippet():
    html = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.test">Only Title</a>'
    results = parse_ddg_html(html, max_results=5)
    assert results == [SearchResult(title="Only Title", url="https://x.test", snippet="")]


def test_empty_page_returns_no_results():
    assert parse_ddg_html("<html><body>nothing</body></html>", max_results=5) == []


def _backend(handler):
    return DuckDuckGoBackend(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_backend_returns_parsed_results():
    def handler(request):
        return httpx.Response(200, text=_DDG_HTML)

    results = _backend(handler).search("example", max_results=5)
    assert len(results) == 2


def test_backend_200_with_no_results_is_empty_not_unavailable():
    def handler(request):
        return httpx.Response(200, text="<html>no results</html>")

    assert _backend(handler).search("zzz", max_results=5) == []


def test_backend_non_200_is_unavailable():
    def handler(request):
        return httpx.Response(429, text="rate limited")

    with pytest.raises(SearchUnavailable):
        _backend(handler).search("example", max_results=5)


def test_backend_network_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(SearchUnavailable):
        _backend(handler).search("example", max_results=5)
