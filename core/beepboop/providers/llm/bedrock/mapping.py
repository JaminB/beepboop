"""Pure translation between beepboop's protocol and Bedrock ConverseStream.

No I/O here, so every rule in docs/LLM_PROVIDERS.md §3.2 (request mapping) and
§3.3 (stream mapping) is unit-testable without a network or credentials.

* ``build_converse_params`` turns a ``Request`` into ``converse_stream`` kwargs.
* ``StreamTranslator`` turns ConverseStream events into protocol ``Event``s.
* ``map_client_error`` turns a botocore failure into a ``ProviderError``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from beepboop.providers.llm.protocol import (
    AuthError,
    BadRequest,
    Done,
    Effort,
    Event,
    JsonObject,
    Message,
    ProviderError,
    Request,
    StopReason,
    TextDelta,
    Throttled,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
    ToolUsePart,
    Unavailable,
    Usage,
)

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime.type_defs import (
        ContentBlockDeltaEventTypeDef,
        ContentBlockUnionTypeDef,
        ConverseStreamOutputTypeDef,
        ConverseStreamRequestTypeDef,
        MessageUnionTypeDef,
        SystemContentBlockTypeDef,
        ToolTypeDef,
    )

#: Bedrock allows at most this many cachePoints per request (§3.2).
MAX_CACHE_POINTS = 4

# ---- tool names -------------------------------------------------------------
# Converse tool names must match [a-zA-Z0-9_-]+ (botocore service model,
# ToolName shape), so "home.turn_on" is sent as "home__turn_on" (§3.2). Every
# door (openai_compat, /mcp) uses the same mapping.


def mangle_tool_name(name: str) -> str:
    """``home.turn_on`` -> ``home__turn_on`` (Converse forbids dots)."""
    return name.replace(".", "__")


def unmangle_tool_name(name: str) -> str:
    """``home__turn_on`` -> ``home.turn_on``."""
    return name.replace("__", ".")


# ---- request mapping (§3.2) -------------------------------------------------


def effort_fields(model_id: str, effort: Effort) -> dict[str, object] | None:
    """``additionalModelRequestFields`` for ``effort`` on ``model_id``.

    Haiku 5.5: thinking disabled at low/medium, adaptive + effort at high.
    Sonnet 5.5 cannot disable thinking: ``between_tools`` at low. Other models
    (e.g. the Haiku 4.5 fallback) get nothing; they do not take ``effort``.
    """
    if "claude-haiku-5-5" in model_id:
        if effort in ("low", "medium"):
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    if "claude-sonnet-5-5" in model_id:
        if effort == "low":
            return {"thinking": {"type": "between_tools"}}
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    return None


def _system_blocks(req: Request) -> list[SystemContentBlockTypeDef]:
    blocks: list[SystemContentBlockTypeDef] = []
    cache_points = 0
    for block in req.system:
        blocks.append({"text": block.text})
        if block.cache:
            cache_points += 1
            if block.ttl == "1h":
                blocks.append({"cachePoint": {"type": "default", "ttl": "1h"}})
            else:
                blocks.append({"cachePoint": {"type": "default"}})
    if cache_points > MAX_CACHE_POINTS:
        msg = f"{cache_points} cache breakpoints requested; Bedrock allows {MAX_CACHE_POINTS}"
        raise BadRequest(msg)
    return blocks


def _content_block(part: object) -> ContentBlockUnionTypeDef:
    if isinstance(part, ToolUsePart):
        return {
            "toolUse": {
                "toolUseId": part.id,
                "name": mangle_tool_name(part.name),
                "input": part.input,
            }
        }
    if isinstance(part, ToolResultPart):
        return {
            "toolResult": {
                "toolUseId": part.tool_use_id,
                "content": [{"text": part.content}],
                "status": "error" if part.is_error else "success",
            }
        }
    text = getattr(part, "text", None)
    if not isinstance(text, str):  # pragma: no cover - the Part union is closed
        msg = f"unsupported message part {type(part).__name__}"
        raise BadRequest(msg)
    return {"text": text}


def _messages(messages: Sequence[Message]) -> list[MessageUnionTypeDef]:
    return [
        {"role": m.role, "content": [_content_block(part) for part in m.content]} for m in messages
    ]


def _tools(req: Request) -> list[ToolTypeDef]:
    # Order is the registry's (sorted by name); a provider never reorders (§2).
    return [
        {
            "toolSpec": {
                "name": mangle_tool_name(tool.name),
                "description": tool.description,
                "inputSchema": {"json": tool.input_schema},
            }
        }
        for tool in req.tools
    ]


def build_converse_params(req: Request, model_id: str) -> ConverseStreamRequestTypeDef:
    """Map a ``Request`` to ``converse_stream`` keyword arguments.

    ``metadata`` (profile and turn ids) is never sent; temperature and top_p
    are never sent; ``maxTokens`` is always set (§3.2).
    """
    params: ConverseStreamRequestTypeDef = {
        "modelId": model_id,
        "system": _system_blocks(req),
        "messages": _messages(req.messages),
        "inferenceConfig": {"maxTokens": req.max_tokens},
    }
    if req.stop:
        params["inferenceConfig"]["stopSequences"] = list(req.stop)
    if req.tools:
        params["toolConfig"] = {"tools": _tools(req), "toolChoice": {"auto": {}}}
    extra = effort_fields(model_id, req.effort)
    if extra is not None:
        params["additionalModelRequestFields"] = extra
    return params


# ---- stream mapping (§3.3) --------------------------------------------------

_STOP_REASONS: Mapping[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "guardrail_intervened": "refusal",
    "content_filtered": "refusal",
}

_STREAM_ERROR_KEYS = (
    "internalServerException",
    "modelStreamErrorException",
    "validationException",
    "throttlingException",
    "serviceUnavailableException",
)


def map_stop_reason(raw: str) -> tuple[StopReason, str | None]:
    """Bedrock ``stopReason`` -> (protocol stop reason, detail)."""
    mapped = _STOP_REASONS.get(raw)
    if mapped is None:
        return "error", raw
    detail = raw if mapped == "refusal" or raw != mapped else None
    return mapped, detail


class StreamTranslator:
    """Stateful ConverseStream -> ``Event`` translator for one response.

    Feed raw events with ``feed``; call ``finish`` (normal end) or
    ``cancelled`` (task cancelled) exactly once to get the closing
    ``Usage`` + ``Done``. ``elapsed_ms`` passed to ``feed`` is the time since
    the request was sent; ``Usage.ttft_ms`` is its value at the first content
    event (``TextDelta`` or ``ToolCallStart``), and ``message_start_ms`` at
    Bedrock's ``messageStart``.
    """

    def __init__(self) -> None:
        self._tool_blocks: dict[int, tuple[str, list[str]]] = {}
        self._stop_reason: str | None = None
        self._usage: dict[str, int] = {}
        self.ttft_ms: int | None = None
        self.message_start_ms: int | None = None

    def _first_content(self, elapsed_ms: int) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = elapsed_ms

    def feed(self, raw: ConverseStreamOutputTypeDef, elapsed_ms: int) -> list[Event]:
        """Translate one raw stream event; may return zero or more events."""
        untyped = cast("Mapping[str, Mapping[str, object]]", raw)
        for key in _STREAM_ERROR_KEYS:
            error = untyped.get(key)
            if error is not None:
                raise map_error_code(key, str(error.get("message", key)))
        if "messageStart" in raw:
            if self.message_start_ms is None:
                self.message_start_ms = elapsed_ms
            return []
        if "contentBlockStart" in raw:
            start = raw["contentBlockStart"]
            tool = start["start"].get("toolUse")
            if tool is None:
                return []
            self._tool_blocks[start["contentBlockIndex"]] = (tool["toolUseId"], [])
            self._first_content(elapsed_ms)
            return [ToolCallStart(id=tool["toolUseId"], name=unmangle_tool_name(tool["name"]))]
        if "contentBlockDelta" in raw:
            return self._delta(raw["contentBlockDelta"], elapsed_ms)
        if "contentBlockStop" in raw:
            index = raw["contentBlockStop"]["contentBlockIndex"]
            block = self._tool_blocks.pop(index, None)
            if block is None:
                return []
            tool_id, parts = block
            return [ToolCallEnd(id=tool_id, input=_parse_tool_input("".join(parts)))]
        if "messageStop" in raw:
            self._stop_reason = raw["messageStop"]["stopReason"]
            return []
        if "metadata" in raw:
            usage = raw["metadata"]["usage"]
            self._usage = {
                "input_tokens": usage["inputTokens"],
                "output_tokens": usage["outputTokens"],
                "cache_read_tokens": usage.get("cacheReadInputTokens", 0),
                "cache_write_tokens": usage.get("cacheWriteInputTokens", 0),
            }
        return []

    def _delta(self, delta_event: ContentBlockDeltaEventTypeDef, elapsed_ms: int) -> list[Event]:
        delta = delta_event["delta"]
        if "text" in delta:
            if not delta["text"]:
                return []
            self._first_content(elapsed_ms)
            return [TextDelta(text=delta["text"])]
        tool_delta = delta.get("toolUse")
        if tool_delta is not None:
            block = self._tool_blocks.get(delta_event["contentBlockIndex"])
            if block is None:
                msg = "toolUse delta for a block that never started"
                raise ProviderError(msg)
            if not tool_delta["input"]:  # Bedrock opens with an empty delta
                return []
            block[1].append(tool_delta["input"])
            return [ToolCallArgsDelta(id=block[0], json_delta=tool_delta["input"])]
        # reasoningContent (thinking) and anything else: dropped, never spoken (§3.3).
        return []

    def usage(self) -> Usage:
        """The ``Usage`` event with whatever is known so far."""
        return Usage(
            input_tokens=self._usage.get("input_tokens", 0),
            output_tokens=self._usage.get("output_tokens", 0),
            cache_read_tokens=self._usage.get("cache_read_tokens", 0),
            cache_write_tokens=self._usage.get("cache_write_tokens", 0),
            ttft_ms=self.ttft_ms,
        )

    def finish(self) -> list[Event]:
        """Closing events after the stream ended normally."""
        if self._stop_reason is None:
            return [self.usage(), Done(stop_reason="error", detail="stream ended early")]
        stop, detail = map_stop_reason(self._stop_reason)
        return [self.usage(), Done(stop_reason=stop, detail=detail)]

    def cancelled(self) -> list[Event]:
        """Closing events after the consuming task was cancelled."""
        return [self.usage(), Done(stop_reason="cancelled")]


def _parse_tool_input(text: str) -> JsonObject:
    try:
        parsed: object = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        msg = "model produced tool input that is not valid JSON"
        raise ProviderError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "model produced tool input that is not a JSON object"
        raise ProviderError(msg)
    return cast("JsonObject", parsed)


# ---- errors (§3.3) ----------------------------------------------------------

_THROTTLED = {"throttlingexception", "servicequotaexceededexception", "toomanyrequestsexception"}
_UNAVAILABLE = {
    "modeltimeoutexception",
    "serviceunavailableexception",
    "internalserverexception",
    "modelstreamerrorexception",
    "modelnotreadyexception",
}
_AUTH = {
    "accessdeniedexception",
    "unrecognizedclientexception",
    "expiredtokenexception",
    "expiredtoken",
    "invalidsignatureexception",
    "signaturedoesnotmatch",
}
_BAD_REQUEST = {"validationexception", "resourcenotfoundexception", "modelerrorexception"}


def map_error_code(code: str, message: str) -> ProviderError:
    """Map a Bedrock error code (any case) to the protocol error class."""
    lowered = code.lower()
    if lowered in _THROTTLED:
        return Throttled(message, code=code)
    if lowered in _UNAVAILABLE:
        return Unavailable(message, code=code)
    if lowered in _AUTH:
        return AuthError(message, code=code)
    if lowered in _BAD_REQUEST:
        return BadRequest(message, code=code)
    return ProviderError(message, code=code)


def map_client_error(exc: BaseException) -> ProviderError:
    """Map a boto3/botocore exception to the protocol error class."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        return map_error_code(code, error.get("Message", str(exc)))
    if isinstance(exc, NoCredentialsError | PartialCredentialsError):
        return AuthError(str(exc), code=type(exc).__name__)
    if isinstance(exc, BotoConnectionError | BotoCoreError | OSError):
        return Unavailable(str(exc), code=type(exc).__name__)
    return ProviderError(str(exc), code=type(exc).__name__)
