"""Direct fetch policy for HTTPX's environment/system proxy and CA sources.

The owned pinned transport cannot enforce a proxy's remote DNS. Determine the
route before DNS and refuse an active proxy, rather than silently bypassing it.
NO_PROXY matching uses public URL parsing and the documented mount priorities;
compatibility is tested differentially against HTTPX 0.27/0.28, including quirks.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import ssl
import urllib.request

import httpcore
import httpx

from lohra.web.safety import WebError


@dataclass(frozen=True)
class _Pattern:
    scheme: str
    host: str
    port: int | None
    proxied: bool

    @classmethod
    def parse(cls, text: str, proxied: bool):
        url = httpx.URL(text)
        return cls(
            "" if url.scheme == "all" else url.scheme,
            "" if url.host == "*" else url.host,
            url.port,
            proxied,
        )

    @property
    def priority(self):
        return (self.port is None, -len(self.host), -len(self.scheme))

    def matches(self, url: httpx.URL) -> bool:
        if self.scheme and self.scheme != url.scheme:
            return False
        if self.port is not None and self.port != url.port:
            return False
        if not self.host:
            return True
        if self.host.startswith("*."):
            return url.host.endswith(self.host[1:]) and url.host != self.host[2:]
        if self.host.startswith("*"):
            return url.host == self.host[1:] or url.host.endswith("." + self.host[1:])
        return url.host == self.host


def _no_proxy_pattern(value: str) -> str:
    if "://" in value:
        return value
    try:
        address = ipaddress.ip_address(value.split("/")[0])
    except ValueError:
        if value.lower() == "localhost":
            return f"all://{value}"
        return f"all://*{value}"
    return f"all://[{value}]" if address.version == 6 else f"all://{value}"


def require_direct(url: str) -> None:
    """Evaluate fresh env/system proxy settings, without dialing or logging them."""
    try:
        # Includes environment precedence/CGI behavior and OS proxy fallback.
        proxies = urllib.request.getproxies()
        exclusions = [item.strip() for item in proxies.get("no", "").split(",")]
        if "*" in exclusions:
            return  # HTTPX discards the whole proxy map in this case
        patterns = {}
        for scheme in ("http", "https", "all"):
            value = proxies.get(scheme)
            if value:
                httpx.Proxy(value if "://" in value else f"http://{value}")
                patterns[f"{scheme}://"] = True
        for item in exclusions:
            if item:
                patterns[_no_proxy_pattern(item)] = False
        routes = sorted(
            (_Pattern.parse(key, active) for key, active in patterns.items()),
            key=lambda pattern: pattern.priority,
        )
        target = httpx.URL(url)
        active = next((route.proxied for route in routes if route.matches(target)), False)
    except (ValueError, TypeError, OSError, httpx.InvalidURL):
        raise WebError(
            "invalid proxy/NO_PROXY configuration; review the environment/system proxy settings"
        ) from None
    if active:
        raise WebError(
            "web_fetch cannot use the configured proxy while pinning public addresses; "
            "configure an appropriate NO_PROXY exemption or remove that proxy configuration"
        )


def verified_context() -> ssl.SSLContext:
    """Preserve CA FILE > DIR > certifi default independently from proxy routing."""
    try:
        if os.environ.get("SSL_CERT_FILE"):
            return ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
        if os.environ.get("SSL_CERT_DIR"):
            directory = os.environ["SSL_CERT_DIR"]
            if not Path(directory).is_dir():
                raise OSError
            return ssl.create_default_context(capath=directory)
        return httpcore.default_ssl_context()
    except (OSError, ValueError):
        raise WebError(
            "invalid SSL_CERT_FILE/SSL_CERT_DIR configuration; configure a valid trusted CA"
        ) from None
