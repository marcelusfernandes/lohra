"""Build OpenAI-style content parts for image input (pure).

A text part is ``{"type":"text","text":...}``; an image part is
``{"type":"image_url","image_url":{"url":...}}`` where the url is an ``http(s)``
link or a ``data:<media_type>;base64,<data>`` URI. Local files are read and
base64-encoded into a data URI. The anthropic transport converts these to its
own ``image`` blocks at the boundary.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

_DEFAULT_MEDIA_TYPE = "image/png"


class VisionError(ValueError):
    """A bad image reference (missing file, non-image, unreadable)."""


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_part_from_url(url: str) -> dict[str, Any]:
    """An image part referencing a remote (or data:) URL verbatim."""
    return {"type": "image_url", "image_url": {"url": url}}


def image_part_from_file(path: str) -> dict[str, Any]:
    """Read a local image and return an image part as a base64 data URI."""
    file = Path(path)
    if not file.is_file():
        raise VisionError(f"no such image file: {path}")
    media_type = mimetypes.guess_type(path)[0] or _DEFAULT_MEDIA_TYPE
    if not media_type.startswith("image/"):
        raise VisionError(f"{path} is not an image ({media_type})")
    try:
        data = base64.b64encode(file.read_bytes()).decode("ascii")
    except OSError as exc:
        raise VisionError(f"could not read {path}: {exc}") from exc
    return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
