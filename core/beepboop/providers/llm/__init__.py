"""LLM providers: the ``LLMProvider`` protocol and its adapters.

Contract: docs/LLM_PROVIDERS.md. ``protocol`` is the seam; ``bedrock/`` is the
default adapter; ``conformance/`` holds the shared conformance checks.
"""

from beepboop.providers.llm.protocol import (
    AuthError,
    BadRequest,
    Caps,
    Done,
    Event,
    LLMProvider,
    Message,
    ProviderError,
    Request,
    RequestMetadata,
    SystemBlock,
    TextDelta,
    TextPart,
    Throttled,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolDef,
    ToolResultPart,
    ToolUsePart,
    Unavailable,
    Usage,
)

__all__ = [
    "AuthError",
    "BadRequest",
    "Caps",
    "Done",
    "Event",
    "LLMProvider",
    "Message",
    "ProviderError",
    "Request",
    "RequestMetadata",
    "SystemBlock",
    "TextDelta",
    "TextPart",
    "Throttled",
    "ToolCallArgsDelta",
    "ToolCallEnd",
    "ToolCallStart",
    "ToolDef",
    "ToolResultPart",
    "ToolUsePart",
    "Unavailable",
    "Usage",
]
