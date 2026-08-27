"""Tests for vision content helpers (pure: build OpenAI-style image parts)."""

import base64

import pytest

from lohra.vision.content import (
    VisionError,
    image_part_from_file,
    image_part_from_url,
    text_part,
)

# 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def test_text_part():
    assert text_part("hi") == {"type": "text", "text": "hi"}


def test_image_part_from_url():
    part = image_part_from_url("https://example.com/cat.png")
    assert part == {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}}


def test_image_part_from_file_builds_data_uri(tmp_path):
    f = tmp_path / "pixel.png"
    f.write_bytes(_PNG)
    part = image_part_from_file(str(f))
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # round-trips to the original bytes
    encoded = url.split(",", 1)[1]
    assert base64.b64decode(encoded) == _PNG


def test_image_part_from_file_guesses_media_type(tmp_path):
    f = tmp_path / "pixel.jpg"
    f.write_bytes(_PNG)
    part = image_part_from_file(str(f))
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_part_from_file_missing_raises(tmp_path):
    with pytest.raises(VisionError):
        image_part_from_file(str(tmp_path / "nope.png"))


def test_image_part_from_file_non_image_raises(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    with pytest.raises(VisionError, match="not an image"):
        image_part_from_file(str(f))
