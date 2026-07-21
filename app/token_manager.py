"""
Per-user OAuth token storage and refresh.

Replaces the old single shared "bot account" model: every team member
now authenticates with their own Adobe ID (via app/auth.py's login flow),
and their tokens are stored under their own key in Redis - so every MCP
tool call runs as that specific person, with their own Workfront
permissions, not a shared identity.
"""
import time
from dataclasses import dataclass, asdict
from typing import Optional

import httpx

from app.config import settings
from app.redis_client import get_json, set_json, delete

_KEY_PREFIX = "wf_user_tokens:"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float

    def is_expired(self, skew_seconds: int = 60) -> bool:
        return time.time() >= (self.expires_at - skew_seconds)


async def save_tokens(user_id: str, access_token: str, refresh_token: str, expires_in: int) -> None:
    tokens = TokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + expires_in,
    )
    await set_json(_KEY_PREFIX + user_id, asdict(tokens))


async def get_access_token(user_id: str) -> str:
    data = await get_json(_KEY_PREFIX + user_id)
    if data is None:
        raise RuntimeError("not_authenticated")

    tokens = TokenSet(**data)
    if tokens.is_expired():
        tokens = await _refresh(user_id, tokens.refresh_token)
    return tokens.access_token


async def _refresh(user_id: str, refresh_token: str) -> TokenSet:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            settings.mcp_token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.client_id,
                "resource": settings.mcp_endpoint,
                **({"client_secret": settings.client_secret} if settings.client_secret else {}),
            },
        )
    if resp.status_code != 200:
        # refresh token likely revoked/expired - the user needs to log in again
        await delete(_KEY_PREFIX + user_id)
        raise RuntimeError("reauth_required")

    data = resp.json()
    tokens = TokenSet(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", refresh_token),
        expires_at=time.time() + data.get("expires_in", 3600),
    )
    await set_json(_KEY_PREFIX + user_id, asdict(tokens))
    return tokens


async def forget_user(user_id: str) -> None:
    await delete(_KEY_PREFIX + user_id)
