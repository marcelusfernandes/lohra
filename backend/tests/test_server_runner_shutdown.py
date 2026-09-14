"""The actual CLI/installed Uvicorn shutdown boundary, without sockets or SDK I/O."""

import asyncio
from types import SimpleNamespace

import pytest
import uvicorn

from lohra import cli
from lohra.server.app import create_openai_app
from lohra.server.stream_bridge import StreamLimits
from lohra.server.stream_workers import shutdown_openai_app
from tests.test_server_stream_workers import HeldService


@pytest.mark.parametrize("draining", [False, True])
def test_cli_runner_cancels_http_then_drains_before_shared_client_close(
    monkeypatch, tmp_path, caplog, draining,
):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("lohra.subscription.credentials.subscription_active", lambda _: False)
    service = HeldService()
    app = create_openai_app(service, stream_limits=StreamLimits(shutdown_seconds=0.01))
    closed, workers, options = [], [], []
    app.state.cleanup = lambda: closed.append("closed")
    monkeypatch.setattr(cli, "build_openai_server_app", lambda **kwargs: (app, None))

    def run(passed_app, **kwargs):
        assert passed_app is app
        options.append(kwargs)

        async def start():
            worker = app.state.stream_workers.start(service, {})
            workers.append(worker)
            assert await asyncio.to_thread(service.entered.wait, 1)
            if not draining:
                service.release.set()
                await asyncio.to_thread(worker.thread.join, 1)
                assert not worker.thread.is_alive()

        asyncio.run(start())

    monkeypatch.setattr(uvicorn, "run", run)
    try:
        assert cli.run_openai_server(host="synthetic", port=1, insecure=True) == 0
        assert options[0]["lifespan"] == "off"  # historical frozen-binary workaround
        assert options[0].get("timeout_graceful_shutdown") == 0
        if draining:
            assert closed == [] and service.interrupts == 1
            assert workers[0].thread.is_alive() and workers[0].bridge.receipt is None
            assert "shared provider client left open" in caplog.text
        else:
            assert closed == ["closed"] and app.state.stream_workers.snapshot() == ()
    finally:
        service.release.set()
        for worker in workers:
            worker.thread.join(2)
        assert workers and all(not worker.thread.is_alive() for worker in workers)
    if draining:
        # An embedding host can explicitly retry teardown after actual exit.
        shutdown_openai_app(app)
        assert closed == ["closed"]
    shutdown_openai_app(app)
    assert closed == ["closed"]  # repeated successful teardown does not close again


def test_installed_uvicorn_shutdown_cancels_an_active_task_before_lifespan():
    async def case():
        app = create_openai_app(None)
        server = uvicorn.Server(uvicorn.Config(
            app, lifespan="off", timeout_graceful_shutdown=0,
        ))
        server.servers = []  # no listener/socket is ever created
        entered, cancelled = asyncio.Event(), asyncio.Event()
        never = asyncio.Event()
        observations = []

        async def active_request():
            entered.set()
            try:
                await never.wait()
            finally:
                cancelled.set()

        async def lifespan_shutdown():
            observations.append(task.cancelling() > 0)

        server.lifespan = SimpleNamespace(shutdown=lifespan_shutdown)
        task = asyncio.create_task(active_request())
        server.server_state.tasks.add(task)
        task.add_done_callback(server.server_state.tasks.discard)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(server.shutdown(), 1)
            await asyncio.wait_for(cancelled.wait(), 1)
            assert observations == [True] and task.cancelled()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert task.done()

    asyncio.run(case())
