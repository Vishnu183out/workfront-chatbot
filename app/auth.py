"""
Per-user login flow. Supports two consumers of this API:

1. This app's own built-in UI (static/index.html or public/index.html) -
   same-origin, uses a cookie. Unchanged behavior from before.

2. An external UI, on a completely different domain, calling this API
   directly (e.g. a separate UI team's frontend). Cookies don't work
   reliably cross-origin (browsers increasingly block third-party
   cookies outright), so this path hands back an opaque bearer token
   instead - the external UI stores it and sends it as
   `Authorization: Bearer <token>` on every request.

Either way, the token/cookie value is NOT the Adobe token itself - it's
just a random opaque lookup key into this user's entry in Redis (safe to
store client-side; grants nothing without the server-side Redis entry it
points to).

To use path 2: GET /auth/login?return_to=https://your-ui.example.com/page
- return_to's origin must be listed in ALLOWED_UI_ORIGINS (env var),
  otherwise the request is rejected outright - this is what prevents
  /auth/login from being usable as an open redirect to an arbitrary site.
- After login, the browser is redirected to
  {return_to}?wf_session_token=<token> (or &wf_session_token= if
  return_to already has query params). The external UI reads that once,
  stores it, and should strip it from the visible URL immediately after
  (matches how this app already handles ?login_error=... on its own UI).
"""
import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlencode, urlsplit, urlunsplit, parse_qsl

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import settings
from app.redis_client import get_json, set_json, delete
from app.token_manager import save_tokens

COOKIE_NAME = "wf_uid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
BEARER_TOKEN_PARAM = "wf_session_token"
_OAUTH_STATE_TTL = 600  # 10 minutes to complete the login


def _make_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def is_allowed_return_to(url: str) -> bool:
    if not settings.allowed_ui_origins:
        return False
    return _origin_of(url).rstrip("/") in settings.allowed_ui_origins


def _append_query_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query)
    query.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def get_user_id(request: Request) -> Optional[str]:
    """Resolves the calling user from EITHER an Authorization: Bearer
    header (external UI) or the cookie (this app's own built-in UI).
    Header takes priority if somehow both are present."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    return request.cookies.get(COOKIE_NAME)


async def start_login(redirect_uri: str, return_to: Optional[str] = None) -> RedirectResponse:
    if return_to and not is_allowed_return_to(return_to):
        raise ValueError(f"return_to origin not allowed: {_origin_of(return_to)}")

    verifier, challenge = _make_pkce_pair()
    state = secrets.token_urlsafe(24)

    # stash the PKCE verifier + exact redirect_uri + where to send the
    # user back afterward, keyed by state, so the callback can complete
    # correctly even on a different serverless instance than the one
    # that started it
    await set_json(
        f"oauth_pending:{state}",
        {"verifier": verifier, "redirect_uri": redirect_uri, "return_to": return_to},
        ex_seconds=_OAUTH_STATE_TTL,
    )

    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": settings.mcp_scope,
    }
    query = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    return RedirectResponse(f"{settings.mcp_authz_endpoint}?{query}")


@dataclass
class CallbackResult:
    response: RedirectResponse
    error: Optional[str] = None


async def complete_login(code: Optional[str], state: Optional[str], error: Optional[str]) -> CallbackResult:
    if error:
        return CallbackResult(response=RedirectResponse("/?login_error=" + error), error=error)

    pending = await get_json(f"oauth_pending:{state}") if state else None
    if not pending:
        return CallbackResult(
            response=RedirectResponse("/?login_error=expired_or_invalid_state"),
            error="expired_or_invalid_state",
        )
    await delete(f"oauth_pending:{state}")
    return_to = pending.get("return_to")

    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            settings.mcp_token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": pending["redirect_uri"],
                "client_id": settings.client_id,
                "code_verifier": pending["verifier"],
                **({"client_secret": settings.client_secret} if settings.client_secret else {}),
            },
        )
    if token_resp.status_code != 200:
        error_redirect = return_to or "/"
        return CallbackResult(
            response=RedirectResponse(_append_query_param(error_redirect, "login_error", "token_exchange_failed")),
            error="token_exchange_failed",
        )

    tokens = token_resp.json()
    user_id = secrets.token_urlsafe(24)
    await save_tokens(
        user_id,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_in=tokens.get("expires_in", 3600),
    )

    display_name = await _try_fetch_display_name(tokens["access_token"])
    if display_name:
        await set_json(f"wf_user_profile:{user_id}", {"name": display_name})

    if return_to:
        # External UI path: hand the token back via redirect, no cookie.
        target = _append_query_param(return_to, BEARER_TOKEN_PARAM, user_id)
        return CallbackResult(response=RedirectResponse(target))

    # This app's own built-in UI path: cookie, same as before.
    resp = RedirectResponse("/")
    resp.set_cookie(
        COOKIE_NAME,
        user_id,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return CallbackResult(response=resp)


async def _try_fetch_display_name(access_token: str) -> Optional[str]:
    """Best-effort only - login should succeed even if this fails."""
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {access_token}"}
        async with streamablehttp_client(settings.mcp_endpoint, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("insights_get_current_user", {})
                for block in result.content:
                    text = getattr(block, "text", None)
                    if text:
                        return text[:200]
    except Exception:
        return None
    return None


async def get_profile_name(user_id: str) -> Optional[str]:
    data = await get_json(f"wf_user_profile:{user_id}")
    return data.get("name") if data else None
