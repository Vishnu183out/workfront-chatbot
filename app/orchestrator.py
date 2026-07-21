"""
The actual GPT <-> MCP bridge. One call to `handle_message` = one full
turn: possibly several rounds of "GPT calls a tool, tool result goes
back to GPT" before a final natural-language answer comes out.

Two safety behaviors are enforced here, on top of whatever the model
does on its own by reading tool descriptions:

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
import json
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import settings
from app.mcp_client import mcp_client
from app.schema_adapter import (
    filter_active_tools,
    to_openai_tools,
    confirmation_required_tool_names,
)
from app.session_store import SessionState, get_state, save_state
from app.token_manager import get_access_token

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_SYSTEM_PROMPT = """You are a helpful assistant that manages Adobe Workfront \
work on behalf of the user, using the tools available to you.

Follow the instructions embedded in each tool's own description exactly - \
several tools require you to call a specific docs/lookup tool first (for \
example, resolving a name to an ID before create/update/delete, or reading \
a workflow doc before using status/enum/date filters). Do not skip these \
steps or guess at IDs, field names, or status codes.

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


async def _execute_tool_call(tc, state: SessionState, access_token: str) -> str:
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

    result = await mcp_client.call_tool(access_token, name, args)

    if name == "insights_read_docs":
        state.docs_loaded = True

    return result


async def _run_gpt_loop(state: SessionState, tools_used: list[str], access_token: str) -> ChatResult:
    tools = filter_active_tools(await mcp_client.get_tools(access_token))
    openai_tools = to_openai_tools(tools)
    confirm_set = confirmation_required_tool_names(tools)

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
        for tc in msg.tool_calls:
            result_text = await _execute_tool_call(tc, state, access_token)
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


async def handle_message(user_id: str, user_message: str) -> ChatResult:
    # raises RuntimeError("not_authenticated" / "reauth_required") if this
    # user hasn't logged in or their refresh token was revoked - main.py
    # turns that into a 401 telling the widget to show the login screen
    access_token = await get_access_token(user_id)

    state = await get_state(user_id)

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
            tools_used: list[str] = []
            for tc_dict in assistant_message.get("tool_calls", []):
                tc = _DictToolCall(tc_dict)
                result_text = await _execute_tool_call(tc, state, access_token)
                state.messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result_text}
                )
                tools_used.append(tc.function.name)
            state.pending_confirmation = None
            result = await _run_gpt_loop(state, tools_used, access_token)

    else:
        state.messages.append({"role": "user", "content": user_message})
        result = await _run_gpt_loop(state, [], access_token)

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
