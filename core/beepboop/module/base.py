"""``Module`` base class and the ``@tool`` decorator (docs/MODULES.md §5.1, §5.2)."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any, TypeVar

from beepboop.module.context import ToolContext
from beepboop.module.types import Health, PromptBlock, ToolHandler

TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_ATTR = "__beepboop_tool__"

F = TypeVar("F", bound=Callable[..., Any])


class ToolNameError(ValueError):
    """A ``@tool`` name is not ``<namespace>.<verb>``."""


def tool(name: str) -> Callable[[F], F]:
    """Bind an ``async def handler(self, ctx: ToolContext, **args)`` to a manifest tool."""
    if not TOOL_NAME.match(name):
        raise ToolNameError(name)

    def decorate(fn: F) -> F:
        if not inspect.iscoroutinefunction(fn):
            msg = f"@tool({name!r}) handler must be `async def`"
            raise TypeError(msg)
        setattr(fn, _ATTR, name)
        return fn

    return decorate


def tool_name_of(fn: object) -> str | None:
    """The manifest tool name a callable was decorated with, if any."""
    value = getattr(fn, _ATTR, None)
    return value if isinstance(value, str) else None


class Module:
    """Base class for ``modules/<id>/module.py``. All hooks are optional and async."""

    async def setup(self, ctx: ToolContext) -> None:
        """Once per process start. Warm caches, restore state from ``ctx.kv``. 5 s budget."""

    async def health(self, ctx: ToolContext) -> Health:
        """Per integration row, every 10 min and on demand."""
        return Health("ok")

    async def teardown(self, ctx: ToolContext) -> None:
        """On shutdown or disable. Cancel tasks. 2 s budget."""

    async def prompt_block(self, ctx: ToolContext) -> PromptBlock | None:
        """Only for ``category: persona`` modules with ``prompt_block`` declared (§5.6)."""
        return None

    def tools(self) -> dict[str, ToolHandler]:
        """Every ``@tool`` handler on this instance, by manifest name (bound methods)."""
        return collect_tools(self)


def collect_tools(instance: object) -> dict[str, ToolHandler]:
    found: dict[str, ToolHandler] = {}
    for attr in dir(type(instance)):
        if attr.startswith("__"):
            continue
        raw = getattr(type(instance), attr, None)
        name = tool_name_of(raw)
        if name is None:
            continue
        if name in found:
            msg = f"two handlers for tool {name!r}"
            raise ToolNameError(msg)
        found[name] = getattr(instance, attr)
    return found


def handler_parameters(fn: Callable[..., Any]) -> dict[str, inspect.Parameter]:
    """The keyword parameters after ``ctx`` (what lint L2 compares with ``args``)."""
    params = list(inspect.signature(fn).parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    if not params or params[0].name != "ctx":
        msg = "handler's first parameter after self must be `ctx`"
        raise ToolNameError(msg)
    return {p.name: p for p in params[1:]}


__all__ = ["Module", "ToolNameError", "collect_tools", "handler_parameters", "tool", "tool_name_of"]
