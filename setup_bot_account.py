"""
DEPRECATED - this script was for the earlier single-shared-"bot"-account
model. The app now uses per-user login (see app/auth.py, and
register_oauth_client.py for the one-time OAuth client setup instead).
Kept here for reference only; safe to delete.

---

Run this ONCE to authenticate the shared Workfront "bot" account and
persist its tokens for the server to use. This is the v1 auth model:
one login, shared by all users of the chatbot.

Opens a browser for you to log in as the bot account (or your own
account, for testing - see the conversation this project came from).
After login, tokens are saved to app.config.settings.token_store_path
(bot_tokens.json by default) and the server can start.

Usage:
    python setup_bot_account.py
"""
import base64
import hashlib
import secrets
import webbrowser

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.token_manager import token_manager

app = FastAPI()

STATE = {"code_verifier": None, "csrf_state": None, "client_id": None, "client_secret": None}


def make_pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


async def ensure_client():
    """Use WORKFRONT_CLIENT_ID from .env if set, else dynamically register one."""
    if settings.client_id:
        STATE["client_id"] = settings.client_id
        STATE["client_secret"] = settings.client_secret
        return
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            settings.mcp_registration_endpoint,
            json={
                "client_name": "Workfront Chatbot - Bot Account Setup",
                "redirect_uris": [settings.setup_redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
                "scope": settings.mcp_scope,
            },
        )
        r.raise_for_status()
        data = r.json()
        STATE["client_id"] = data["client_id"]
        STATE["client_secret"] = data.get("client_secret")
        print(f"\nDynamically registered client_id: {STATE['client_id']}")
        print(
            "Tip: save this as WORKFRONT_CLIENT_ID (and its secret as "
            "WORKFRONT_CLIENT_SECRET) in .env so future setup runs and the "
            "server reuse the same registered client.\n"
        )


@app.get("/auth/start")
async def auth_start():
    await ensure_client()
    verifier, challenge = make_pkce_pair()
    csrf_state = secrets.token_urlsafe(16)
    STATE["code_verifier"] = verifier
    STATE["csrf_state"] = csrf_state

    params = {
        "response_type": "code",
        "client_id": STATE["client_id"],
        "redirect_uri": settings.setup_redirect_uri,
        "state": csrf_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": settings.mcp_scope,
        "resource": settings.mcp_endpoint,
    }
    query = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    return RedirectResponse(f"{settings.mcp_authz_endpoint}?{query}")


@app.get("/auth/callback")
async def auth_callback(request: Request):
    code = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(f"<h3>Auth error: {error}</h3>", status_code=400)
    if returned_state != STATE["csrf_state"]:
        return HTMLResponse("<h3>State mismatch, aborting.</h3>", status_code=400)

    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            settings.mcp_token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.setup_redirect_uri,
                "client_id": STATE["client_id"],
                "code_verifier": STATE["code_verifier"],
                "resource": settings.mcp_endpoint,
                **({"client_secret": STATE["client_secret"]} if STATE["client_secret"] else {}),
            },
        )
    if token_resp.status_code != 200:
        return HTMLResponse(
            f"<h3>Token exchange failed: {token_resp.status_code}</h3>"
            f"<pre>{token_resp.text}</pre>",
            status_code=500,
        )

    tokens = token_resp.json()
    await token_manager.save_initial_tokens(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_in=tokens.get("expires_in", 3600),
    )
    return HTMLResponse(
        "<h3>Bot account authenticated and tokens saved.</h3>"
        "<p>Saved to Redis. You can close this window and start the server "
        "with <code>uvicorn app.main:app</code> (or it's already live if "
        "you're pointed at the same Redis database used by your Vercel "
        "deployment).</p>"
    )


if __name__ == "__main__":
    print("Opening browser to authenticate the shared Workfront bot account...")
    webbrowser.open("https://nontenantable-wearproof-easton.ngrok-free.dev/auth/start")
    uvicorn.run(app, host="0.0.0.0", port=8000)
