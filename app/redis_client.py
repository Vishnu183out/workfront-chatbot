"""
Tiny async client for Upstash Redis's REST API. Used instead of a
persistent Redis connection because Vercel serverless functions are
short-lived - a REST call per operation works everywhere (local dev,
Vercel, anywhere else) with no connection pooling to manage.

Create a free database at https://console.upstash.com, then set
UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (both shown on the
database's detail page) - same two env vars work locally and on Vercel.
"""
import json
import os
from typing import Any, Optional

import httpx

_UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
_UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")


async def _command(*args: str) -> Any:
    if not _UPSTASH_URL or not _UPSTASH_TOKEN:
        raise RuntimeError(
            "UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are not set. "
            "Create a free database at https://console.upstash.com and add "
            "both to your .env (locally) or Vercel project env vars."
        )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _UPSTASH_URL,
            headers={"Authorization": f"Bearer {_UPSTASH_TOKEN}"},
            json=list(args),
        )
    resp.raise_for_status()
    return resp.json().get("result")


async def get_json(key: str) -> Optional[Any]:
    raw = await _command("GET", key)
    return json.loads(raw) if raw else None


async def set_json(key: str, value: Any, ex_seconds: Optional[int] = None) -> None:
    payload = json.dumps(value)
    if ex_seconds:
        await _command("SET", key, payload, "EX", str(ex_seconds))
    else:
        await _command("SET", key, payload)


async def delete(key: str) -> None:
    await _command("DEL", key)
