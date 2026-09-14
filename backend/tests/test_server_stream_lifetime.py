"""ASGI framing, backpressure and admission with explicit producer cleanup."""

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient
from openai.types.responses import ResponseFailedEvent

from lohra.server.app import create_openai_app
from lohra.server.service import CompletionInterrupted
from lohra.server.stream_bridge import StreamLimits
from tests.test_server_app import FakeService
from tests.test_server_stream_workers import RESULT

PATHS = ("/v1/chat/completions", "/v1/responses")


class Request:
    def __init__(self, app, path, version="2.4", *, hold_send=False):
        self.messages = []
        self.initial = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.release_send = asyncio.Event()
        self.hold_send = hold_send
        body = {"model": "synthetic", "stream": True}
        body.update({"input": "A"} if path.endswith("responses") else
                    {"messages": [{"role": "user", "content": "A"}]})
        self.body = json.dumps(body).encode()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": version},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
                 "raw_path": path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")],
                 "client": ("synthetic", 1), "server": ("synthetic", 2)}
        self.task = asyncio.create_task(app(scope, self.receive, self.send))

    async def receive(self):
        if self.body is not None:
            body, self.body = self.body, None
            return {"type": "http.request", "body": body, "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        self.messages.append(message)
        if message["type"] == "http.response.body":
            self.initial.set()
            if self.hold_send:
                await self.release_send.wait()

    async def cleanup(self):
        self.release_send.set()
        self.disconnect.set()
        if not self.task.done():
            self.task.cancel()
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 2)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("version", ["2.0", "2.4"])
@pytest.mark.parametrize("action", ["disconnect", "task-cancel"])
def test_stalled_send_and_full_queue_are_cancelable(path, version, action):
    async def case():
        loop = asyncio.get_running_loop()
        attempted = asyncio.Event()
        producer_threads = []

        class Service:
            interrupts = 0
            finished = False

            def run_cancellable(self, *, cancellation, on_delta, **kwargs):
                producer_threads.append(threading.current_thread())

                def interrupt():
                    self.interrupts += 1

                token = cancellation.bind(interrupt)
                try:
                    on_delta("a")  # first frame is held: this fills the single slot
                    loop.call_soon_threadsafe(attempted.set)
                    on_delta("🙂" * 100)
                    self.finished = True
                    return RESULT
                finally:
                    cancellation.unbind(token)

        service = Service()
        app = create_openai_app(service, stream_limits=StreamLimits(pieces=1, bytes=4))
        request = Request(app, path, version, hold_send=True)
        try:
            await asyncio.wait_for(asyncio.gather(request.initial.wait(), attempted.wait()), 2)
            worker, = app.state.stream_workers.snapshot()
            assert worker.bridge.buffered == (1, 1) and not service.finished
            if action == "disconnect":
                request.disconnect.set()
            else:
                request.task.cancel()
            done, _ = await asyncio.wait({request.task}, timeout=1)
            assert done and service.interrupts == 1
            assert worker.bridge.buffered == (0, 0)
            await asyncio.to_thread(worker.thread.join, 1)
            assert not worker.thread.is_alive() and service.finished
            assert worker.bridge.receipt.disposition == "cancelled"
        finally:
            await request.cleanup()
            for worker in app.state.stream_workers.snapshot():
                worker.bridge.close(cancel=True)
            for thread in producer_threads:
                await asyncio.to_thread(thread.join, 2)
            assert producer_threads and all(not thread.is_alive() for thread in producer_threads)

    asyncio.run(case())


@pytest.mark.parametrize("path", PATHS)
def test_draining_overload_is_503_before_stream_start_and_bindings_stay_local(path):
    async def case():
        records = []
        release = threading.Event()
        started = asyncio.Queue()
        loop = asyncio.get_running_loop()

        class Service:
            def run_cancellable(self, *, cancellation, **kwargs):
                record = {"interrupts": 0, "thread": threading.current_thread()}
                records.append(record)

                def interrupt():
                    record["interrupts"] += 1

                token = cancellation.bind(interrupt)
                loop.call_soon_threadsafe(started.put_nowait, record)
                try:
                    assert release.wait(5)
                    return RESULT
                finally:
                    cancellation.unbind(token)

        app = create_openai_app(Service(), stream_limits=StreamLimits(workers=2))
        requests = []
        try:
            for _ in range(2):
                requests.append(Request(app, path))
                await asyncio.wait_for(requests[-1].initial.wait(), 2)
                await asyncio.wait_for(started.get(), 2)
            requests[0].disconnect.set()
            await asyncio.wait_for(requests[0].task, 1)
            # The next HTTP request must neither signal nor replace its sibling.
            assert [r["interrupts"] for r in records] == [1, 0]
            requests.append(Request(app, path))
            await asyncio.wait_for(requests[-1].task, 1)
            assert requests[-1].messages[0]["status"] == 503
            assert len(records) == 2
            payload = json.loads(requests[-1].messages[1]["body"])
            assert "capacity" in payload["error"]["message"]
            assert not any(b"data:" in m.get("body", b"") for m in requests[-1].messages)
        finally:
            release.set()
            for request in requests:
                await request.cleanup()
            for record in records:
                await asyncio.to_thread(record["thread"].join, 2)
            assert records and all(not record["thread"].is_alive() for record in records)
            assert app.state.stream_workers.snapshot() == ()

    asyncio.run(case())


@pytest.mark.parametrize("known", [False, True])
def test_interrupted_responses_keep_nullable_or_known_floor_usage_and_sdk_shape(known):
    usage = RESULT["usage"] if known else None
    service = FakeService(error=CompletionInterrupted(usage, True))
    with TestClient(create_openai_app(service)) as client:
        response = client.post("/v1/responses", json={
            "model": "synthetic", "input": "A", "stream": True,
        })
    payload = next(json.loads(block.split("data: ", 1)[1])
                   for block in response.text.split("\n\n") if "event: response.failed" in block)
    parsed = ResponseFailedEvent.model_validate(payload)
    assert parsed.response.status == "failed" and "lower bound" in parsed.response.error.message
    assert parsed.response.usage is None
    if known:
        assert parsed.response.lohra_usage == {"status": "lower_bound", "observed": {
            "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "reasoning_tokens": 0}}
    else:
        assert parsed.response.lohra_usage == {"status": "unknown"}
    assert "response.completed" not in response.text
