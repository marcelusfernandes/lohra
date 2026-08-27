"""Tests for the web_fetch and web_search tool handlers (backend/fetch stubbed)."""

import json

import httpx

import lohra.web.tool as web_tool
from lohra.web.safety import WebError
from lohra.web.search import SearchResult, SearchUnavailable


def _result(handler_out):
    return json.loads(handler_out)


# --- web_fetch ---


def test_web_fetch_returns_extracted_text(monkeypatch):
    monkeypatch.setattr(web_tool, "fetch_url", lambda url: "<h1>Hi</h1><p>body</p>")
    out = _result(web_tool.web_fetch({"url": "https://example.com"}))
    assert out["ok"] is True
    assert out["text"] == "Hi body"
    assert out["url"] == "https://example.com"


def test_web_fetch_missing_url_errors():
    assert "error" in _result(web_tool.web_fetch({}))


def test_web_fetch_unsafe_url_surfaces_clean_error(monkeypatch):
    def boom(url):
        raise WebError("refusing to fetch a non-public address")

    monkeypatch.setattr(web_tool, "fetch_url", boom)
    out = _result(web_tool.web_fetch({"url": "http://127.0.0.1"}))
    assert "error" in out


def test_web_fetch_http_error_surfaces_clean_error(monkeypatch):
    def boom(url):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(web_tool, "fetch_url", boom)
    out = _result(web_tool.web_fetch({"url": "https://example.com"}))
    assert "error" in out


# --- web_search ---


class _FakeBackend:
    def __init__(self, results=None, error=None):
        self._results = results or []
        self._error = error

    def search(self, query, *, max_results=5):
        if self._error:
            raise self._error
        return self._results[:max_results]


def test_web_search_returns_results(monkeypatch):
    backend = _FakeBackend([SearchResult("T", "https://x.test", "snip")])
    monkeypatch.setattr(web_tool, "_backend", backend)
    out = _result(web_tool.web_search({"query": "hello"}))
    assert out["ok"] is True
    assert out["results"] == [{"title": "T", "url": "https://x.test", "snippet": "snip"}]


def test_web_search_missing_query_errors():
    assert "error" in _result(web_tool.web_search({"query": "  "}))


def test_web_search_caps_max_results(monkeypatch):
    seen = {}

    class B:
        def search(self, query, *, max_results=5):
            seen["max_results"] = max_results
            return []

    monkeypatch.setattr(web_tool, "_backend", B())
    web_tool.web_search({"query": "x", "max_results": 99})
    assert seen["max_results"] == 10


def test_web_search_unavailable_is_distinct_error(monkeypatch):
    backend = _FakeBackend(error=SearchUnavailable("rate limited"))
    monkeypatch.setattr(web_tool, "_backend", backend)
    out = _result(web_tool.web_search({"query": "x"}))
    assert "unavailable" in out["error"]


def test_set_search_backend_swaps_the_default():
    original = web_tool._backend
    sentinel = _FakeBackend([SearchResult("S", "https://s.test", "")])
    try:
        web_tool.set_search_backend(sentinel)
        assert web_tool._backend is sentinel
    finally:
        web_tool.set_search_backend(original)


def test_tools_are_registered():
    from lohra.tools import load_builtin_tools, registry

    load_builtin_tools()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert {"web_fetch", "web_search"} <= names
