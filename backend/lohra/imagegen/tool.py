"""The ``image_gen`` tool — intercepted, runner-bound (mirror of ``vision``).

Schema lives in the registry so the model sees it; execution binds to a session
``runner`` (an image-capable one-shot call) via the intercept dispatcher. The
tool validates the prompt, asks the runner to generate and persist the image(s),
and returns the saved file paths.
"""

from __future__ import annotations

from typing import Any, Callable

from lohra.imagegen.storage import save_image_b64
from lohra.tools.registry import registry, tool_error, tool_result

# (prompt, size, n) -> saved file paths. ``size`` may be None (provider default).
ImageGenRunner = Callable[[str, str | None, int], list[str]]

DEFAULT_IMAGE_MODEL = "gpt-image-1"

# gpt-image-1's accepted sizes; the model must not invent its own (e.g. 512x512
# is a DALL·E 2 size and 400s here).
VALID_SIZES = ("1024x1024", "1024x1536", "1536x1024", "auto")

# gpt-image-1 caps a single request at 10 images; each one is billed.
MAX_IMAGES = 10

IMAGE_GEN_GUIDANCE = (
    "Generate one or more images from a text 'prompt' and save them to disk; "
    "returns the file paths. Optional 'size' (one of '1024x1024', '1024x1536', "
    "'1536x1024', 'auto') and 'n' (how many, 1-10). Use this to create "
    "illustrations, mockups, or diagrams the user asks for."
)

_SCHEMA = {
    "description": IMAGE_GEN_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to draw"},
            "size": {
                "type": "string",
                "enum": list(VALID_SIZES),
                "description": "Image size (optional; defaults to the provider's default)",
            },
            "n": {"type": "integer", "description": "How many images, 1-10 (default 1)"},
        },
        "required": ["prompt"],
    },
}


def _coerce_count(value: Any) -> int:
    """A count clamped to 1..MAX_IMAGES, defaulting to 1 for missing/garbage input."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, MAX_IMAGES))


class ImageGenTool:
    """Runs one image-generation call against a session-bound runner."""

    def __init__(self, runner: ImageGenRunner) -> None:
        self._runner = runner

    def handle(self, args: dict[str, Any]) -> str:
        prompt = args.get("prompt")
        if not prompt or not str(prompt).strip():
            return tool_error("image_gen requires a non-empty 'prompt'")
        size = args.get("size") or None
        n = _coerce_count(args.get("n"))
        paths = self._runner(str(prompt), size, n)
        return tool_result(images=paths)


def register_image_gen_tool_schema() -> None:
    """Register the image_gen schema (execution is intercepted)."""
    registry.register(
        "image_gen", "imagegen", _SCHEMA, _intercepted_handler, override=True, emoji="🎨",
        author_time_only=True,
    )


def _intercepted_handler(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("the image_gen tool must be intercepted with a session runner")


def make_image_gen_runner(
    client: Any, out_dir: str, *, model: str = DEFAULT_IMAGE_MODEL
) -> ImageGenRunner:
    """A runner that generates image(s) and saves each to ``out_dir``."""

    def runner(prompt: str, size: str | None, n: int) -> list[str]:
        b64_images = client.generate_image(prompt=prompt, model=model, size=size, n=n)
        return [save_image_b64(b64, out_dir) for b64 in b64_images if b64]

    return runner
