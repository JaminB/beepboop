"""Request and stream mapping for Bedrock ConverseStream (LLM_PROVIDERS §3.2, §3.3)."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from beepboop.providers.llm import (
    AuthError,
    BadRequest,
    Done,
    Event,
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
from beepboop.providers.llm.bedrock.fake import RawEvent
from beepboop.providers.llm.bedrock.mapping import (
    StreamTranslator,
    build_converse_params,
    effort_fields,
    mangle_tool_name,
    map_client_error,
    map_error_code,
    map_stop_reason,
    unmangle_tool_name,
)
from beepboop.providers.llm.protocol import Effort

HAIKU = "global.anthropic.claude-haiku-5-5"
SONNET = "global.anthropic.claude-sonnet-5-5"
HAIKU_45 = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

#: Parsed JSON as it would go on the wire.
Wire = dict[str, Any]  # Any: arbitrary JSON, inspected by key in assertions


def _req(**overrides: object) -> Request:
    base: dict[str, object] = {
        "system": [
            SystemBlock(text="voice contract"),
            SystemBlock(text="module cards", cache=True),
            SystemBlock(text="volatile: 07:15"),
        ],
        "messages": [Message(role="user", content=[TextPart(text="lights off")])],
        "metadata": RequestMetadata(profile_id="alex", turn_id="turn-1"),
    }
    base.update(overrides)
    return Request.model_validate(base)


def _wire(req: Request, model: str = HAIKU) -> Wire:
    return cast("Wire", json.loads(json.dumps(build_converse_params(req, model))))


def _feed(translator: StreamTranslator, raw: RawEvent, elapsed_ms: int = 0) -> list[Event]:
    return translator.feed(cast("Any", raw), elapsed_ms)  # Any: TypedDict from a plain dict


# ---- request ----------------------------------------------------------------


def test_one_cache_point_after_the_cached_block_and_volatile_after_it() -> None:
    assert _wire(_req())["system"] == [
        {"text": "voice contract"},
        {"text": "module cards"},
        {"cachePoint": {"type": "default"}},
        {"text": "volatile: 07:15"},
    ]


def test_one_hour_ttl_is_passed_through() -> None:
    wire = _wire(_req(system=[SystemBlock(text="stable", cache=True, ttl="1h")]))
    assert wire["system"][1] == {"cachePoint": {"type": "default", "ttl": "1h"}}


def test_more_than_four_cache_points_is_a_bad_request() -> None:
    blocks = [SystemBlock(text=str(i), cache=True) for i in range(5)]
    with pytest.raises(BadRequest, match="4"):
        build_converse_params(_req(system=blocks), HAIKU)


def test_max_tokens_always_set_and_sampling_and_metadata_never_sent() -> None:
    wire = _wire(_req())
    assert wire["inferenceConfig"] == {"maxTokens": 400}
    body = json.dumps(wire)
    for absent in ("temperature", "topP", "requestMetadata", "alex", "turn-1"):
        assert absent not in body


def test_stop_sequences() -> None:
    assert _wire(_req(stop=["\n\n"]))["inferenceConfig"]["stopSequences"] == ["\n\n"]


def test_tools_keep_registry_order_are_mangled_and_auto() -> None:
    schema: dict[str, object] = {"type": "object", "properties": {}}
    tools = [
        ToolDef(name="home.turn_off", description="off", input_schema=schema),
        ToolDef(name="find_tools", description="find", input_schema=schema),
    ]
    wire = _wire(_req(tools=tools))
    specs = [t["toolSpec"] for t in wire["toolConfig"]["tools"]]
    assert [s["name"] for s in specs] == ["home__turn_off", "find_tools"]
    assert specs[0]["inputSchema"] == {"json": schema}
    assert wire["toolConfig"]["toolChoice"] == {"auto": {}}


def test_no_tool_config_without_tools() -> None:
    assert "toolConfig" not in _wire(_req())


def test_tool_history_is_mapped() -> None:
    messages = [
        Message(role="user", content=[TextPart(text="lights off")]),
        Message(
            role="assistant",
            content=[ToolUsePart(id="t1", name="home.turn_off", input={"target": "kitchen"})],
        ),
        Message(
            role="user",
            content=[ToolResultPart(tool_use_id="t1", content='{"ok":false}', is_error=True)],
        ),
    ]
    wire = _wire(_req(messages=messages))
    assert wire["messages"][1]["content"][0] == {
        "toolUse": {"toolUseId": "t1", "name": "home__turn_off", "input": {"target": "kitchen"}}
    }
    assert wire["messages"][2]["content"][0] == {
        "toolResult": {
            "toolUseId": "t1",
            "content": [{"text": '{"ok":false}'}],
            "status": "error",
        }
    }


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        (HAIKU, "low", {"thinking": {"type": "disabled"}}),
        (HAIKU, "medium", {"thinking": {"type": "disabled"}}),
        (HAIKU, "high", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
        (SONNET, "low", {"thinking": {"type": "between_tools"}}),
        (
            SONNET,
            "medium",
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}},
        ),
        (HAIKU_45, "low", None),
    ],
)
def test_effort_mapping(model: str, effort: Effort, expected: object) -> None:
    assert effort_fields(model, effort) == expected
    assert _wire(_req(effort=effort), model).get("additionalModelRequestFields") == expected


def test_tool_name_mangling_round_trips() -> None:
    assert mangle_tool_name("timer.set") == "timer__set"
    assert unmangle_tool_name("timer__set") == "timer.set"
    assert unmangle_tool_name(mangle_tool_name("find_tools")) == "find_tools"


# ---- stream -----------------------------------------------------------------


def _translate(events: list[RawEvent]) -> list[Event]:
    translator = StreamTranslator()
    out: list[Event] = []
    for i, raw in enumerate(events):
        out.extend(_feed(translator, raw, elapsed_ms=10 * i))
    out.extend(translator.finish())
    return out


def test_recorded_text_stream(text_events: list[RawEvent]) -> None:
    events = _translate(text_events)
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert len(deltas) >= 3
    assert "".join(d.text for d in deltas).startswith("A kettle")
    usage, done = events[-2], events[-1]
    assert isinstance(usage, Usage)
    assert (usage.input_tokens, usage.output_tokens) == (32, 57)
    assert usage.ttft_ms == 10  # the first TextDelta is the second raw event
    assert done == Done(stop_reason="end_turn")


def test_recorded_tool_stream(tool_events: list[RawEvent]) -> None:
    events = _translate(tool_events)
    start = events[0]
    assert isinstance(start, ToolCallStart)
    assert start.name == "home.turn_off"
    args = [e for e in events if isinstance(e, ToolCallArgsDelta)]
    assert args
    assert all(a.json_delta for a in args)  # the empty opening delta is dropped
    end = next(e for e in events if isinstance(e, ToolCallEnd))
    assert end.input == {"target": "kitchen lights"}
    assert events[-1] == Done(stop_reason="tool_use")


def test_reasoning_blocks_are_dropped() -> None:
    raw: list[RawEvent] = [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"reasoningContent": {"text": "hm"}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"text": "Done."}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    assert [type(e) for e in _translate(raw)] == [TextDelta, Usage, Done]


def test_cache_tokens_and_message_start_are_recorded() -> None:
    translator = StreamTranslator()
    _feed(translator, {"messageStart": {"role": "assistant"}}, elapsed_ms=120)
    usage: RawEvent = {
        "metadata": {
            "usage": {
                "inputTokens": 5,
                "outputTokens": 3,
                "totalTokens": 8,
                "cacheReadInputTokens": 1800,
                "cacheWriteInputTokens": 0,
            },
            "metrics": {"latencyMs": 1},
        }
    }
    _feed(translator, usage, elapsed_ms=300)
    assert translator.message_start_ms == 120
    assert translator.usage().cache_read_tokens == 1800


def test_stream_ending_without_message_stop_is_an_error() -> None:
    assert _translate([{"messageStart": {"role": "assistant"}}])[-1] == Done(
        stop_reason="error", detail="stream ended early"
    )


def test_cancelled_finalisation() -> None:
    usage, done = StreamTranslator().cancelled()
    assert isinstance(usage, Usage)
    assert done == Done(stop_reason="cancelled")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", ("end_turn", None)),
        ("stop_sequence", ("end_turn", "stop_sequence")),
        ("tool_use", ("tool_use", None)),
        ("max_tokens", ("max_tokens", None)),
        ("guardrail_intervened", ("refusal", "guardrail_intervened")),
        ("content_filtered", ("refusal", "content_filtered")),
        ("malformed_tool_use", ("error", "malformed_tool_use")),
    ],
)
def test_stop_reasons(raw: str, expected: tuple[str, str | None]) -> None:
    assert map_stop_reason(raw) == expected


def test_in_stream_exception_is_raised_as_provider_error() -> None:
    with pytest.raises(Throttled):
        _feed(StreamTranslator(), {"throttlingException": {"message": "slow"}})


def test_invalid_tool_json_is_a_provider_error() -> None:
    translator = StreamTranslator()
    _feed(
        translator,
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": "t", "name": "x"}},
            }
        },
    )
    _feed(
        translator,
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": "{no"}}}},
    )
    with pytest.raises(ProviderError, match="not valid JSON"):
        _feed(translator, {"contentBlockStop": {"contentBlockIndex": 0}})


# ---- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        ("ThrottlingException", Throttled),
        ("ServiceQuotaExceededException", Throttled),
        ("ModelTimeoutException", Unavailable),
        ("ServiceUnavailableException", Unavailable),
        ("InternalServerException", Unavailable),
        ("modelStreamErrorException", Unavailable),
        ("ValidationException", BadRequest),
        ("ResourceNotFoundException", BadRequest),
        ("AccessDeniedException", AuthError),
        ("UnrecognizedClientException", AuthError),
        ("ExpiredTokenException", AuthError),
    ],
)
def test_error_codes(code: str, cls: type[ProviderError]) -> None:
    error = map_error_code(code, "msg")
    assert type(error) is cls
    assert error.code == code


def test_unknown_code_is_a_plain_provider_error() -> None:
    assert type(map_error_code("Weird", "msg")) is ProviderError


def test_map_client_error() -> None:
    client_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no access"}}, "ConverseStream"
    )
    assert isinstance(map_client_error(client_error), AuthError)
    assert isinstance(map_client_error(NoCredentialsError()), AuthError)
    assert isinstance(
        map_client_error(EndpointConnectionError(endpoint_url="https://x")), Unavailable
    )
    passthrough = Throttled("x")
    assert map_client_error(passthrough) is passthrough
