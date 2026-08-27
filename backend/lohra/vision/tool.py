"""The ``vision_analyze`` tool — intercepted, runner-bound (spec §6, §9).

Schema lives in the registry so the model sees it; execution is bound to a
session ``runner`` (a vision-capable one-shot call) via the intercept dispatcher.
The tool builds a single user message (prompt + image part) and returns the
runner's text analysis.
"""

from __future__ import annotations

from typing import Any, Callable

from lohra.tools.registry import registry, tool_error, tool_result
from lohra.vision.content import VisionError, image_part_from_file, image_part_from_url, text_part

# (messages) -> analysis text. The messages carry one user turn with an image.
VisionRunner = Callable[[list[dict]], str]

DEFAULT_PROMPT = "Describe this image in detail."

# Vision answers are short descriptions, not long-form generation.
DEFAULT_MAX_TOKENS = 1024

VISION_GUIDANCE = (
    "Analyze an image and return a text description. Pass a local image 'path' "
    "or a remote 'url', and an optional 'prompt' for what to look for. Use this "
    "to read screenshots, diagrams, or photos the conversation refers to."
)

_SCHEMA = {
    "description": VISION_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Local image file path"},
            "url": {"type": "string", "description": "Image URL (http or data URI)"},
            "prompt": {"type": "string", "description": "What to look for (optional)"},
        },
    },
}


class VisionTool:
    """Runs one vision call against a session-bound runner."""

    def __init__(self, runner: VisionRunner) -> None:
        self._runner = runner

    def handle(self, args: dict[str, Any]) -> str:
        path, url = args.get("path"), args.get("url")
        try:
            if path:
                image = image_part_from_file(path)
            elif url:
                image = image_part_from_url(url)
            else:
                return tool_error("vision_analyze requires a 'path' or a 'url'")
        except VisionError as exc:
            return tool_error(str(exc))

        prompt = args.get("prompt") or DEFAULT_PROMPT
        messages = [{"role": "user", "content": [text_part(prompt), image]}]
        analysis = self._runner(messages)
        return tool_result(analysis=analysis)


def register_vision_tool_schema() -> None:
    """Register the vision_analyze schema (execution is intercepted)."""
    registry.register("vision_analyze", "vision", _SCHEMA, _intercepted_handler, override=True, emoji="👁️")


def _intercepted_handler(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("the vision_analyze tool must be intercepted with a session runner")


def make_vision_runner(client: Any, transport: Any, model: str) -> VisionRunner:
    """A runner that sends the image message to a vision-capable model, once."""

    def runner(messages: list[dict]) -> str:
        kwargs = transport.build_kwargs(model=model, messages=messages, max_tokens=DEFAULT_MAX_TOKENS)
        raw = client.create(**kwargs)
        return transport.normalize_response(raw).content or ""

    return runner
