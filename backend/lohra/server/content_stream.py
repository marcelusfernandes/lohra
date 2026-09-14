"""Responses content framing, using the existing bounded delta delivery."""

from lohra.agent.stream_parts import OutputDelta
from lohra.server.responses import build_output_item_added_event, responses_sse


class ContentStream:
    """Track part identity and delivered lengths, not a second text buffer."""

    def __init__(self, response_id: str, counter) -> None:
        self.response_id, self.counter = response_id, counter
        self.keys: list[tuple] = []
        self.kinds: list[str] = []
        self.lengths: list[int] = []
        self.started = False

    def _event(self, kind: str, index: int, **values) -> str:
        event_type = "response." + kind
        return responses_sse(event_type, {
            "type": event_type, "sequence_number": self.counter.next(),
            "item_id": "msg_" + self.response_id, "output_index": 0,
            "content_index": index, **values,
        })

    def _add(self, key: tuple, kind: str):
        if not self.started:
            self.started = True
            yield build_output_item_added_event(response_id=self.response_id,
                                                sequence_number=self.counter.next())
        self.keys.append(key)
        self.kinds.append(kind)
        self.lengths.append(0)
        part = {"type": kind, "refusal" if kind == "refusal" else "text": ""}
        if kind == "output_text":
            part["annotations"] = []
        yield self._event("content_part.added", len(self.keys) - 1, part=part)

    def delta(self, text: str):
        kind = text.kind if isinstance(text, OutputDelta) else "output_text"
        key = text.part_key if isinstance(text, OutputDelta) else ("legacy", "text")
        if key not in self.keys:
            yield from self._add(key, kind)
        index = self.keys.index(key)
        if isinstance(text, OutputDelta) and not text:
            return  # structural start, not an invented empty text/refusal delta
        self.lengths[index] += len(text)
        yield self._event(kind + ".delta", index, delta=str(text),
                          **({"logprobs": []} if kind == "output_text" else {}))

    def finish(self, response: dict):
        if not response["output"]:
            return
        item = response["output"][0]
        for index, part in enumerate(item["content"]):
            kind = part["type"]
            if index == len(self.keys):
                yield from self._add(("final", index), kind)
            text = part["refusal" if kind == "refusal" else "text"]
            if len(text) > self.lengths[index]:
                yield self._event(kind + ".delta", index, delta=text[self.lengths[index]:],
                                  **({"logprobs": []} if kind == "output_text" else {}))
            yield self._event(kind + ".done", index,
                              **{"refusal" if kind == "refusal" else "text": text},
                              **({"logprobs": []} if kind == "output_text" else {}))
            yield self._event("content_part.done", index, part=part)
        event_type = "response.output_item.done"
        yield responses_sse(event_type, {"type": event_type, "sequence_number": self.counter.next(),
                                        "output_index": 0, "item": item})
