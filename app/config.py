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

    # Workfront MCP
    mcp_endpoint: str = os.getenv(
        "WORKFRONT_MCP_ENDPOINT", "https://mcp.workfront.adobe.com/mcp/v1/workfront"
    )
    mcp_authz_endpoint: str = os.getenv(
        "WORKFRONT_AUTHZ_ENDPOINT", "https://mcp.workfront.adobe.com/oauth/authorize"
    )
    mcp_token_endpoint: str = os.getenv(
        "WORKFRONT_TOKEN_ENDPOINT", "https://mcp.workfront.adobe.com/oauth/token"
    )
    mcp_registration_endpoint: str = os.getenv(
        "WORKFRONT_REGISTRATION_ENDPOINT",
        "https://mcp.workfront.adobe.com/mcp/v1/oauth/register",
    )
    mcp_scope: str = os.getenv(
        "WORKFRONT_SCOPE",
        "AdobeID openid profile email additional_info.projectedProductContext read_pc.workfront",
    )

    # The OAuth client every user's login goes through (one client, shared
    # scope, registered once via register_oauth_client.py with BOTH your
    # local and production redirect URLs). Each user still gets their own
    # tokens after logging in - see app/auth.py and app/token_manager.py.
    client_id: str = os.getenv("WORKFRONT_CLIENT_ID", "")
    client_secret: str = os.getenv("WORKFRONT_CLIENT_SECRET", "")

    # NOTE: per-user tokens live in Redis (see app/token_manager.py and
    # app/redis_client.py), not local files - required for Vercel, where
    # the filesystem doesn't persist between invocations.

    # Tool list cache TTL in seconds - avoids calling tools/list on every request
    tool_cache_ttl_seconds: int = int(os.getenv("TOOL_CACHE_TTL_SECONDS", "3600"))

    # Max iterations of the GPT <-> tool-call loop per user message, as a
    # safety valve against runaway loops
    max_tool_iterations: int = int(os.getenv("MAX_TOOL_ITERATIONS", "8"))


settings = Settings()
