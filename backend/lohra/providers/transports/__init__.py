"""Transports — protocol adapters keyed by api_mode.

Importing this package registers the built-in transports.
"""

from lohra.providers.transports.anthropic_messages import AnthropicMessagesTransport
from lohra.providers.transports.base import Transport, get_transport, register_transport
from lohra.providers.transports.chat_completions import ChatCompletionsTransport
from lohra.providers.transports.responses import ResponsesTransport

register_transport(AnthropicMessagesTransport())
register_transport(ChatCompletionsTransport())
register_transport(ResponsesTransport())

__all__ = [
    "AnthropicMessagesTransport",
    "ChatCompletionsTransport",
    "ResponsesTransport",
    "Transport",
    "get_transport",
    "register_transport",
]
