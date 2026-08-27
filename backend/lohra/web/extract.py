"""Turn HTML into readable plain text (stdlib only).

Not a full readability engine — it drops obviously-non-content tags (script,
style, head, nav, footer, …), keeps the remaining text, collapses whitespace,
and caps the length so a long article can't blow the agent's context.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP_TAGS = frozenset(
    {"script", "style", "head", "noscript", "template", "nav", "footer", "svg"}
)
_MAX_CHARS = 20_000


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._parts.append(text)

    @property
    def text(self) -> str:
        return " ".join(self._parts)


def html_to_text(html: str, *, max_chars: int = _MAX_CHARS) -> str:
    """Extract readable text from ``html``, collapsed and capped at ``max_chars``."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    collapsed = re.sub(r"\s+", " ", parser.text).strip()
    return collapsed[:max_chars]
