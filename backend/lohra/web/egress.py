"""Operator host restrictions shared by the workflow gate and every fetch hop.

The dispatcher/registry preserve the argument object. Only its internal type
carries authority: JSON keys, including ``allowed_hosts``, grant nothing.
"""

from typing import Any
from urllib.parse import urlparse

from lohra.web.safety import WebError


class RestrictedFetchArgs(dict[str, Any]):
    """A fresh argument mapping with an out-of-band, immutable host tuple.

    Constructed by the sandbox, never from tool JSON. Keeping the policy out of
    mapping entries also keeps it out of argument serialization and audit.
    """

    def __init__(self, args: dict[str, Any], allowed_hosts: tuple[str, ...]):
        super().__init__(args)
        self.allowed_hosts = tuple(allowed_hosts)


def _host(raw_url: Any) -> str:
    if not isinstance(raw_url, str):
        return ""
    try:
        return (urlparse(raw_url).hostname or "").lower()
    except ValueError:
        return ""  # malformed authority cannot match an operator grant


def host_allowed(raw_url: Any, allowed_hosts: tuple[str, ...]) -> bool:
    """Exact, case-insensitive host matching; no wildcard/subdomain grants."""
    host = _host(raw_url)
    return bool(host) and host in {h.lower() for h in allowed_hosts}


class EgressDenied(WebError):
    """A typed policy refusal; only the public error text names the host/hop."""

    def __init__(self, url: str, hop: int):
        self.reason = "egress_redirect_not_allowed" if hop else "egress_not_allowed"
        where = f"redirect hop {hop}" if hop else "initial URL"
        super().__init__(
            f"host {_host(url) or '(none)'!r} at {where} is not in the workflow "
            "egress allowlist (sandbox denied); an operator must allow this host "
            "in workflow_policy.json egress_allow"
        )


def validate_egress_url(url: str, allowed_hosts: tuple[str, ...] | None, *, hop: int) -> None:
    """No DNS: None is unrestricted; an empty tuple denies every destination."""
    if allowed_hosts is not None and not host_allowed(url, allowed_hosts):
        raise EgressDenied(url, hop)
