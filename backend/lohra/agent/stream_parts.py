"""String-compatible callback deltas with optional native content identity."""

from __future__ import annotations


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
