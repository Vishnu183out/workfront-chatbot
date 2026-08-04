"""
The actual GPT <-> MCP bridge. One call to `handle_message` = one full
turn: possibly several rounds of "GPT calls a tool, tool result goes
back to GPT" before a final natural-language answer comes out.

Performance notes (this is where the "sometimes slow / times out"
behavior got addressed):

1. ONE MCP session per request. Previously every tool call opened its
   own HTTPS connection + MCP handshake to Adobe - a question needing 4
   tool calls meant 4 separate connection setups. Now handle_message
   opens exactly one session (see app/mcp_client.py) and reuses it for
   every tool call in that request's whole GPT loop.

2. Independent tool calls run in parallel. When GPT asks for several
   tool calls in the same turn, they're almost always independent reads
   (e.g. look up a user AND look up a project) - these now run
   concurrently instead of one-by-one. The one exception is
   insights_read_docs, which is run first by itself since later calls
   in the same batch may depend on docs having been loaded.

3. Conversation history is trimmed. Long-running conversations no
   longer resend an ever-growing message list to GPT on every turn -
   old messages are dropped (in whole-turn chunks, never splitting a
   tool_call from its tool_result) once history gets long.

Two safety behaviors are enforced here too, on top of whatever the
model does on its own by reading tool descriptions:

1. Confirmation gating - if the model's response includes a call to any
   tool flagged by schema_adapter.requires_confirmation, the ENTIRE
   assistant turn is held (not executed) and the end user is asked to
   confirm first. This matters for a chat widget specifically: without
   this, GPT could execute a delete/permanent-change tool the moment it
   decides to, with no human in the loop.

2. Insights docs prefetch - the live tool catalog states that
   insights_read_docs("mcp-usage") is REQUIRED before any other
   insights_* data-query tool, once per session. Rather than trust the
   model to remember this every time, tool calls to the gated insights
   tools are intercepted and bounced back to the model with an
   instruction to call insights_read_docs first if it hasn't yet.
"""
import asyncio
import json
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from app.config import settings
from app.mcp_client import mcp_client, MCPAuthError, MCPSession
from app.schema_adapter import (
    filter_active_tools,
    to_openai_tools,
    confirmation_required_tool_names,
)
from app.session_store import SessionState, get_state, save_state
from app.token_manager import get_access_token, forget_user

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_SYSTEM_PROMPT = """You are a helpful assistant that manages Adobe Workfront \
work on behalf of the user, using the tools available to you.

Follow the instructions embedded in each tool's own description exactly - \
several tools require you to call a specific docs/lookup tool first (for \
example, resolving a name to an ID before create/update/delete, or reading \
a workflow doc before using status/enum/date filters). Do not skip these \
steps or guess at IDs, field names, or status codes.

If tools named core-list_orgs / core-switch_org are available, this \
server scopes every tool call to one active Adobe organization - call \
these once near the start of a session if the user's account spans \
multiple organizations, before using any Workfront (or other product) \
tool. If the account only has one organization, this is a no-op but \
still safe to call.

If a tool call is rejected with a message telling you to call a different \
tool first, do that tool call before retrying.

Never fabricate Workfront data (project names, IDs, statuses) that a tool \
did not actually return to you.

Workfront data can change between messages, even within the same \
conversation. For any question about current state (tasks, statuses, \
approvals, projects, or anything else that could have changed) always call \
the relevant tool again rather than answering from something a tool \
returned earlier in this conversation - do not assume earlier results are \
still accurate.

Some tools require the end user's explicit confirmation before they run - \
this is enforced by the system outside of your control. If you attempt one \
and are asked to wait for confirmation, just wait; the user's next message \
will tell you whether to proceed.
"""

# Insights tools that require insights_read_docs("mcp-usage") to have been
# called first this session, per the live tool descriptions.
_DOCS_GATED_INSIGHTS_TOOLS = {
    "insights_find_workfront_data",
    "insights_list_entities",
    "insights_search_fields",
    "insights_summarize_object",
}

