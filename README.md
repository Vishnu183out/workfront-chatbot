# Workfront chatbot backend

FastAPI service that bridges GPT-4o function calling to the Adobe
Workfront MCP server. Each team member logs into their own Adobe ID
through the widget — the assistant acts with their own Workfront
permissions, not a shared account. Redis-backed state so it works both
locally and on serverless hosting (Vercel).

## How it works

1. `register_oauth_client.py` — run once, ever. Registers a single OAuth
   client covering every environment you'll run in (local + Vercel), and
   prints `WORKFRONT_CLIENT_ID`/`WORKFRONT_CLIENT_SECRET` to save.
2. `app/main.py` — the server. `/auth/login` sends a visitor to Adobe's
   login; `/auth/callback` completes it and sets an httpOnly cookie
   identifying them for future requests — no password or token ever
   touches the frontend directly.
3. `POST /chat` — every message goes through `app/orchestrator.py`'s
   GPT ↔ MCP tool-calling loop, using whichever user's cookie made the
   request to determine their Workfront identity and permissions.
4. Tools are pulled live from the Workfront MCP server on first use and
   cached in memory for `TOOL_CACHE_TTL_SECONDS` (default 1hr).
5. Conversation history and every user's tokens live in Redis (Upstash),
   not local files or in-memory dicts — required for serverless, where
   neither survives between requests.

`setup_bot_account.py` from the earlier single-shared-account version is
no longer used — safe to delete, kept only for reference.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in OPENAI_API_KEY at minimum
```

Create a free Redis database at https://console.upstash.com and copy its
REST URL + token into `.env` as `UPSTASH_REDIS_REST_URL` /
`UPSTASH_REDIS_REST_TOKEN`.

Register the OAuth client — set `REDIRECT_URIS` in `.env` first (comma
separated, every URL you'll ever run at, e.g.
`http://localhost:8000/auth/callback,https://your-app.vercel.app/auth/callback`),
then:
```bash
python register_oauth_client.py
```
Paste the printed `WORKFRONT_CLIENT_ID`/`WORKFRONT_CLIENT_SECRET` into
`.env` — and into your Vercel project's environment variables once you
deploy, using the exact same values.

Start the server:
```bash
uvicorn app.main:app --reload
```
Open `http://localhost:8000/` — you'll see a "Connect your Workfront
account" screen. Log in with your own Adobe ID to start chatting.

## Deploying to Vercel

1. Push this repo to GitHub (or run `vercel` from the CLI directly).
2. Import the project in Vercel — framework preset "Other" is fine, it
   auto-detects the Python entrypoint from `requirements.txt` +
   `app/main.py`.
3. Add all the same environment variables from `.env` (Settings →
   Environment Variables) — `OPENAI_API_KEY`, `WORKFRONT_CLIENT_ID`/
   `SECRET` (same values from `register_oauth_client.py`), and
   `UPSTASH_REDIS_REST_URL`/`TOKEN`.
4. Deploy. The chat UI is live at your Vercel URL's root; the API is at
   `/chat`, `/auth/login`, `/auth/me`, etc. on that same domain.

**If login fails on Vercel with a redirect_uri mismatch**: your deployed
URL wasn't included in `REDIRECT_URIS` when you ran
`register_oauth_client.py`. Re-run it with the corrected list (this
creates a *new* client — remember to update the env var everywhere).

**Note on Vercel's Hobby plan**: it's restricted by Vercel's terms to
non-commercial, personal use. For an internal company tool — even in
testing — Pro is the compliant tier once more than one person is using
it or the project is being demoed as company work.

## API contract (for the UI team)

Auth and chat both rely on a cookie the backend sets — the widget must
send requests with `credentials: 'include'` (fetch) so the cookie is
sent/received, and the widget must be served from the same origin as
this API (see "Cross-origin" note below if that's not the case).

### `GET /auth/me`
Call this on page load to check login state.
```json
{ "authenticated": true, "name": "Jane Doe" }
```
If `authenticated: false`, show a login screen with a button that
navigates (full page redirect, not fetch) to `GET /auth/login`.

### `GET /auth/login`
Full-page redirect only — sends the browser to Adobe's login. Adobe
redirects back to `/auth/callback`, which sets the cookie and redirects
to `/`.

### `POST /chat`
```json
{ "message": "show my overdue tasks" }
```
Response:
```json
{
  "reply": "You have 3 overdue tasks: ...",
  "tools_used": ["insights_read_docs", "insights_find_workfront_data"],
  "awaiting_confirmation": false
}
```
- No `session_id` needed anymore — identity comes from the cookie.
- `401` response → the widget should show the login screen again (covers
  both "never logged in" and "login expired/revoked").
- `awaiting_confirmation: true` — the assistant is asking the user to
  confirm a sensitive action. Just send their next typed reply (yes/no)
  as a normal `POST /chat` call — no special handling needed.

### `POST /chat/reset`
Clears the current user's conversation history (wire to a "new
conversation" button).

### `GET /health`
Liveness check, no auth required.

### Cross-origin note
If the widget ends up embedded on a different domain than this API,
cookie-based auth needs adjusting: the cookie's `SameSite` attribute
would need to change from `lax` to `none` (still `Secure`), and
`CORSMiddleware`'s `allow_origins` in `app/main.py` would need to list
the widget's actual origin explicitly rather than `"*"`. Flag this if it
comes up — it's a small change, just not the current default.

## Known v1 limitations

- **No streaming** — `/chat` blocks until the full answer (including
  every tool call round-trip) is ready. `vercel.json` raises the
  function timeout above the default to accommodate multi-tool-call
  chains.
- **No logout / switch-account UI yet** — the cookie lasts 30 days
  regardless. Clearing cookies in the browser is the current workaround
  if someone needs to switch accounts.
- **Free-tier Upstash Redis rate limits** — fine for testing, worth
  checking Upstash's pricing page before heavier usage.

## Safety behaviors baked into the orchestrator

- Deprecated MCP tools (flagged `[DEPRECATED]` in their own description
  by the Workfront server) are filtered out before GPT ever sees them.
- Tools whose description implies a destructive or hard-to-reverse
  action (confirmation language, "cannot be undone", etc.) are held back
  from execution until the end user explicitly confirms via chat.
- `insights_read_docs` is enforced as a required first call before any
  other `insights_*` data-query tool, per the server's own tool
  descriptions, rather than relying on the model to remember.

See `app/orchestrator.py` for the exact logic and the system prompt
given to GPT.
