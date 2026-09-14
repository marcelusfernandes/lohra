"""Missing #116 ASGI cases; existing disconnect/burst probes stay unchanged."""

import asyncio
import json
import threading

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient, assemble_responses_stream
from lohra.server.app import create_openai_app
from lohra.server.service import CompletionService
from lohra.subscription.provider import CODEX_PROVIDER

PATHS = ("/v1/chat/completions", "/v1/responses")


async def _case(path, version, phase):
    loop = asyncio.get_running_loop()
    entered, constructed, streamed, initial = (asyncio.Event() for _ in range(4))
    disconnect, interrupted = asyncio.Event(), asyncio.Event()
    release_factory, release_provider = threading.Event(), threading.Event()
    agents, workers, messages = [], [], []

    class Source:
        close_calls = 0

        def __iter__(self):
            loop.call_soon_threadsafe(streamed.set)
            assert release_provider.wait(5), "provider cleanup was not released"
            yield {"type": "response.output_text.delta", "delta": "x"}
            yield {"type": "response.completed", "response": {"status": "completed",
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "x"}]}],
                "usage": {"input_tokens": 5, "output_tokens": 1}}}

        def close(self):
            self.close_calls += 1

    source = Source()

    class SharedClient(ModelClient):
        closed = 0

        def create(self, **kwargs):
            raise AssertionError("non-streaming provider call")

        def stream(self, *, on_text=None, abort_check=None, **kwargs):
            return assemble_responses_stream(source, on_text=on_text, abort_check=abort_check)

        def close(self):
            self.closed += 1

    class TrackedAgent(Agent):
        interrupts = 0

        def request_interrupt(self):
            self.interrupts += 1
            super().request_interrupt()
            loop.call_soon_threadsafe(interrupted.set)

    client = SharedClient()

    def factory():
        workers.append(threading.current_thread())
        if phase == "before-construction":
            loop.call_soon_threadsafe(entered.set)
            assert release_factory.wait(5), "factory cleanup was not released"
        agent = TrackedAgent(model="synthetic", provider=CODEX_PROVIDER, client=client)
        agents.append(agent)
        loop.call_soon_threadsafe(constructed.set)
        if phase == "constructed-before-return":
            loop.call_soon_threadsafe(entered.set)
            assert release_factory.wait(5), "binding cleanup was not released"
        return agent

    app = create_openai_app(CompletionService(factory))
    draining = asyncio.Event()
    drain = app.state.stream_workers.drain

    async def observe_drain(worker):
        draining.set()
        await drain(worker)

    app.state.stream_workers.drain = observe_drain
    payload = {"model": "synthetic", "stream": True}
    payload.update({"input": "A"} if path.endswith("responses") else
                   {"messages": [{"role": "user", "content": "A"}]})
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start" and phase == "send-start-failure":
            await asyncio.wait_for(streamed.wait(), 2)
            raise OSError("synthetic send failure")
        if message["type"] == "http.response.body":
            initial.set()
            if phase == "send-failure":
                # Prove the real Agent is inside its stream before failing send.
                await asyncio.wait_for(streamed.wait(), 2)
                raise OSError("synthetic send failure")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": version},
        "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
        "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("synthetic", 1), "server": ("synthetic", 2)}
    task = asyncio.create_task(app(scope, receive, send))
    waits = []
    try:
        if phase in ("send-failure", "send-start-failure"):
            try:
                await asyncio.wait_for(asyncio.shield(task), 2)
            except OSError:
                pass
            except Exception as exc:
                # Framework may wrap the send error; unrelated failures are not accepted.
                assert "synthetic send failure" in str(exc) or type(exc).__name__ == "ClientDisconnect"
            assert task.done() and streamed.is_set() and agents
            assert agents[0].interrupts == 1, {"interrupts": agents[0].interrupts,
                "worker_alive": workers[0].is_alive(), "status": messages[0]}
        elif phase in ("disconnect", "task-cancel", "task-cancel-twice"):
            await asyncio.wait_for(asyncio.gather(streamed.wait(), initial.wait()), 2)
            if phase == "disconnect":
                disconnect.set()
            else:
                task.cancel()
            if phase == "task-cancel-twice":
                await asyncio.wait_for(draining.wait(), 1)
                assert not task.done() and workers[0].is_alive()
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            assert done and agents[0].interrupts == 1
            assert workers[0].is_alive()  # HTTP exit is not a claim that I/O died
            retained = app.state.stream_workers.snapshot()
            assert len(retained) == 1 and retained[0].bridge.receipt is None
            assert retained[0].bridge.buffered == (0, 0)
        else:
            await asyncio.wait_for(asyncio.gather(entered.wait(), initial.wait()), 2)
            disconnect.set()
            # A bounded observation, proposed as a 1s HTTP oracle at claim; no sleep.
            done, _ = await asyncio.wait({task}, timeout=1)
            http_done = bool(done)
            release_factory.set()
            await asyncio.wait_for(constructed.wait(), 2)
            waits = [asyncio.create_task(interrupted.wait()), asyncio.create_task(streamed.wait())]
            await asyncio.wait(waits, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            facts = {"http_done_while_factory_held": http_done,
                "interrupts": agents[0].interrupts, "stream_started": streamed.is_set()}
            assert http_done and agents[0].interrupts == 1, facts
    finally:
        release_factory.set()
        release_provider.set()
        for waiter in waits:
            waiter.cancel()
        if waits:
            await asyncio.gather(*waits, return_exceptions=True)
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), 3)
        except (asyncio.CancelledError, OSError):
            pass
        except Exception as exc:
            assert "synthetic send failure" in str(exc) or type(exc).__name__ == "ClientDisconnect"
        for worker in workers:
            await asyncio.to_thread(worker.join, 2)
        assert workers and all(not worker.is_alive() for worker in workers)
        assert client.closed == 0
        assert source.close_calls == int(streamed.is_set())


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("version", ["2.0", "2.4"])
def test_send_failure_interrupts_the_request_agent(tmp_path, monkeypatch, path, version):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    asyncio.run(_case(path, version, "send-failure"))


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("phase", ["before-construction", "constructed-before-return"])
def test_disconnect_remains_sticky_until_agent_binding(tmp_path, monkeypatch, path, phase):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    asyncio.run(_case(path, "2.4", phase))


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("version", ["2.0", "2.4"])
@pytest.mark.parametrize("phase", ["disconnect", "task-cancel", "task-cancel-twice", "send-start-failure"])
def test_silent_stream_http_lifetime_is_bounded(tmp_path, monkeypatch, path, version, phase):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    asyncio.run(_case(path, version, phase))
