"""
FastAPI entrypoint. This is what the UI team's widget calls.

Run with:
    uvicorn app.main:app --reload

Per-user auth model: each visitor logs into their own Adobe ID via
/auth/login -> Adobe -> /auth/callback. This app's own built-in UI gets
an httpOnly cookie; an external UI (different domain) gets a bearer
token via /auth/login?return_to=... instead - see app/auth.py.

Prerequisite: run register_oauth_client.py once to register the OAuth
client covering both your local and production redirect URLs.
"""
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import auth
from app.config import settings
from app.models import ChatRequest, ChatResponse, MeResponse
from app.orchestrator import handle_message
from app.session_store import reset_session as reset_session_state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("workfront_chatbot")

app = FastAPI(title="Workfront Chatbot API")

# Cookie-based auth only works same-origin as configured here (allow_credentials
# requires a specific origin, not "*", per the CORS spec - Starlette reflects
# the request's actual origin when credentials are allowed, which is fine for
# the current same-origin widget deployment). Revisit if the widget ends up
# embedded on a different domain than this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/auth/login")
async def auth_login(return_to: str | None = None):
    redirect_uri = f"{settings.public_base_url}/auth/callback"
    try:
        return await auth.start_login(redirect_uri, return_to=return_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/auth/callback")
async def auth_callback(request: Request):
    result = await auth.complete_login(
        code=request.query_params.get("code"),
        state=request.query_params.get("state"),
        error=request.query_params.get("error"),
    )
    return result.response


@app.get("/auth/me", response_model=MeResponse)
async def auth_me(request: Request):
    ims_token = auth.get_direct_ims_token(request)
    if ims_token:
        return MeResponse(authenticated=True, name=None)

    user_id = auth.get_user_id(request)
    if not user_id:
        return MeResponse(authenticated=False)
    name = await auth.get_profile_name(user_id)
    return MeResponse(authenticated=True, name=name)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    ims_token = auth.get_direct_ims_token(request)
    if ims_token:
        user_id = auth.user_id_from_ims_token(ims_token)
        access_token_override = ims_token
    else:
        user_id = auth.get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="not_authenticated")
        access_token_override = None

    try:
        result = await handle_message(user_id, req.message, access_token_override=access_token_override)
    except RuntimeError as e:
        # not_authenticated (token missing) or reauth_required (refresh
        # failed/revoked, or the MCP server rejected the IMS token) -
        # either way the widget should show the login screen / re-fetch
        # a fresh IMS token from the shell and retry
        logger.info(f"Auth issue for user: {e}")
        raise HTTPException(status_code=401, detail=str(e))
    except Exception:
        logger.exception("Unexpected error handling chat message")
        raise HTTPException(status_code=500, detail="Internal error handling message")

    return ChatResponse(
        reply=result.reply,
        tools_used=result.tools_used,
        awaiting_confirmation=result.awaiting_confirmation,
    )


@app.post("/chat/reset")
async def reset_session(request: Request):
    ims_token = auth.get_direct_ims_token(request)
    if ims_token:
        user_id = auth.user_id_from_ims_token(ims_token)
    else:
        user_id = auth.get_user_id(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="not_authenticated")
    await reset_session_state(user_id)
    return {"status": "reset"}


# Local-dev convenience only: serves static/index.html at "/" when running
# via `uvicorn app.main:app`. On Vercel this folder isn't part of the
# deployed function - static/index.html is duplicated into public/ instead,
# which Vercel serves automatically without touching this app at all. This
# mount is guarded so the app doesn't crash if static/ isn't present.
if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
