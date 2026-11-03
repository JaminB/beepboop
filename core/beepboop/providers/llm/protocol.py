"""The ``LLMProvider`` seam: one ``Request`` in, a stream of typed events out.

This module is the code form of docs/LLM_PROVIDERS.md §2; the doc is the
contract and wins on any disagreement. A provider *translates* only: the core
agent loop owns tool execution, sentence chunking, retries, the fallback
ladder and cancellation policy (§1).

Ordering guarantees every provider keeps (checked by ``conformance``):

* events for one tool call are contiguous:
  ``ToolCallStart`` -> zero or more ``ToolCallArgsDelta`` -> ``ToolCallEnd``;
* exactly one ``Usage`` and then exactly one ``Done`` arrive last, also after
  cancellation (``Done(stop_reason="cancelled")``);
* errors are *raised* as ``ProviderError`` subclasses, never yielded.

Cancellation contract (docs §2, §6 ``test_cancel_mid_stream``): when the task
consuming ``stream()`` is cancelled while it waits for the next event, the
provider closes its HTTP stream, yields ``Usage`` and ``Done("cancelled")``,
and re-raises ``CancelledError`` on the following pull. Consume with a plain
``async for`` (do not ``break`` on ``Done``) so the cancellation propagates.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Effort = Literal["low", "medium", "high"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "cancelled", "error"]

#: A JSON object (tool input schemas and parsed tool inputs).
JsonObject = dict[str, object]


class _Model(BaseModel):
    """Base for protocol models: immutable, no unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Caps(_Model):
    """What a provider can do; the loop and the UI read this, never the vendor."""

    tools: bool
    parallel_tools: bool
    streaming_tool_args: bool
    cache: bool
    effort: bool
    max_ctx: int
    max_output: int
    guardrails: bool


# ---- request ----------------------------------------------------------------


class SystemBlock(_Model):
    """One system-prompt block. ``cache`` allows a cache breakpoint *after* it."""

    text: str
    cache: bool = False
    ttl: Literal["5m", "1h"] = "5m"


class ToolDef(_Model):
    """A tool definition; ``input_schema`` keys are sorted by the registry."""

    name: str
    description: str
    input_schema: JsonObject
    strict: bool = True


class TextPart(_Model):
    type: Literal["text"] = "text"
    text: str


class ToolUsePart(_Model):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: JsonObject


class ToolResultPart(_Model):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False
    untrusted: bool = False


Part = Annotated[TextPart | ToolUsePart | ToolResultPart, Field(discriminator="type")]


class Message(_Model):
    role: Literal["user", "assistant"]
    content: list[Part]


class RequestMetadata(_Model):
    """Local bookkeeping. Never sent to a vendor (``test_no_secrets_on_wire``)."""

    profile_id: str
    turn_id: str
    kind: Literal["turn", "followup", "eval", "warm"] = "turn"


class Request(_Model):
    """One LLM call. Layout and caching rules: docs/LLM_PROVIDERS.md §8."""

    system: list[SystemBlock]
    messages: list[Message]
    tools: list[ToolDef] = Field(default_factory=list[ToolDef])
    max_tokens: int = 400
    effort: Effort = "low"
    stop: list[str] = Field(default_factory=list[str])
    metadata: RequestMetadata


# ---- stream events ----------------------------------------------------------


class TextDelta(_Model):
    type: Literal["text"] = "text"
    text: str


class ToolCallStart(_Model):
    type: Literal["tool_start"] = "tool_start"
    id: str
    name: str


class ToolCallArgsDelta(_Model):
    type: Literal["tool_args"] = "tool_args"
    id: str
    json_delta: str


class ToolCallEnd(_Model):
    type: Literal["tool_end"] = "tool_end"
    id: str
    input: JsonObject


class Usage(_Model):
    type: Literal["usage"] = "usage"
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    ttft_ms: int | None = None


class Done(_Model):
    type: Literal["done"] = "done"
    stop_reason: StopReason
    detail: str | None = None


Event = TextDelta | ToolCallStart | ToolCallArgsDelta | ToolCallEnd | Usage | Done


# ---- errors (raised, not yielded) -------------------------------------------


class ProviderError(Exception):
    """Base for provider failures. ``code`` is the vendor error code, if any."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class Throttled(ProviderError):  # noqa: N818 - names fixed by LLM_PROVIDERS §2
    """429 / ThrottlingException: the ladder moves on immediately."""


class Unavailable(ProviderError):  # noqa: N818 - names fixed by LLM_PROVIDERS §2
    """Connect/DNS/5xx/timeout: the ladder moves on."""


class BadRequest(ProviderError):  # noqa: N818 - names fixed by LLM_PROVIDERS §2
    """A 4xx we caused (schema, model id): no retry, bug report."""


class AuthError(ProviderError):
    """Credentials or model access: no retry, health page."""


# ---- the seam ---------------------------------------------------------------


class LLMProvider(Protocol):
    """What every LLM adapter implements (docs/LLM_PROVIDERS.md §2)."""

    id: str
    caps: Caps
    license: str

    def stream(self, req: Request) -> AsyncIterator[Event]:
        """Stream one response. Implemented as an ``async def`` generator."""
        ...

    async def warm(self) -> None:
        """Open or refresh the connection; cheap and token-free (§3.7)."""
        ...

    async def healthcheck(self) -> str:
        """``"ok"`` or a short human reason, for the Device page."""
        ...
