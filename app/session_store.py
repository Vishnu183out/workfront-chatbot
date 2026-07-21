"""
Conversation state, now persisted in Redis (via app/redis_client.py)
instead of an in-memory dict.

This matters specifically for serverless hosting (Vercel): each request
can land on a completely fresh process with no memory of prior requests,
so state that needs to survive between messages in the same conversation
must live somewhere external. Redis with a TTL is the standard fix.
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from app.redis_client import get_json, set_json, delete

SESSION_TTL_SECONDS = 60 * 60 * 24  # 24h of inactivity before a session expires
_KEY_PREFIX = "wf_session:"


@dataclass
class SessionState:
    messages: list[dict] = field(default_factory=list)
    docs_loaded: bool = False
    pending_confirmation: Optional[dict[str, Any]] = None


async def get_state(session_id: str) -> SessionState:
    data = await get_json(_KEY_PREFIX + session_id)
    if data is None:
        return SessionState()
    return SessionState(**data)


async def save_state(session_id: str, state: SessionState) -> None:
    await set_json(_KEY_PREFIX + session_id, asdict(state), ex_seconds=SESSION_TTL_SECONDS)


async def reset_session(session_id: str) -> None:
    await delete(_KEY_PREFIX + session_id)
