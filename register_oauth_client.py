"""
Run this ONCE to register a single OAuth client that works for BOTH local
dev and your production (Vercel) deployment. Adobe requires the redirect
URL used at login time to exactly match one that was registered up front
- so rather than registering separately per environment, this registers
one client with every redirect URL you list, and you reuse the same
client_id/secret everywhere.

Set REDIRECT_URIS in .env as a comma-separated list before running, e.g.:
    REDIRECT_URIS=http://localhost:8000/auth/callback,https://your-app.vercel.app/auth/callback

Usage:
    python register_oauth_client.py
"""
import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

REGISTRATION_ENDPOINT = os.getenv(
    "WORKFRONT_REGISTRATION_ENDPOINT",
    "https://mcp.workfront.adobe.com/mcp/v1/oauth/register",
)
SCOPE = os.getenv(
    "WORKFRONT_SCOPE",
    "AdobeID openid profile email additional_info.projectedProductContext read_pc.workfront",
)
REDIRECT_URIS = [u.strip() for u in os.getenv("REDIRECT_URIS", "").split(",") if u.strip()]


async def main():
    if not REDIRECT_URIS:
        print(
            "Set REDIRECT_URIS in .env first, comma-separated, e.g.\n"
            "REDIRECT_URIS=http://localhost:8000/auth/callback,https://your-app.vercel.app/auth/callback"
        )
        return

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            REGISTRATION_ENDPOINT,
            json={
                "client_name": "Workfront Chatbot",
                "redirect_uris": REDIRECT_URIS,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
                "scope": SCOPE,
            },
        )
        if r.status_code >= 400:
            print(f"\nRegistration failed: {r.status_code}")
            print(r.text)
            return
        r.raise_for_status()
        data = r.json()

    print("\nRegistered client covering:")
    for u in REDIRECT_URIS:
        print(f"  - {u}")
    print(f"\nWORKFRONT_CLIENT_ID={data['client_id']}")
    print(f"WORKFRONT_CLIENT_SECRET={data.get('client_secret', '')}")
    print(
        "\nPaste both lines into your local .env AND your Vercel project's "
        "environment variables - both environments must use this same "
        "client_id/secret pair."
    )


if __name__ == "__main__":
    asyncio.run(main())
