"""
Per-user login flow. Each team member visiting the widget gets sent
through Adobe's OAuth login (same handshake validated earlier in
development), and a random opaque session ID is stored in a cookie in
their browser, mapping to their tokens in Redis.

The cookie value is NOT the Adobe token itself - it's just a lookup key
(safe to store client-side since it grants nothing on its own without
the server-side Redis entry it points to).
"""
import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import settings
from app.redis_client import get_json, set_json, delete
from app.token_manager import save_tokens

COOKIE_NAME = "wf_uid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_OAUTH_STATE_TTL = 600  # 10 minutes to complete the login


def _make_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def get_user_id(request: Request) -> Optional[str]:
    return request.cookies.get(COOKIE_NAME)


async def start_login(redirect_uri: str) -> RedirectResponse:
    verifier, challenge = _make_pkce_pair()
    state = secrets.token_urlsafe(24)

    # stash the PKCE verifier + the exact redirect_uri used, keyed by
    # state, so the callback can complete the exchange correctly even on
    # a different serverless instance than the one that started it
    await set_json(
        f"oauth_pending:{state}",
        {"verifier": verifier, "redirect_uri": redirect_uri},
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
        "resource": settings.mcp_endpoint,
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

    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            settings.mcp_token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": pending["redirect_uri"],
                "client_id": settings.client_id,
                "code_verifier": pending["verifier"],
                "resource": settings.mcp_endpoint,
                **({"client_secret": settings.client_secret} if settings.client_secret else {}),
            },
        )
    if token_resp.status_code != 200:
        return CallbackResult(
            response=RedirectResponse("/?login_error=token_exchange_failed"),
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
