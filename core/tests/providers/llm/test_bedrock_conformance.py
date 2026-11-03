"""The LLM conformance suite (LLM_PROVIDERS §6) against the Bedrock provider.

Recorded Haiku 5.5 streams replayed through ``FakeConverseClient``: no
network, no credentials. M0 requires text streaming and cancellation; the
model-behaviour tests are skeletons (xfail) until v0.1 records fixtures for
them. ``test_ttft_budget`` is ``live`` (real Bedrock; ``-m live``).
"""

from __future__ import annotations

import json
import os

import pytest

from beepboop.providers.llm.bedrock.fake import FakeConverseClient, RawEvent
from beepboop.providers.llm.bedrock.probe import percentile
from beepboop.providers.llm.bedrock.provider import BedrockProvider, make_client
from beepboop.providers.llm.conformance import (
    ConformanceError,
    check_cancel_mid_stream,
    check_no_secrets_on_wire,
    check_text_streams,
    check_tool_call_streams_args,
    check_usage_reported,
    collect,
    text_request,
    tool_request,
)
from beepboop.providers.llm.protocol import TextDelta

pytestmark = pytest.mark.conformance

MODEL = "global.anthropic.claude-haiku-5-5"


def _provider(
    responses: list[list[RawEvent]], hold_after: int | None = None
) -> tuple[BedrockProvider, FakeConverseClient]:
    client = FakeConverseClient(responses, hold_after=hold_after)
    return BedrockProvider(client, MODEL), client


async def test_text_streams(text_events: list[RawEvent]) -> None:
    provider, _ = _provider([text_events])
    await check_text_streams(provider)


async def test_cancel_mid_stream(text_events: list[RawEvent]) -> None:
    # messageStart + two text deltas, then the "socket" blocks until closed.
    provider, client = _provider([text_events], hold_after=3)
    events = await check_cancel_mid_stream(provider, client.last_closed_at)
    assert 1 <= sum(isinstance(e, TextDelta) for e in events) <= 2  # cancel races delta 2


async def test_cancel_check_rejects_a_provider_that_never_closes(
    text_events: list[RawEvent], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, client = _provider([text_events], hold_after=3)
    monkeypatch.setattr(client, "last_closed_at", lambda: None)
    with pytest.raises(ConformanceError, match="never closed"):
        await check_cancel_mid_stream(provider, client.last_closed_at)


async def test_tool_call_streams_args(tool_events: list[RawEvent]) -> None:
    provider, _ = _provider([tool_events])
    await check_tool_call_streams_args(provider)


async def test_no_secrets_on_wire(text_events: list[RawEvent], tool_events: list[RawEvent]) -> None:
    provider, client = _provider([text_events, tool_events])
    await collect(provider, text_request())
    await collect(provider, tool_request())
    for call in client.calls:
        check_no_secrets_on_wire(json.dumps(call))


def test_no_secrets_check_detects_a_leak() -> None:
    body = json.dumps({"requestMetadata": text_request().metadata.model_dump()})
    with pytest.raises(ConformanceError):
        check_no_secrets_on_wire(body)


@pytest.mark.xfail(
    reason="needs a recorded identical-request pair with a >= 512-token cached prefix",
    raises=ConformanceError,
    strict=True,
)
async def test_usage_reported(text_events: list[RawEvent]) -> None:
    provider, _ = _provider([text_events])
    await check_usage_reported(provider)


@pytest.mark.xfail(reason="v0.1: record a two-tool parallel call fixture", strict=True)
async def test_parallel_tool_calls() -> None:
    raise NotImplementedError


@pytest.mark.xfail(reason="v0.1: model must reference is_error/untrusted results", strict=True)
async def test_tool_result_roundtrip() -> None:
    raise NotImplementedError


@pytest.mark.xfail(reason="v0.1: enum/integer/required schema fidelity fixture", strict=True)
async def test_schema_fidelity() -> None:
    raise NotImplementedError


@pytest.mark.live
async def test_ttft_budget() -> None:
    """Live: p50 TTFT with a cached prefix. Threshold: docs/ROADMAP.md G3.

    Needs AWS credentials (``AWS_PROFILE``) and model access; the reported
    number is informational here, the gate is judged by ``bench --gates``.
    """
    import boto3

    if boto3.Session().get_credentials() is None:  # e.g. `-m conformance` in CI
        pytest.skip("no AWS credentials (set AWS_PROFILE to run the live TTFT check)")
    region = os.environ.get("AWS_REGION", "us-west-2")
    provider = BedrockProvider(make_client(region), MODEL)
    ttfts: list[float] = []
    for _ in range(3):
        events = await check_text_streams(provider)
        usage = events[-2]
        assert usage.type == "usage"
        assert usage.ttft_ms is not None
        ttfts.append(usage.ttft_ms)
    assert percentile(ttfts, 50) > 0