_AFFIRMATIVE = {"yes", "y", "yes please", "confirm", "confirmed", "go ahead", "do it", "proceed"}
_NEGATIVE = {"no", "n", "cancel", "stop", "don't", "do not", "nevermind", "never mind"}

# Once a conversation's message list grows past this, older whole turns
# get dropped (never mid-turn, which would break tool_call/tool_result
# pairing) - keeps long conversations from getting progressively slower.
_MAX_HISTORY_MESSAGES = 40


@dataclass
class ChatResult:
    reply: str
    tools_used: list[str]
    awaiting_confirmation: bool


def _interpret_confirmation(text: str) -> str:
    normalized = text.strip().lower().rstrip(".!")
    if normalized in _AFFIRMATIVE:
        return "yes"
    if normalized in _NEGATIVE:
        return "no"
    return "unclear"


def _trim_history(messages: list[dict]) -> list[dict]:
    if len(messages) <= _MAX_HISTORY_MESSAGES:
        return messages
    cutoff = len(messages) - _MAX_HISTORY_MESSAGES
    # never cut in the middle of a tool_call/tool_result run - walk
    # forward to the next 'user' message boundary
    while cutoff < len(messages) and messages[cutoff].get("role") != "user":
        cutoff += 1
    return messages[cutoff:]


async def _execute_tool_call(tc, state: SessionState, mcp_session: MCPSession) -> str:
    name = tc.function.name
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        return f"Error: could not parse arguments for {name} as JSON."

    if name in _DOCS_GATED_INSIGHTS_TOOLS and not state.docs_loaded:
        return (
            "Error: you must call insights_read_docs (with the mcp-usage "
            "doc) before using this tool this session. Call it now, then "
            "retry this request."
        )

    if name == "insights_read_docs" and state.docs_loaded:
        return "Docs already loaded this session - no need to call this again, proceed with your original request."

    result = await mcp_session.call_tool(name, args)

    if name == "insights_read_docs":
        state.docs_loaded = True

    return result


async def _execute_tool_calls_batch(tool_calls, state: SessionState, mcp_session: MCPSession) -> list[str]:
    """Runs a batch of tool calls from one GPT turn. insights_read_docs
    (if present) runs first, alone, since other calls in the same batch
    may depend on state.docs_loaded being set. Everything else in the
    batch runs concurrently, since GPT decided all of them without
    seeing each other's results - they're independent by construction."""
    docs_calls = [tc for tc in tool_calls if tc.function.name == "insights_read_docs"]
    other_calls = [tc for tc in tool_calls if tc.function.name != "insights_read_docs"]

    results: dict[str, str] = {}

    for tc in docs_calls:
        results[tc.id] = await _execute_tool_call(tc, state, mcp_session)

    if other_calls:
        outcomes = await asyncio.gather(
            *[_execute_tool_call(tc, state, mcp_session) for tc in other_calls],
            return_exceptions=True,
        )
        for tc, outcome in zip(other_calls, outcomes):
            if isinstance(outcome, BaseException):
                # re-raise as-is (e.g. MCPAuthError) so the existing
                # handling in handle_message still catches it correctly
                raise outcome
            results[tc.id] = outcome

    # return in the original order GPT requested, not completion order
    return [results[tc.id] for tc in tool_calls]


