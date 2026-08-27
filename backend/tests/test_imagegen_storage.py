"""Tests for image_gen storage — decode base64 and persist to disk (pure-ish)."""

import base64
from pathlib import Path

import pytest

from lohra.imagegen.storage import ImageGenError, save_image_b64

_PNG = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
).decode("ascii")


def test_save_writes_a_png_and_returns_its_path(tmp_path):
    out = save_image_b64(_PNG, str(tmp_path))
    path = Path(out)
    assert path.is_file()
    assert path.suffix == ".png"
    assert path.parent == tmp_path
    # round-trips the exact bytes
    assert path.read_bytes() == base64.b64decode(_PNG)


def test_creates_the_output_directory_if_missing(tmp_path):
    nested = tmp_path / "a" / "b"
    out = save_image_b64(_PNG, str(nested))
    assert Path(out).is_file()
    assert nested.is_dir()


def test_each_save_gets_a_unique_filename(tmp_path):
    first = save_image_b64(_PNG, str(tmp_path))
    second = save_image_b64(_PNG, str(tmp_path))
    assert first != second


def test_custom_extension(tmp_path):
    out = save_image_b64(_PNG, str(tmp_path), ext="webp")
    assert Path(out).suffix == ".webp"


def test_bad_base64_raises_imagegen_error(tmp_path):
    with pytest.raises(ImageGenError):
        save_image_b64("not base64!!", str(tmp_path))
