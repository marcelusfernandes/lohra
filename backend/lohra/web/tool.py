"""The ``web_fetch`` and ``web_search`` tools — plain, stateless, self-registering.

Unlike vision/image_gen these need no provider client, so they register real
handlers (like fs/terminal) and run anywhere. ``web_search`` reads a module-level
backend that defaults to keyless DuckDuckGo; ``set_search_backend`` swaps it (for
config or tests). Safety (SSRF) lives in the fetch layer, so the handlers stay thin.
"""

from __future__ import annotations

from typing import Any

import httpx

from lohra.tools.registry import registry, tool_error, tool_result
from lohra.web.extract import html_to_text
from lohra.web.fetch import fetch_url
from lohra.web.safety import WebError
from lohra.web.search import DuckDuckGoBackend, SearchBackend, SearchUnavailable

_MAX_SEARCH_RESULTS = 10
_DEFAULT_SEARCH_RESULTS = 5

_backend: SearchBackend = DuckDuckGoBackend()


def set_search_backend(backend: SearchBackend) -> None:
    """Replace the default search backend (config/testing seam)."""
    global _backend
    _backend = backend


def _coerce_max_results(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_SEARCH_RESULTS
    return max(1, min(n, _MAX_SEARCH_RESULTS))


def web_fetch(args: dict[str, Any], **_kwargs: Any) -> str:
    url = args.get("url")
    if not url or not isinstance(url, str):
        return tool_error("missing required argument 'url' (string)")
    try:
        html = fetch_url(url)
    except WebError as exc:
        return tool_error(str(exc), url=url)
    except httpx.HTTPError as exc:
        return tool_error(f"could not fetch the page: {exc}", url=url)
    return tool_result(url=url, text=html_to_text(html))


def web_search(args: dict[str, Any], **_kwargs: Any) -> str:
    query = args.get("query")
    if not query or not str(query).strip():
        return tool_error("missing required argument 'query' (string)")
    max_results = _coerce_max_results(args.get("max_results"))
    try:
        results = _backend.search(str(query), max_results=max_results)
    except SearchUnavailable as exc:
        return tool_error(f"search is unavailable right now: {exc}", query=query)
    except (WebError, httpx.HTTPError) as exc:
        return tool_error(f"search failed: {exc}", query=query)
    return tool_result(
        query=query,
        results=[{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results],
    )


_FETCH_SCHEMA = {
    "description": (
        "Fetch a web page by URL and return its readable text content. Use this "
        "to read an article, doc, or page the conversation refers to. Only public "
        "http(s) URLs are allowed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to fetch"},
        },
        "required": ["url"],
    },
}

_SEARCH_SCHEMA = {
    "description": (
        "Search the web and return a list of results (title, url, snippet). Use "
        "this to find pages, then 'web_fetch' to read the most relevant one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for"},
            "max_results": {
                "type": "integer",
                "description": "How many results, 1-10 (default 5)",
            },
        },
        "required": ["query"],
    },
}

registry.register("web_fetch", "web", _FETCH_SCHEMA, web_fetch, emoji="🌐")
registry.register("web_search", "web", _SEARCH_SCHEMA, web_search, emoji="🔎")
