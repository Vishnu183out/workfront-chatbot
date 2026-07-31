"""
Thin wrapper around the MCP Python SDK for talking to the Workfront MCP
server. Opens a fresh session per call (v1: simple request/response, no
persistent connection).

Takes an access_token per call now (per-user auth model) rather than
pulling from one shared token source. The tool schema list is still
cached globally, since the catalog of available tools is the same
regardless of which authenticated user fetched it - only the results of
calling a tool depend on the specific user's permissions.
"""
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.config import settings

logger = logging.getLogger("workfront_chatbot.mcp_client")


class MCPAuthError(Exception):
    """The MCP server itself rejected the access token (401) - distinct
    from a missing/expired token caught earlier by token_manager, since
    this means the token was present and looked valid but the server
    still refused it (e.g. wrong audience/scope, or genuinely revoked)."""


async def _log_and_wrap_401(eg: BaseExceptionGroup, endpoint: str) -> MCPAuthError:
    for e in eg.exceptions:
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 401:
            try:
                await e.response.aread()
                body = e.response.text
            except Exception as read_err:
                body = f"<could not read response body: {read_err}>"
            logger.error(
                "MCP server rejected token. endpoint=%s response_body=%s",
                endpoint,
                body,
            )
    return MCPAuthError("MCP server rejected the access token")


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict


class MCPClient:
    def __init__(self):
        self._tool_cache: list[ToolInfo] | None = None
        self._tool_cache_at: float = 0

    async def get_tools(self, access_token: str, force_refresh: bool = False) -> list[ToolInfo]:
        cache_age = time.time() - self._tool_cache_at
        if (
            self._tool_cache is not None
            and not force_refresh
            and cache_age < settings.tool_cache_ttl_seconds
        ):
            return self._tool_cache

        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            async with streamablehttp_client(settings.mcp_endpoint, headers=headers) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    tools = [
                        ToolInfo(
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.inputSchema or {"type": "object", "properties": {}},
                        )
                        for t in result.tools
                    ]
        except* httpx.HTTPStatusError as eg:
            if any(e.response.status_code == 401 for e in eg.exceptions):
                raise (await _log_and_wrap_401(eg, settings.mcp_endpoint)) from eg
            raise

        self._tool_cache = tools
        self._tool_cache_at = time.time()
        return tools

    async def call_tool(self, access_token: str, name: str, arguments: dict) -> Any:
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            async with streamablehttp_client(settings.mcp_endpoint, headers=headers) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    # MCP tool results are a list of content blocks (text, etc.)
                    # Flatten to a single string for feeding back to the LLM.
                    parts = []
                    for block in result.content:
                        text = getattr(block, "text", None)
                        parts.append(text if text is not None else str(block))
                    return "\n".join(parts)
        except* httpx.HTTPStatusError as eg:
            if any(e.response.status_code == 401 for e in eg.exceptions):
                raise (await _log_and_wrap_401(eg, settings.mcp_endpoint)) from eg
            raise


mcp_client = MCPClient()
