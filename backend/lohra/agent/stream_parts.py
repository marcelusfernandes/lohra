"""String-compatible callback deltas with optional native content identity."""

from __future__ import annotations

from collections.abc import Callable


class StreamedChatMessage(dict):
    """Local assembler metadata, not a field an upstream JSON body can supply."""

    def __init__(self, *args, part_order: tuple[str, ...], **kwargs):
        super().__init__(*args, **kwargs)
        self.part_order = part_order


class OutputDelta(str):
    """Legacy callbacks still receive a string; relay serializers retain its kind.

    The key is local to one provider stream. It is never a raw payload, replay
    field or model input, and slicing for the bounded queue retains it.
    """

    __slots__ = ("_kind", "_part_key")

    def __new__(cls, text: str, kind: str, part_key: tuple):
        value = super().__new__(cls, text)
        object.__setattr__(value, "_kind", kind)
        object.__setattr__(value, "_part_key", part_key)
        return value

    def __setattr__(self, name, value):
        raise AttributeError("OutputDelta is immutable")

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def part_key(self) -> tuple:
        return self._part_key

    def piece(self, start: int, stop: int) -> OutputDelta:
        return OutputDelta(self[start:stop], self.kind, self.part_key)


class PartCallback:
    """Opt in to structural part starts without changing legacy text callbacks.

    Empty typed strings use the same bounded delivery as text. They reserve
    identity, not text, and need no additional payload buffer or side channel.
    """

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def __call__(self, text: str) -> None:
        self._callback(text)

    def start_part(self, kind: str, key: tuple) -> None:
        if kind in ("output_text", "refusal"):
            self._callback(OutputDelta("", kind, key))
