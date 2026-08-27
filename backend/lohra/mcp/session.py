"""MCP session — a sync facade over the async ``mcp`` SDK (spec §8).

Registry handlers are synchronous, but the MCP SDK is asyncio-based and a
session must be created and used on one event loop. ThreadedMCPSession owns a
background event loop, opens the connection on it, and exposes blocking
``list_tools``/``call_tool``/``close`` by submitting coroutines to that loop.

The bridge itself is SDK-free (driven by any async context manager yielding an
object with async ``list_tools``/``call_tool``); ``connect_stdio_session`` is the
thin, optional adapter that builds the real SDK opener.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Protocol

CONNECT_TIMEOUT_SECONDS = 30.0
CALL_TIMEOUT_SECONDS = 120.0


class MCPSession(Protocol):
    """Synchronous view of a connected MCP server."""

    def list_tools(self) -> list: ...
    def call_tool(self, name: str, args: dict) -> Any: ...
    def close(self) -> None: ...


class ThreadedMCPSession:
    """Run an async MCP session on a private background loop; call it sync.

    ``opener`` is an async context manager whose value exposes async
    ``list_tools()`` / ``call_tool(name, args)``. It is entered on the background
    loop and kept open until ``close()``.
    """

    def __init__(self, opener: Any) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._session: Any = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(opener,), daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=CONNECT_TIMEOUT_SECONDS):
            raise TimeoutError("timed out connecting to MCP server")
        if self._error is not None:
            self._thread.join(timeout=CONNECT_TIMEOUT_SECONDS)
            if not self._thread.is_alive() and not self._loop.is_closed():
                self._loop.close()  # only close a loop whose thread has finished
            raise self._error

    def _run(self, opener: Any) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve(opener))

    async def _serve(self, opener: Any) -> None:
        self._stop = asyncio.Event()
        try:
            async with opener as session:
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # surface connect/teardown failures to the caller
            self._error = exc
            self._ready.set()

    def _submit(self, coro: Any) -> Any:
        # Fail fast if the session is dead: a stopped loop never schedules the
        # coroutine, so without this guard the call would block the full timeout.
        if self._error is not None or not self._loop.is_running():
            coro.close()
            raise RuntimeError("MCP session is not connected")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=CALL_TIMEOUT_SECONDS)

    def list_tools(self) -> list:
        return self._submit(self._session.list_tools())

    def call_tool(self, name: str, args: dict) -> Any:
        return self._submit(self._session.call_tool(name, args))

    def close(self) -> None:
        if self._stop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=CONNECT_TIMEOUT_SECONDS)
        if not self._loop.is_closed():
            self._loop.close()


def _import_sdk():  # pragma: no cover - thin lazy import
    try:
        from contextlib import asynccontextmanager

        import mcp
        import mcp.client.stdio
        import mcp.client.streamable_http

        return mcp, mcp.client.stdio, mcp.client.streamable_http, asynccontextmanager
    except ImportError as exc:
        raise RuntimeError(
            "the mcp SDK is not installed; run `pip install lohra[mcp]`"
        ) from exc


def _view_of(session: Any):  # pragma: no cover - exercised only against the live SDK
    """Adapt an SDK ClientSession to the bridge's async list_tools/call_tool."""

    class _View:
        async def list_tools(self):
            return (await session.list_tools()).tools

        async def call_tool(self, name, args):
            return await session.call_tool(name, args)

    return _View()


def connect_stdio_session(config: Any) -> MCPSession:  # pragma: no cover - needs the SDK + a subprocess
    """Live stdio MCP session: wrap stdio_client + ClientSession into an opener."""
    mcp, stdio_mod, _http_mod, asynccontextmanager = _import_sdk()
    params = mcp.StdioServerParameters(
        command=config.command, args=list(config.args), env=config.env or None
    )

    @asynccontextmanager
    async def opener():
        async with stdio_mod.stdio_client(params) as (read, write):
            async with mcp.ClientSession(read, write) as session:
                await session.initialize()
                yield _view_of(session)

    return ThreadedMCPSession(opener())


def connect_http_session(config: Any) -> MCPSession:  # pragma: no cover - needs the SDK + a server
    """Live streamable-HTTP MCP session for a config with a ``url``."""
    mcp, _stdio_mod, http_mod, asynccontextmanager = _import_sdk()

    @asynccontextmanager
    async def opener():
        async with http_mod.streamablehttp_client(config.url) as (read, write, *_rest):
            async with mcp.ClientSession(read, write) as session:
                await session.initialize()
                yield _view_of(session)

    return ThreadedMCPSession(opener())


def connect_session(
    config: Any,
    *,
    stdio: Any = connect_stdio_session,
    http: Any = connect_http_session,
) -> MCPSession:
    """Route a config to the connector for its transport (stdio or http)."""
    if config.transport == "stdio":
        return stdio(config)
    if config.transport == "http":
        return http(config)
    raise RuntimeError(f"MCP transport {config.transport!r} not supported")
