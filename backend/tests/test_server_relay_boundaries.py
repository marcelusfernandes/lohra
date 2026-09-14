"""No-call, ASGI phase and bounded typed-delivery controls for #133."""

import asyncio
import json
import threading

import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.agent.stream_parts import OutputDelta
from lohra.providers import get_provider_profile
from lohra.server.app import create_openai_app
from lohra.server.service import CompletionService
from lohra.server.stream_bridge import StreamBridge, StreamLimits
from tests.relay_helpers import factory, isolated as isolated, relay, request
from tests.test_server_stream_lifetime import Request


@pytest.mark.parametrize("endpoint", ["chat", "responses"])
@pytest.mark.parametrize("streamed", [False, True])
def test_no_call_cannot_invent_success_or_usage(endpoint, streamed):
    calls = []

    def build():
        agent = factory([], calls=calls)()
        agent.max_iterations = 0
        return agent

    with relay([], agent_factory=build) as (sdk, local, _, requests, closes):
        if endpoint == "responses" and streamed:
            events = list(request(sdk, endpoint, streamed))
            assert [e.type for e in events] == ["response.created", "response.failed"]
            assert events[-1].response.usage is None
            assert events[-1].response.lohra_usage == {"status": "unknown"}
        else:
            with pytest.raises(openai.APIError) as caught:
                result = request(sdk, endpoint, streamed)
                if streamed:
                    list(result)
            assert caught.value.body["lohra_usage"] == {"status": "unknown"}
        assert calls == [] and len(requests) == len(closes) == 1


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("partial", [False, True])
def test_provider_failure_after_sse_prefix_has_one_error_terminal(path, partial):
    async def case():
        release = threading.Event()
        threads = []

        class Client(ModelClient):
            def create(self, **kwargs):
                raise AssertionError("streaming client required")

            def stream(self, *, on_text=None, **kwargs):
                threads.append(threading.current_thread())
                assert release.wait(5)
                if partial:
                    on_text("PART")
                raise RuntimeError("synthetic provider failure")

        service = CompletionService(lambda: Agent(model="synthetic", client=Client(),
                    provider=get_provider_profile("openai"), max_iterations=1))
        app = create_openai_app(service)
        incoming = Request(app, path)
        try:
            await asyncio.wait_for(incoming.initial.wait(), 2)
            assert incoming.messages[0]["status"] == 200
            prefix = incoming.messages[1]["body"]
            assert b"response.created" in prefix if path.endswith("responses") else b"assistant" in prefix
            assert not release.is_set()  # prefix does not wait for any model delta
            release.set()
            await asyncio.wait_for(incoming.task, 2)
            text = b"".join(m.get("body", b"") for m in incoming.messages).decode()
            frames = [json.loads(line[6:]) for line in text.splitlines()
                      if line.startswith("data: ") and line != "data: [DONE]"]
            if path.endswith("responses"):
                assert [f["type"] for f in frames if f["type"] in (
                    "response.completed", "response.incomplete", "response.failed")] == ["response.failed"]
                assert frames[-1]["response"]["usage"] is None
                assert frames[-1]["response"]["lohra_usage"] == {"status": "unknown"}
            else:
                assert len([f for f in frames if "error" in f]) == 1
                assert not any(c.get("finish_reason") for f in frames for c in f.get("choices", []))
                assert text.count("data: [DONE]") == 1
            assert ("PART" in text) == partial
        finally:
            release.set()
            await incoming.cleanup()
            await app.state.stream_workers.shutdown()
            for thread in threads:
                await asyncio.to_thread(thread.join, 2)
            assert threads and all(not thread.is_alive() for thread in threads)
            assert app.state.stream_workers.snapshot() == ()

    asyncio.run(case())


def test_bounded_utf8_split_preserves_native_part_identity_and_string_callbacks():
    async def case():
        bridge = StreamBridge(StreamLimits(pieces=1, bytes=4))
        value = OutputDelta("🙂🙂", "refusal", (2, 3))
        with pytest.raises(AttributeError):
            value.kind = "output_text"

        def produce():
            bridge.put(value)
            bridge.publish({}, None)

        producer = threading.Thread(target=produce)
        producer.start()
        try:
            pieces = [part async for part in bridge.deltas()]
            assert pieces == ["🙂", "🙂"] and "".join(pieces) == value
            assert all(part.kind == "refusal" and part.part_key == (2, 3) for part in pieces)
            assert bridge.buffered == (0, 0)
        finally:
            bridge.close(cancel=True)
            await asyncio.to_thread(producer.join, 2)
            assert not producer.is_alive()

    asyncio.run(case())
