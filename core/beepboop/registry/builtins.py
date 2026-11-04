"""The two registry built-ins, ``find_tools`` and ``read_guide`` (docs/MCP.md §6)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from beepboop.module.context import ToolContext
from beepboop.module.types import ToolError
from beepboop.registry.find_tools import ToolIndex
from beepboop.registry.manifest import ToolSpec

FIND_TOOLS_SPEC = ToolSpec(
    name="find_tools",
    description=(
        "Search the connected modules for tools that match a request and load them for this "
        "turn. Use when the request needs a module listed in the system prompt, e.g. "
        '"calendar tomorrow", "play jazz". Returns the matching tool names and descriptions.'
    ),
    risk="read",
    untrusted_output=False,
    deferred=False,
    idempotent=True,
    args={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What the user wants, in a few words."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "Max tools to return. Default 5.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {
            "speak": {"type": "string"},
            "tools": {"type": "array", "items": {"type": "object"}},
            "loaded": {"type": "boolean"},
        },
        "required": ["speak", "tools", "loaded"],
    },
    timeout_ms=2000,
)

READ_GUIDE_SPEC = ToolSpec(
    name="read_guide",
    description=(
        "Read a module's guide (its AGENT.md) and load its tools for this turn. Use when a "
        'request is ambiguous or needs the module\'s conventions, e.g. read_guide("caldav").'
    ),
    risk="read",
    untrusted_output=False,
    deferred=False,
    idempotent=True,
    args={
        "type": "object",
        "properties": {"module": {"type": "string", "description": "Module id, e.g. caldav."}},
        "required": ["module"],
        "additionalProperties": False,
    },
    output={
        "type": "object",
        "properties": {
            "speak": {"type": "string"},
            "guide": {"type": "string"},
            "tools": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["speak", "guide", "tools"],
    },
    timeout_ms=2000,
)


def make_find_tools(
    index: Callable[[], ToolIndex], allowed_for: Callable[[ToolContext], set[str]]
) -> Callable[..., Any]:
    """Build the ``find_tools`` handler; results are filtered to tools the caller may call."""

    async def find_tools(ctx: ToolContext, query: str, limit: int = 5) -> dict[str, Any]:
        hits = index().search(query, allowed=allowed_for(ctx), limit=limit)
        return {
            "speak": "" if hits else "I don't have a tool for that.",
            "tools": [
                {"name": h.name, "module": h.module_id, "description": h.description} for h in hits
            ],
            "loaded": bool(hits),
        }

    return find_tools


def make_read_guide(
    guide_of: Callable[[str], str | None], tools_of: Callable[[str], list[str]]
) -> Callable[..., Any]:
    async def read_guide(ctx: ToolContext, module: str) -> dict[str, Any]:
        guide = guide_of(module)
        if guide is None:
            raise ToolError(f"No module called {module}.", code="not_found")
        return {"speak": "", "guide": guide, "tools": tools_of(module)}

    return read_guide


__all__ = ["FIND_TOOLS_SPEC", "READ_GUIDE_SPEC", "make_find_tools", "make_read_guide"]
