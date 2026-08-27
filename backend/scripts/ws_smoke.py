#!/usr/bin/env python3
"""Manual smoke test for the dashboard WebSocket gateway.

Usage:
    # terminal 1 — start the gateway (insecure = no token, local only)
    ANTHROPIC_API_KEY=sk-ant-... lohra dashboard --insecure

    # terminal 2 — drive a real turn and print streamed events
    python scripts/ws_smoke.py "leia o arquivo README.md e resuma em uma frase"

With a token (secure mode), pass it:
    python scripts/ws_smoke.py --token <TOKEN> "olá"

This exercises the live path: session.create -> prompt.submit -> streamed
message.*/tool.* events -> message.complete. Requires the `websockets` package
(already a backend dependency) and a running `lohra dashboard`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def main() -> int:
    parser = argparse.ArgumentParser(description="Lohra gateway WS smoke test")
    parser.add_argument("prompt", help="prompt text to send")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9119)
    parser.add_argument("--token", help="WS token (omit in --insecure mode)")
    args = parser.parse_args()

    url = f"ws://{args.host}:{args.port}/api/ws"
    if args.token:
        url += f"?token={args.token}"

    async with websockets.connect(url) as ws:
        ready = json.loads(await ws.recv())
        print(f"<= {ready['params']['type']}")

        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.create", "params": {}}))
        created = json.loads(await ws.recv())
        session_id = created["result"]["session_id"]
        print(f"<= session created: {session_id}")
        await ws.recv()  # session.info

        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "prompt.submit",
                    "params": {"session_id": session_id, "text": args.prompt},
                }
            )
        )
        ack = json.loads(await ws.recv())
        print(f"<= ack: {ack['result']}")

        while True:
            frame = json.loads(await ws.recv())
            event = frame.get("params", {})
            etype = event.get("type")
            payload = event.get("payload", {})
            if etype == "message.delta":
                sys.stdout.write(payload.get("text", ""))
                sys.stdout.flush()
            elif etype == "tool.start":
                print(f"\n[tool.start] {payload.get('name')} {payload.get('args_text', '')}")
            elif etype == "tool.complete":
                print(f"[tool.complete] {payload.get('name')} -> {payload.get('result', '')[:120]}")
            elif etype == "message.complete":
                print(f"\n<= message.complete (status={payload.get('status')})")
                return 0
            elif etype == "error":
                print(f"\n<= error: {payload.get('message')}")
                return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
