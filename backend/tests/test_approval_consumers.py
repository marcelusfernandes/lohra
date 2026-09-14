"""#129 regression: approval belongs to a live consumer, never the whole process."""

import json

import pytest

from lohra import cli
from tests.approval_lab import lab as lab
from tests.approval_lab import COMMAND, OTHER, ScriptedClient, chat, text, tool


@pytest.mark.parametrize("choice", ["s", "o"])
@pytest.mark.parametrize("headless", [{"json_output": True}, {"no_input": True}])
@pytest.mark.parametrize("same_id", [False, True])
def test_cli_invocations_cannot_reuse_another_invocations_approval(lab, choice, headless, same_id):
    lab.answer = choice
    lab.clients = [ScriptedClient([tool(), text()]), ScriptedClient([tool(), text()])]
    chat(session="cli-A")
    chat(session="cli-A" if same_id else "cli-B", **headless)
    assert lab.commands == [COMMAND]
    assert lab.prompts == [True]


def test_exact_command_cache_does_not_grant_another_command_or_prompt_headless(lab):
    lab.clients = [ScriptedClient([tool(), text()]), ScriptedClient([tool(OTHER), text()])]
    chat(session="cli-A")
    chat(session="cli-B", json_output=True)
    assert lab.commands == [COMMAND] and lab.prompts == [True]


@pytest.mark.parametrize("seed", [None, "yolo", "once"])
def test_two_real_dashboard_sessions_never_inherit_cli_authority(lab, seed):
    lab.answer = "o"
    lab.clients = ([ScriptedClient([tool(), text()])] if seed else []) + [
        ScriptedClient([tool(), text(), tool(), text()]),
    ]
    if seed:
        chat(session="cli-seed", yolo=seed == "yolo")
    initial_commands, initial_prompts = list(lab.commands), list(lab.prompts)
    manager, app, _token = cli.build_dashboard_app(insecure=False)
    try:
        for sid in ("dashboard-A", "dashboard-B"):
            session = manager.create_session(session_id=sid)
            events = []
            session.submit("synthetic", events.append)
            results = [
                frame["params"]["payload"]["result"]
                for frame in events
                if frame["params"]["type"] == "tool.complete"
            ]
            assert len(results) == 1
            assert "not approved" in json.loads(results[0])["error"]
        assert lab.commands == initial_commands
        assert lab.prompts == initial_prompts
    finally:
        app.state.cleanup()


@pytest.mark.parametrize("choice,prompts", [("s", 1), ("o", 2)])
def test_same_live_cli_dispatch_preserves_session_and_once(lab, choice, prompts):
    lab.answer = choice
    lab.clients = [ScriptedClient([tool(), tool(), text()])]
    chat(session="same-live-invocation")
    assert lab.commands == [COMMAND, COMMAND] and len(lab.prompts) == prompts


@pytest.mark.parametrize("headless", [{"json_output": True}, {"no_input": True}])
def test_explicit_yolo_applies_only_to_its_headless_consumer(lab, headless):
    lab.clients = [ScriptedClient([tool(), text()]), ScriptedClient([tool(), text()])]
    chat(session="yolo", yolo=True, **headless)
    chat(session="deny", **headless)
    assert lab.commands == [COMMAND] and lab.prompts == []


def test_concurrent_headless_cli_cannot_replace_an_interactive_clis_callback(lab):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    interactive_ready, headless_ready = Event(), Event()

    class InteractiveClient(ScriptedClient):
        def create(self, **kwargs):
            interactive_ready.set()
            assert headless_ready.wait(5)
            return super().create(**kwargs)

    class HeadlessClient(ScriptedClient):
        def create(self, **kwargs):
            headless_ready.set()  # both CLIs have configured their own approvals now
            return super().create(**kwargs)

    lab.clients = [InteractiveClient([tool(), text()]), HeadlessClient([tool(), text()])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        interactive = pool.submit(chat, session="interactive")
        assert interactive_ready.wait(5)
        headless = pool.submit(chat, session="headless", json_output=True)
        headless.result(5)
        interactive.result(5)
    assert lab.commands == [COMMAND] and lab.prompts == [True]


def test_separate_cli_processes_never_share_approval_even_for_same_persisted_id(lab):
    import os
    from pathlib import Path
    import subprocess
    import sys

    helper = Path(__file__).with_name("approval_process.py")
    env = {**os.environ, "PYTHONPATH": str(helper.parents[1]), "PYTHONDONTWRITEBYTECODE": "1"}
    outcomes = []
    for mode in ("interactive", "headless"):
        result = subprocess.run(
            [sys.executable, str(helper), str(lab.root / "separate-home"), mode],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        outcomes.append(json.loads(result.stdout))
    assert outcomes == [
        {"code": 0, "commands": [COMMAND], "prompts": [True]},
        {"code": 0, "commands": [], "prompts": []},
    ]
