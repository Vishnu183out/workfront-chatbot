"""
Bridges MCP tool definitions to OpenAI's function-calling schema, and
applies two policies read directly from the Workfront MCP tool
descriptions themselves (as observed against the live server):

1. Deprecated tools (description starts with "[DEPRECATED]") are dropped
   entirely rather than exposed to the model - no reason to let GPT pick
   a tool that's scheduled for removal when a replacement is named right
   in the same description.

2. Tools whose description signals a destructive / confirmable action are
   flagged so the orchestrator can pause and ask the end user before
   executing them, instead of GPT silently auto-confirming on their
   behalf. This is a keyword heuristic over real phrasing seen in the
   Workfront tool catalog ("requires confirmation", "MUST obtain
   explicit user confirmation", "cannot be undone") - it's a safety net,
   not a substitute for also instructing the model to respect these
   tools' own descriptions (see orchestrator.py's system prompt).
"""
from app.mcp_client import ToolInfo

_CONFIRMATION_PHRASES = [
    "requires confirmation",
    "must obtain explicit user confirmation",
    "cannot be undone",
    "requires explicit confirmation",
    "permanently",
]


def is_deprecated(tool: ToolInfo) -> bool:
    return tool.description.strip().startswith("[DEPRECATED]")


def requires_confirmation(tool: ToolInfo) -> bool:
    desc_lower = tool.description.lower()
    return any(phrase in desc_lower for phrase in _CONFIRMATION_PHRASES)


def filter_active_tools(tools: list[ToolInfo]) -> list[ToolInfo]:
    return [t for t in tools if not is_deprecated(t)]


def to_openai_tools(tools: list[ToolInfo]) -> list[dict]:
    """Convert MCP tool defs to the OpenAI `tools` array format."""
    openai_tools = []
    for t in tools:
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
        )
    return openai_tools


def confirmation_required_tool_names(tools: list[ToolInfo]) -> set[str]:
    return {t.name for t in tools if requires_confirmation(t)}
