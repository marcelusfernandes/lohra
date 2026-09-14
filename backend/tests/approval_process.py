"""Separate interpreter control for #129; no actual shell/model call."""

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from lohra import cli
from lohra.agent.client import ModelClient

COMMAND = "sudo --version"


class Client(ModelClient):
    def __init__(self):
        self.responses = [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "terminal",
                        "input": {"command": COMMAND},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": None,
            },
            {
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": None,
            },
        ]

    def create(self, **kwargs):
        return self.responses.pop(0)

    def stream(self, **kwargs):
        return self.create(**kwargs)


def main(home: str, mode: str):
    commands, prompts = [], []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="fake", stderr="", returncode=0)

    def answer(*args):
        prompts.append(True)
        return "s"

    module = importlib.import_module("lohra.tools.terminal")
    env = {
        "LOHRA_HOME": home,
        "LOHRA_PROVIDER": "anthropic",
        "LOHRA_MODEL": "synthetic",
        "ANTHROPIC_API_KEY": "synthetic-no-network",
    }
    Path(home).mkdir(parents=True, exist_ok=True)
    os.chdir(Path(home).parent)
    with (
        patch.dict(os.environ, env, clear=True),
        patch("lohra.agent.client.build_client", return_value=Client()),
        patch("lohra.mcp.register_configured_mcp_servers"),
        patch("builtins.input", answer),
        patch.object(
            module, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
        ),
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        code = cli.run_chat(
            "synthetic", provider="anthropic", session="same-id", json_output=mode == "headless"
        )
    return {"code": code, "commands": commands, "prompts": prompts}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], sys.argv[2])))
