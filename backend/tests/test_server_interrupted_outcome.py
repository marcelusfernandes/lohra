"""#116 service boundary: real loop/abort, synthetic SDK, no background workers."""

from dataclasses import asdict
import asyncio
import threading

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.server import service as service_module
from lohra.server.format import UpstreamError
from tests.test_stream_abort import EndlessStream, StreamingClient, _openai_text
from tests.test_server_service import _factory, _messages, _text
from lohra.server.stream_bridge import StreamBridge, StreamLimits
from lohra.server.stream_workers import StreamWorkers


@pytest.mark.parametrize("phase", ["before-run", "first-stream", "known-floor"])
def test_interrupted_service_refuses_success_and_keeps_loop_evidence(monkeypatch, tmp_path, phase):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    agents, raw_results, tool_calls = [], [], []
    stream = EndlessStream(on_chunk=lambda n: agents[0].request_interrupt())
    first = {
        "choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "tc1", "type": "function", "function": {
                "name": "synthetic_runtime", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
    }

    class SharedClient(StreamingClient):
        closed = 0

        def close(self):
            self.closed += 1

    client = SharedClient(([first] if phase == "known-floor" else []) + [stream])

    def factory():
        agent = Agent(model="synthetic", provider=get_provider_profile("openrouter"),
            client=client, tool_definitions=({"type": "function", "function": {
                "name": "synthetic_runtime"}},),
            tool_dispatch=lambda name, args: tool_calls.append(name) or "{}")
        agents.append(agent)
        if phase == "before-run" and len(agents) == 1:
            agent.request_interrupt()
        return agent

    original = service_module.run_conversation

    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        raw_results.append(result)
        return result

    monkeypatch.setattr(service_module, "run_conversation", record)
    service = service_module.CompletionService(factory)
    returned, error = None, None
    try:
        returned = service.run(model="synthetic", messages=[{"role": "user", "content": "A"}],
                               on_delta=lambda _: None)
    except UpstreamError as exc:
        error = exc
    raw = raw_results[0]
    assert raw["interrupted"] and not raw["completed"] and raw["final_response"] is None
    assert raw["usage_uncertain"] == (phase != "before-run")
    if phase == "known-floor":
        assert raw["usage_total"].input_tokens == 11 and raw["usage_total"].output_tokens == 5
        assert tool_calls == ["synthetic_runtime"]
    else:
        assert raw["usage_total"] is None and tool_calls == []
    assert stream.closed == int(phase != "before-run")

    # Another real request uses the identical client after interruption.
    client._script = [_openai_text("NEXT", usage={"prompt_tokens": 2, "completion_tokens": 1})]
    next_result = service.run(model="synthetic", messages=[{"role": "user", "content": "B"}],
                              on_delta=lambda _: None)
    assert next_result["content"] == "NEXT" and client.closed == 0
    assert len(agents) == 2 and agents[0] is not agents[1] and agents[0].client is agents[1].client
    evidence = {"phase": phase, "returned": returned, "interrupted": raw["interrupted"],
        "usage_uncertain": raw["usage_uncertain"],
        "known_usage": asdict(raw["usage_total"]) if raw["usage_total"] else None,
        "stream_close_calls": stream.closed, "shared_client_close_calls": client.closed,
        "next_request": next_result["content"]}
    assert error is not None and returned is None, evidence
    assert isinstance(error, service_module.CompletionInterrupted)
    assert error.usage_uncertain == raw["usage_uncertain"]
    if phase == "known-floor":
        assert error.usage["prompt_tokens"] == 11 and error.usage["completion_tokens"] == 5
    else:
        assert error.usage is None  # no ordinary absent-usage estimate on interruption


@pytest.mark.parametrize("reported", [False, True])
def test_cancel_after_service_return_preserves_only_observed_usage(monkeypatch, tmp_path, reported):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))

    async def case():
        loop = asyncio.get_running_loop()
        returned, release = asyncio.Event(), threading.Event()
        response = _text("abcd")
        if reported:
            response["usage"] = {"input_tokens": 11, "output_tokens": 5}
        service = service_module.CompletionService(_factory([response]))
        original = StreamBridge.publish

        def hold_publish(self, result, error):
            loop.call_soon_threadsafe(returned.set)
            assert release.wait(5)
            original(self, result, error)

        monkeypatch.setattr(StreamBridge, "publish", hold_publish)
        worker = StreamWorkers(StreamLimits()).start(service, {"model": "m", "messages": _messages()})
        try:
            await asyncio.wait_for(returned.wait(), 2)
            worker.bridge.close(cancel=True)
            assert worker.bridge.receipt is None
            release.set()
            await asyncio.to_thread(worker.thread.join, 2)
            assert not worker.thread.is_alive()
            receipt = worker.bridge.receipt
            assert receipt.disposition == "cancelled"
            assert receipt.usage_observed == reported
            assert "usage_observed" not in receipt.result
            if reported:
                assert receipt.usage["prompt_tokens"] == 11 and receipt.usage["completion_tokens"] == 5
            else:
                assert receipt.usage is None  # ordinary success estimate is not an observed bill
        finally:
            release.set()
            await asyncio.to_thread(worker.thread.join, 2)
            assert not worker.thread.is_alive()

    asyncio.run(case())
