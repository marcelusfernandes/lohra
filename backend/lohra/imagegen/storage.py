"""Decode base64 image data and persist it to disk (one file per image)."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from uuid import uuid4

_DEFAULT_EXT = "png"


class ImageGenError(ValueError):
    """A bad image payload (undecodable base64) or an unwritable destination."""


def save_image_b64(b64_data: str, out_dir: str, *, ext: str = _DEFAULT_EXT) -> str:
    """Decode ``b64_data`` and write it under ``out_dir`` with a unique name.

    Returns the absolute path of the written file. Creates ``out_dir`` (and any
    parents) if it does not exist.
    """
    try:
        data = base64.b64decode(b64_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenError(f"image data is not valid base64: {exc}") from exc

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid4().hex}.{ext}"
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise ImageGenError(f"could not write image to {path}: {exc}") from exc
    return str(path)
