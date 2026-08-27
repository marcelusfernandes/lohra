"""First-party OAuth device flow against auth.openai.com (Fase 10, B5).

Mirrors the Codex/opencode headless device flow (verifiable: openai/codex +
sst/opencode): request a user code, the user enters it at a URL, poll for the
authorization code, exchange it for tokens. No localhost server (works over SSH).
HTTP is injectable so the mechanics are testable offline. ⚠️ Uses the Codex OAuth
client_id — opt-in, ToS-gray (see manage.TOS_WARNING).
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from lohra.subscription.constants import CODEX_CLIENT_ID
from lohra.subscription.token_store import OAuthTokens

_ISSUER = "https://auth.openai.com"
USERCODE_URL = f"{_ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{_ISSUER}/api/accounts/deviceauth/token"
TOKEN_URL = f"{_ISSUER}/oauth/token"
DEVICE_VERIFY_URL = f"{_ISSUER}/codex/device"
DEVICE_REDIRECT = f"{_ISSUER}/deviceauth/callback"

_POLL_PENDING = (403, 404)  # keep polling
_MAX_POLL_SECONDS = 600  # give up after 10 min


class OAuthError(Exception):
    """Login failed (never carries a token)."""


@dataclass(frozen=True)
class DeviceCode:
    device_auth_id: str
    user_code: str
    interval: int
    verify_url: str = DEVICE_VERIFY_URL


# Injectable HTTP: (url, json_body) -> (status_code, parsed_json_or_None)
HttpPost = Callable[[str, dict], "tuple[int, Any]"]
# Injectable sleeper (seconds) — patched out in tests.
Sleeper = Callable[[float], None]


def start_device_login(post: HttpPost) -> DeviceCode:
    """Request a device user code. The caller shows verify_url + user_code."""
    status, body = post(USERCODE_URL, {"client_id": CODEX_CLIENT_ID})
    if status != 200 or not isinstance(body, dict) or not body.get("user_code"):
        raise OAuthError(f"could not start device login (status {status})")
    try:
        interval = max(int(body.get("interval") or 5), 1)
    except (TypeError, ValueError):
        interval = 5
    return DeviceCode(
        device_auth_id=str(body.get("device_auth_id") or ""),
        user_code=str(body["user_code"]),
        interval=interval,
    )


def poll_for_tokens(
    device: DeviceCode, post: HttpPost, *, sleep: Sleeper = time.sleep, now: Callable[[], float] = time.monotonic
) -> OAuthTokens:
    """Poll until the user authorizes, then exchange the code for tokens.
    Raises OAuthError on failure/timeout (token-free)."""
    deadline = now() + _MAX_POLL_SECONDS
    while now() < deadline:
        status, body = post(
            DEVICE_TOKEN_URL,
            {"device_auth_id": device.device_auth_id, "user_code": device.user_code},
        )
        if status == 200 and isinstance(body, dict) and body.get("authorization_code"):
            return _exchange(body["authorization_code"], body.get("code_verifier", ""), post)
        if status not in _POLL_PENDING:
            raise OAuthError(f"device authorization failed (status {status})")
        sleep(device.interval)
    raise OAuthError("device login timed out — please try again")


def _exchange(code: str, code_verifier: str, post: HttpPost) -> OAuthTokens:
    status, body = post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DEVICE_REDIRECT,
            "client_id": CODEX_CLIENT_ID,
            "code_verifier": code_verifier,
        },
    )
    if status != 200 or not isinstance(body, dict):
        raise OAuthError(f"token exchange failed (status {status})")
    return tokens_from_response(body)


def tokens_from_response(body: dict) -> OAuthTokens:
    """Build OAuthTokens from a token endpoint response (also used by refresh)."""
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError("token response had no access_token")
    expires_in = body.get("expires_in")
    expires_at = time.time() + (float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0)
    return OAuthTokens(
        access_token=access,
        refresh_token=body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else "",
        account_id=_account_id(body.get("id_token")) or _account_id(access),
        expires_at=expires_at,
    )


def refresh_tokens(refresh_token: str, post: HttpPost) -> OAuthTokens:
    """Refresh our token family. The server ROTATES the refresh token; we keep the
    new one (or the old, if the response didn't rotate) so the next refresh works."""
    if not refresh_token:
        raise OAuthError("no refresh token available")
    status, body = post(
        TOKEN_URL,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CODEX_CLIENT_ID},
    )
    if status != 200 or not isinstance(body, dict):
        raise OAuthError(f"token refresh failed (status {status})")
    result = tokens_from_response(body)
    if not result.refresh_token:  # server didn't rotate -> keep the existing one
        result = replace(result, refresh_token=refresh_token)
    return result


def default_post(url: str, json_body: dict) -> "tuple[int, Any]":
    """Real HTTP poster (httpx). Form-encodes the OAuth token endpoint, JSON the
    device endpoints. A User-Agent is REQUIRED — the auth endpoints reject clients
    without one (opencode/Codex both send it). Imported lazily so tests need no net."""
    import httpx

    from lohra import __version__

    headers = {"User-Agent": f"lohra/{__version__}"}
    if url == TOKEN_URL:
        resp = httpx.post(url, data=json_body, headers=headers, timeout=30.0)
    else:
        resp = httpx.post(url, json=json_body, headers=headers, timeout=30.0)
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    return resp.status_code, parsed


def _account_id(token: Any) -> str | None:
    """The ChatGPT account id from a JWT claim (no signature verification — we only
    read a claim). Mirrors opencode: chatgpt_account_id / nested auth / org[0].id."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    nested = claims.get("https://api.openai.com/auth")
    orgs = claims.get("organizations")
    return (
        claims.get("chatgpt_account_id")
        or (nested.get("chatgpt_account_id") if isinstance(nested, dict) else None)
        or (orgs[0].get("id") if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict) else None)
    )
