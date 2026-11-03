"""Conformance checks shared by every ``LLMProvider`` (docs/LLM_PROVIDERS.md §6)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence

from beepboop.providers.llm.protocol import (
    Done,
    Event,
    LLMProvider,
    Message,
    Request,
    RequestMetadata,
    SystemBlock,
    TextDelta,
    TextPart,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolDef,
    Usage,
)

#: ``test_cancel_mid_stream``: the HTTP stream must close within this budget.
CANCEL_CLOSE_BUDGET_MS = 200

#: Strings that must never reach a vendor (``test_no_secrets_on_wire``).
_CANARY_PROFILE = "profile-canary-7f3a"
_CANARY_TURN = "turn-canary-91c2"


class ConformanceError(AssertionError):
    """A provider broke the LLMProvider contract."""


def _metadata() -> RequestMetadata:
    return RequestMetadata(profile_id=_CANARY_PROFILE, turn_id=_CANARY_TURN, kind="eval")


def text_request() -> Request:
    """A request whose answer is two short sentences and no tool."""
    return Request(
        system=[SystemBlock(text="You are a terse assistant.", cache=True)],
        messages=[
            Message(
                role="user",
                content=[TextPart(text="In two short sentences: what is a kettle for?")],
            )
        ],
        metadata=_metadata(),
    )


def tool_request() -> Request:
    """A request that should produce exactly one ``home.turn_off`` call."""
    schema: dict[str, object] = {
        "additionalProperties": False,
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
        "type": "object",
    }
    return Request(
        system=[SystemBlock(text="You control a home. Use tools; do not ask questions.")],
        messages=[Message(role="user", content=[TextPart(text="turn off the kitchen lights")])],
        tools=[
            ToolDef(name="home.turn_off", description="Turn a device off.", input_schema=schema)
        ],
        metadata=_metadata(),
    )


async def collect(provider: LLMProvider, req: Request) -> list[Event]:
    """Consume a whole stream."""
    return [event async for event in provider.stream(req)]


def assert_stream_shape(events: Sequence[Event]) -> None:
    """Ordering guarantees from §2: contiguous tool calls, Usage then Done last."""
    if len(events) < 2 or not isinstance(events[-2], Usage) or not isinstance(events[-1], Done):
        msg = f"stream must end with Usage, Done; got {[e.type for e in events[-2:]]}"
        raise ConformanceError(msg)
    if sum(isinstance(e, Usage) for e in events) != 1:
        raise ConformanceError("exactly one Usage per stream")
    if sum(isinstance(e, Done) for e in events) != 1:
        raise ConformanceError("exactly one Done per stream")
    open_call: str | None = None
    for event in events:
        if isinstance(event, ToolCallStart):
            if open_call is not None:
                raise ConformanceError("tool call started before the previous one ended")
            open_call = event.id
        elif isinstance(event, ToolCallArgsDelta | ToolCallEnd):
            if event.id != open_call:
                msg = f"{event.type} for {event.id!r} outside its call group"
                raise ConformanceError(msg)
            if isinstance(event, ToolCallEnd):
                open_call = None
        elif open_call is not None:
            msg = f"{event.type} interleaved inside tool call {open_call!r}"
            raise ConformanceError(msg)


async def check_text_streams(provider: LLMProvider, req: Request | None = None) -> list[Event]:
    """``test_text_streams``: >= 3 TextDeltas, then one Usage and Done(end_turn)."""
    events = await collect(provider, req or text_request())
    assert_stream_shape(events)
    deltas = [e for e in events if isinstance(e, TextDelta)]
    if len(deltas) < 3:
        msg = f"expected >= 3 TextDelta for a two-sentence answer, got {len(deltas)}"
        raise ConformanceError(msg)
    if not "".join(d.text for d in deltas).strip():
        raise ConformanceError("text deltas concatenate to an empty answer")
    done = events[-1]
    if not isinstance(done, Done) or done.stop_reason != "end_turn":
        raise ConformanceError(f"expected Done(end_turn), got {done!r}")
    return events


async def check_cancel_mid_stream(
    provider: LLMProvider,
    stream_closed_at: Callable[[], float | None],
    req: Request | None = None,
    *,
    budget_ms: int = CANCEL_CLOSE_BUDGET_MS,
) -> list[Event]:
    """``test_cancel_mid_stream``.

    Cancels the consuming task after the first ``TextDelta`` and checks that
    the provider closed its HTTP stream within ``budget_ms`` and finalised
    with ``Usage`` + ``Done(cancelled)``. ``stream_closed_at`` returns the
    ``time.perf_counter()`` at which the transport saw the close (a fake
    transport in CI; a socket hook live), or ``None`` if it never closed.
    """
    events: list[Event] = []
    first_text = asyncio.Event()

    async def consume() -> None:
        async for event in provider.stream(req or text_request()):
            events.append(event)
            if isinstance(event, TextDelta):
                first_text.set()

    task = asyncio.create_task(consume())
    waiter = asyncio.create_task(first_text.wait())
    await asyncio.wait({task, waiter}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if not first_text.is_set():
        task.cancel()
        raise ConformanceError("no TextDelta arrived before the stream ended")
    cancelled_at = time.perf_counter()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise ConformanceError("cancellation was swallowed: the consumer finished normally")

    closed = stream_closed_at()
    if closed is None:
        raise ConformanceError("the HTTP stream was never closed after cancellation")
    close_ms = (closed - cancelled_at) * 1000
    if close_ms > budget_ms:
        msg = f"stream closed {close_ms:.0f} ms after cancel; budget is {budget_ms} ms"
        raise ConformanceError(msg)
    assert_stream_shape(events)
    done = events[-1]
    if not isinstance(done, Done) or done.stop_reason != "cancelled":
        raise ConformanceError(f"expected Done(cancelled), got {done!r}")
    return events


async def check_tool_call_streams_args(
    provider: LLMProvider, req: Request | None = None
) -> list[Event]:
    """``test_tool_call_streams_args``: Start -> >= 1 ArgsDelta -> End, Done(tool_use)."""
    events = await collect(provider, req or tool_request())
    assert_stream_shape(events)
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    if not starts:
        raise ConformanceError("no ToolCallStart")
    call_id = starts[0].id
    deltas = [e.json_delta for e in events if isinstance(e, ToolCallArgsDelta) and e.id == call_id]
    if not deltas:
        raise ConformanceError("no ToolCallArgsDelta before ToolCallEnd")
    end = next(e for e in events if isinstance(e, ToolCallEnd) and e.id == call_id)
    if json.loads("".join(deltas)) != end.input:
        raise ConformanceError("accumulated argument deltas do not parse to ToolCallEnd.input")
    done = events[-1]
    if not isinstance(done, Done) or done.stop_reason != "tool_use":
        raise ConformanceError(f"expected Done(tool_use), got {done!r}")
    return events


async def check_usage_reported(provider: LLMProvider, req: Request | None = None) -> None:
    """``test_usage_reported``: token counts > 0; cache read on the 2nd identical call."""
    request = req or text_request()
    first = await collect(provider, request)
    second = await collect(provider, request)
    for events in (first, second):
        usage = next(e for e in events if isinstance(e, Usage))
        if usage.input_tokens + usage.cache_read_tokens <= 0 or usage.output_tokens <= 0:
            raise ConformanceError(f"token counts must be > 0, got {usage!r}")
    if provider.caps.cache:
        usage2 = next(e for e in second if isinstance(e, Usage))
        if usage2.cache_read_tokens <= 0:
            raise ConformanceError("caps.cache is true but the 2nd identical call read no cache")


def check_no_secrets_on_wire(wire_body: str) -> None:
    """``test_no_secrets_on_wire``: request ids built by ``*_request()`` never leak."""
    for canary in (_CANARY_PROFILE, _CANARY_TURN):
        if canary in wire_body:
            msg = f"request metadata {canary!r} was sent to the vendor"
            raise ConformanceError(msg)
