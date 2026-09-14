"""Part notifications use the existing finite delivery/cancellation contract."""
import asyncio
import json
import threading

import pytest

from lohra.agent.agent import Agent
from lohra.agent.stream_abort import AbortedStream
from lohra.agent.stream_parts import OutputDelta, PartCallback
from lohra.server.app import create_openai_app
from lohra.server.service import CompletionService
from lohra.server.stream_bridge import StreamBridge, StreamLimits
from tests.relay_helpers import isolated as isolated
from tests.test_native_outcome_authority_repair import sdk_client
from tests.test_server_relay_empty_parts import PROFILE, native_events
from tests.test_server_stream_lifetime import Request


@pytest.mark.parametrize("publish_first", [False, True])
def test_empty_part_notifications_count_toward_capacity_and_cancel(publish_first):
    async def case():
        bridge = StreamBridge(StreamLimits(pieces=1, bytes=4))
        callback = PartCallback(bridge.put)
        callback.start_part("refusal", (0, 0))
        assert bridge.buffered == (1, 0)
        waiting = threading.Event()
        original_wait = bridge._condition.wait

        def wait(*args):
            waiting.set()
            return original_wait(*args)

        bridge._condition.wait = wait
        worker = threading.Thread(target=lambda: callback.start_part("output_text", (0, 1)))
        worker.start()
        try:
            assert await asyncio.to_thread(waiting.wait, 1)
            if publish_first:
                bridge.publish({"content": ""}, None)
                assert bridge.receipt is not None  # terminal never needs the full slot
            bridge.close(cancel=True)
            await asyncio.to_thread(worker.join, 1)
            assert not worker.is_alive() and bridge.buffered == (0, 0)
            callback.start_part("refusal", (0, 2))
            assert bridge.buffered == (0, 0)
            assert [piece async for piece in bridge.deltas()] == []
            assert bridge.cancellation.cancelled
        finally:
            bridge.close(cancel=True)
            await asyncio.to_thread(worker.join, 1)
            assert not worker.is_alive()

    asyncio.run(case())


def test_empty_part_identity_and_utf8_pieces_survive_the_same_queue():
    async def case():
        bridge = StreamBridge(StreamLimits(pieces=1, bytes=4))
        callback = PartCallback(bridge.put)

        def produce():
            callback.start_part("refusal", (0, 0))
            callback(OutputDelta("A🙂é", "refusal", (0, 0)))
            bridge.publish({}, None)

        worker = threading.Thread(target=produce)
        worker.start()
        try:
            pieces = [piece async for piece in bridge.deltas()]
            assert str(pieces[0]) == "" and "".join(pieces) == "A🙂é"
            assert all(isinstance(p, OutputDelta) and p.kind == "refusal" and p.part_key == (0, 0)
                       and len(p.encode()) <= 4 for p in pieces)
        finally:
            bridge.close(cancel=True)
            await asyncio.to_thread(worker.join, 1)
            assert not worker.is_alive()

    asyncio.run(asyncio.wait_for(case(), 3))


def test_abort_after_a_part_start_still_closes_the_upstream_stream():
    received = []
    with sdk_client(native_events([("output_text", ""), ("refusal", "NO")]), True) as (upstream, sent, closes):
        try:
            result = upstream.stream(model="synthetic", input="A", on_text=PartCallback(received.append),
                                     abort_check=lambda: bool(received))
        finally:
            assert len(sent) == len(closes) == 1
    assert isinstance(result, AbortedStream)
    assert len(received) == 1 and received[0] == "" and received[0].part_key == (0, 0)


def test_headers_part_start_and_text_are_sent_before_the_terminal(monkeypatch):
    async def case():
        payload = native_events([("output_text", ""), ("refusal", "VISIBLE")])
        held, release = threading.Event(), threading.Event()
        with sdk_client(payload, True) as (upstream, sent, closes):
            original = upstream._open_stream

            def open_stream(kwargs):
                stream = original(kwargs)

                def events():
                    try:
                        for event in stream:
                            if event.type == "response.completed":
                                held.set()
                                assert release.wait(2), "test terminal gate was not released"
                            yield event
                    finally:
                        stream.close()
                return events()

            monkeypatch.setattr(upstream, "_open_stream", open_stream)
            def factory():
                return Agent(model="synthetic", provider=PROFILE, client=upstream, max_iterations=1)

            app = create_openai_app(CompletionService(factory), stream_limits=StreamLimits(pieces=1, bytes=4))
            delivered = asyncio.Event()
            original_send = Request.send

            async def send(request, message):
                await original_send(request, message)
                for line in message.get("body", b"").splitlines():
                    if line.startswith(b"data: "):
                        payload = json.loads(line[6:])
                        if payload.get("type") == "response.refusal.delta" and payload.get("delta") == "E":
                            delivered.set()

            monkeypatch.setattr(Request, "send", send)
            request = Request(app, "/v1/responses")
            workers = ()
            try:
                await asyncio.wait_for(delivered.wait(), 2)
                assert await asyncio.to_thread(held.wait, 1)
                workers = app.state.stream_workers.snapshot()
                assert len(workers) == 1 and workers[0].bridge.receipt is None
                assert any(m["type"] == "http.response.start" for m in request.messages)
                body = b"".join(m.get("body", b"") for m in request.messages)
                assert b'response.created' in body and b'response.content_part.added' in body
                assert b'response.refusal.delta' in body and b'response.completed' not in body
                release.set()
                await asyncio.wait_for(request.task, 2)
            finally:
                release.set()
                await request.cleanup()
                await app.state.stream_workers.shutdown()
                for worker in workers:
                    await asyncio.to_thread(worker.thread.join, 1)
                    assert not worker.thread.is_alive()
                assert app.state.stream_workers.snapshot() == ()
                assert len(sent) == len(closes) == 1

    asyncio.run(case())
