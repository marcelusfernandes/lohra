"""Small native-authority boundary shared by normalizers and stream failures.

Only protocol tokens enter diagnostics: no payload, opaque message, headers,
container repr, or unbounded provider string. Unknown tokens remain diagnostic;
only each transport's explicit vocabulary grants authority.
"""
from dataclasses import replace
import re
from typing import NoReturn

from lohra.agent.types import NativeOutcome, Usage
from lohra.providers.errors import ProviderCallFailed

_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def native_token(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else "<invalid>"


def reject_native(native: NativeOutcome, usage: Usage | None, rejection: str,
                  *, message: str | None = None) -> NoReturn:
    """Carry a reported measurement once through the loop's existing catch."""
    raise ProviderCallFailed(
        message or f"{native.api_mode} native outcome rejected: {rejection}",
        code=native.error_code, native_outcome=replace(native, rejection=rejection), usage=usage,
    )


def reason_finish(reason: object, mapping: dict[str, str], native: NativeOutcome,
                  usage: Usage | None, *, has_calls: bool) -> str:
    if not isinstance(reason, str) or reason not in mapping:
        reject_native(native, usage, "invalid_reason")
    finish = mapping[reason]
    if has_calls and finish != "tool_calls":
        reject_native(native, usage, "calls_without_authority")
    return finish