async def _run_gpt_loop(
    state: SessionState, tools_used: list[str], mcp_session: MCPSession, access_token: str
) -> ChatResult:
    tools = filter_active_tools(await mcp_client.get_tools(access_token, session=mcp_session))
    openai_tools = to_openai_tools(tools)
    confirm_set = confirmation_required_tool_names(tools)

    state.messages = _trim_history(state.messages)

    for _ in range(settings.max_tool_iterations):
        resp = await _openai.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": _SYSTEM_PROMPT}] + state.messages,
            tools=openai_tools,
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            state.messages.append({"role": "assistant", "content": msg.content})
            return ChatResult(reply=msg.content or "", tools_used=tools_used, awaiting_confirmation=False)

        gated = [tc for tc in msg.tool_calls if tc.function.name in confirm_set]
        if gated:
            state.pending_confirmation = {"assistant_message": msg.model_dump(exclude_none=True)}
            names = ", ".join(sorted({tc.function.name for tc in gated}))
            return ChatResult(
                reply=(
                    f"This requires your confirmation before I proceed: {names}. "
                    "Reply yes to continue, or no to cancel."
                ),
                tools_used=tools_used,
                awaiting_confirmation=True,
            )

        state.messages.append(msg.model_dump(exclude_none=True))
        result_texts = await _execute_tool_calls_batch(msg.tool_calls, state, mcp_session)
        for tc, result_text in zip(msg.tool_calls, result_texts):
            state.messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result_text}
            )
            tools_used.append(tc.function.name)

    return ChatResult(
        reply=(
            "I wasn't able to finish this within the allowed number of tool "
            "calls - could you narrow the request or break it into smaller steps?"
        ),
        tools_used=tools_used,
        awaiting_confirmation=False,
    )


async def handle_message(
    user_id: str, user_message: str, access_token_override: Optional[str] = None
) -> ChatResult:
    if access_token_override:
        # App Builder path - the shell already handed us a valid,
        # correctly-scoped token. No login flow, no Redis lookup.
        access_token = access_token_override
    else:
        # raises RuntimeError("not_authenticated" / "reauth_required") if
        # this user hasn't logged in or their refresh token was revoked -
        # main.py turns that into a 401 telling the widget to show login
        access_token = await get_access_token(user_id)

    state = await get_state(user_id)

    try:
        async with mcp_client.session(access_token) as mcp_session:
            if state.pending_confirmation is not None:
                decision = _interpret_confirmation(user_message)

                if decision == "unclear":
                    result = ChatResult(
                        reply="Sorry, should I go ahead? Please reply yes or no.",
                        tools_used=[],
                        awaiting_confirmation=True,
                    )

                elif decision == "no":
                    state.pending_confirmation = None
                    reply = "Okay, I won't do that. Let me know what you'd like instead."
                    state.messages.append({"role": "assistant", "content": reply})
                    result = ChatResult(reply=reply, tools_used=[], awaiting_confirmation=False)

                else:
                    # decision == "yes": replay the held assistant message and execute its tool calls
                    assistant_message = state.pending_confirmation["assistant_message"]
                    state.messages.append(assistant_message)
                    tool_calls = [_DictToolCall(d) for d in assistant_message.get("tool_calls", [])]
                    result_texts = await _execute_tool_calls_batch(tool_calls, state, mcp_session)
                    tools_used: list[str] = []
                    for tc, result_text in zip(tool_calls, result_texts):
                        state.messages.append(
                            {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                        )
                        tools_used.append(tc.function.name)
                    state.pending_confirmation = None
                    result = await _run_gpt_loop(state, tools_used, mcp_session, access_token)

            else:
                state.messages.append({"role": "user", "content": user_message})
                result = await _run_gpt_loop(state, [], mcp_session, access_token)
    except MCPAuthError:
        # The MCP server itself rejected this token (not just expired -
        # genuinely refused). Forget it so the next attempt starts a
        # completely fresh login rather than retrying a token that will
        # never work.
        await forget_user(user_id)
        raise RuntimeError("reauth_required")

    # single save point covering every branch above - state is external
    # (Redis) now, not an in-memory object that persists on its own
    # between requests, which matters specifically for serverless hosting
    # where each request can land on a fresh process.
    await save_state(user_id, state)
    return result


class _DictToolCall:
    """Adapts a plain dict (from a replayed/stored assistant message) back
    into the tiny attribute interface _execute_tool_call expects, so the
    confirmation-replay path can reuse the same function as the live path."""

    class _Function:
        def __init__(self, d):
            self.name = d["name"]
            self.arguments = d["arguments"]

    def __init__(self, d):
        self.id = d["id"]
        self.function = self._Function(d["function"])
