"""
Central configuration, loaded from environment variables / .env.

Nothing here should be hardcoded secrets - see .env.example for the
full list of variables this app expects.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # OpenAI
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")

    # --- CX Coworker Gateway (Adobe's broader MCP gateway, covers Workfront
    # among other products) - used instead of connecting directly to
    # mcp.workfront.adobe.com, because this gateway's dynamic client
    # registration accepts arbitrary production redirect URIs, while the
    # direct Workfront MCP server's registration is restricted to an
    # Adobe-controlled allowlist (confirmed by testing both directly).
    # NOTE: Workfront support on this gateway is listed by Adobe as
    # "Preview" - if Workfront tools don't appear after login, your Adobe
    # organization likely needs Preview enablement from your Adobe account
    # team, separate from anything in this code.
    mcp_endpoint: str = os.getenv(
        "WORKFRONT_MCP_ENDPOINT", "https://cx-coworker-gateway.adobe.io/mcp"
    )
    mcp_authz_endpoint: str = os.getenv(
        "WORKFRONT_AUTHZ_ENDPOINT", "https://cx-coworker-gateway.adobe.io/authorize"
    )
    mcp_token_endpoint: str = os.getenv(
        "WORKFRONT_TOKEN_ENDPOINT", "https://cx-coworker-gateway.adobe.io/token"
    )
    mcp_registration_endpoint: str = os.getenv(
        "WORKFRONT_REGISTRATION_ENDPOINT", "https://cx-coworker-gateway.adobe.io/register"
    )
    mcp_scope: str = os.getenv("WORKFRONT_SCOPE", "openid profile email")

    # The OAuth client every user's login goes through (one client, shared
    # scope, registered once via register_oauth_client.py with BOTH your
    # local and production redirect URLs). Each user still gets their own
    # tokens after logging in - see app/auth.py and app/token_manager.py.
    client_id: str = os.getenv("WORKFRONT_CLIENT_ID", "")
    client_secret: str = os.getenv("WORKFRONT_CLIENT_SECRET", "")

    # The externally-reachable base URL of THIS deployment - used to build
    # the OAuth redirect_uri. Set explicitly rather than inferred from
    # request headers (Vercel's Python runtime doesn't reliably forward
    # x-forwarded-host in a way that matched what was tried before).
    # Local dev: http://localhost:8000
    # Vercel: https://your-actual-project.vercel.app (no trailing slash)
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

    # Tool list cache TTL in seconds - avoids calling tools/list on every request
    tool_cache_ttl_seconds: int = int(os.getenv("TOOL_CACHE_TTL_SECONDS", "3600"))

    # Max iterations of the GPT <-> tool-call loop per user message, as a
    # safety valve against runaway loops
    max_tool_iterations: int = int(os.getenv("MAX_TOOL_ITERATIONS", "8"))

    # Comma-separated list of external UI origins allowed to receive a
    # post-login redirect with a session token (see app/auth.py). Only
    # needed if a separate frontend team is calling this API - without an
    # origin on this list, /auth/login's return_to param is rejected, so
    # this can't be used as an open redirect to an arbitrary site.
    # e.g. ALLOWED_UI_ORIGINS=https://ui-team.example.com,https://staging.ui-team.example.com
    allowed_ui_origins: tuple = tuple(
        o.strip().rstrip("/") for o in os.getenv("ALLOWED_UI_ORIGINS", "").split(",") if o.strip()
    )


settings = Settings()
