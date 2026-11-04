"""The module SDK: what ``modules/<id>/module.py`` imports (docs/MODULES.md §5).

::

    from beepboop.module import Module, ToolContext, tool, ToolError, Health, PromptBlock

Nothing else from core is importable by a module (CLAUDE.md hard rule 2, lint L9).
"""

from beepboop.module.base import Module, ToolNameError, collect_tools, handler_parameters, tool
from beepboop.module.context import ContextSpec, ToolContext, utc_now
from beepboop.module.types import (
    Announce,
    AnnouncePriority,
    EntityRef,
    EntityState,
    GrantView,
    HAClient,
    Health,
    HealthStatus,
    HttpClient,
    HttpMethod,
    HttpResponse,
    KVStore,
    Principal,
    ProfileSummary,
    PromptBlock,
    Risk,
    SecretStore,
    ServiceResult,
    Spawn,
    ToolArgs,
    ToolError,
    ToolErrorCode,
    ToolHandler,
    ToolResult,
)

__all__ = [
    "Announce",
    "AnnouncePriority",
    "ContextSpec",
    "EntityRef",
    "EntityState",
    "GrantView",
    "HAClient",
    "Health",
    "HealthStatus",
    "HttpClient",
    "HttpMethod",
    "HttpResponse",
    "KVStore",
    "Module",
    "Principal",
    "ProfileSummary",
    "PromptBlock",
    "Risk",
    "SecretStore",
    "ServiceResult",
    "Spawn",
    "ToolArgs",
    "ToolContext",
    "ToolError",
    "ToolErrorCode",
    "ToolHandler",
    "ToolNameError",
    "ToolResult",
    "collect_tools",
    "handler_parameters",
    "tool",
    "utc_now",
]
