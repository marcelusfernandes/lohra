"""Own sender, disconnect listener and cleanup on every ASGI HTTP version."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, StreamingResponse

from lohra.server.stream_bridge import StreamBridge
from lohra.server.stream_workers import StreamUnavailable, StreamWorkers


class OwnedStreamResponse(StreamingResponse):
    def __init__(
        self, workers: StreamWorkers, service: Any, kwargs: dict,
        format_body: Callable[[StreamBridge], AsyncIterator[str]],
    ) -> None:
        # Body ownership starts at ASGI entry, never while merely building a
        # response that cancellation may prevent the framework from invoking.
        super().__init__((), media_type="text/event-stream")
        self._workers = workers
        self._service = service
        self._kwargs = kwargs
        self._format_body = format_body

    async def __call__(self, scope, receive, send) -> None:
        try:
            worker = self._workers.start(self._service, self._kwargs)
        except StreamUnavailable as exc:
            await JSONResponse(status_code=503, content={
                "error": {"message": str(exc), "type": "server_error"},
            })(scope, receive, send)
            return
        self.body_iterator = self._format_body(worker.bridge)
        tasks = []
        completed = False
        try:
            sender = asyncio.create_task(self.stream_response(send))
            listener = asyncio.create_task(self.listen_for_disconnect(receive))
            tasks = [sender, listener]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                await sender
                completed = True
            else:
                await listener
        except OSError as exc:
            raise ClientDisconnect() from exc
        finally:
            # Signal and free blocked puts before any cancellable await. The
            # producer remains registered if it cannot honor this signal yet.
            worker.bridge.close(cancel=not completed)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.body_iterator.aclose()
            await self._workers.drain(worker)
