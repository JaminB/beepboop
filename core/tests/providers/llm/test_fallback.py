"""The fallback ladder (docs/LLM_PROVIDERS.md §3.6): fast and sideways."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr

from beepboop.providers.llm.fallback import (
    AlwaysReachable,
    DnsReachability,
    Ladder,
    LadderExhausted,
    LadderSetupError,
    Offline,
    Rung,
    build_ladder,
    default_llm_config,
)
from beepboop.providers.llm.protocol import (
    AuthError,
    BadRequest,
    Done,
    Event,
    LLMProvider,
    Request,
    RequestMetadata,
    SystemBlock,
    TextDelta,
    Throttled,
    Unavailable,
    Usage,
)
from beepboop.testing.llm import ScriptedProvider, text

REQ = Request(
    system=[SystemBlock(text="a"), SystemBlock(text="b", cache=True), SystemBlock(text="v")],
    messages=[],
    metadata=RequestMetadata(profile_id="alex", turn_id="T1"),
)


async def collect(ladder: Ladder, chosen: list[Rung] | None = None) -> list[Event]:
    sink = chosen if chosen is not None else []
    return [e async for e in ladder.stream(REQ, on_rung=sink.append)]


def ladder_of(*providers: ScriptedProvider, timeout: float = 1.2) -> Ladder:
    names = ["fast", "smart", "fallback"]
    return Ladder(
        [Rung(names[i], p, p.model_id) for i, p in enumerate(providers)],
        AlwaysReachable(),
        first_event_timeout_s=timeout,
    )


async def test_first_rung_answers() -> None:
    chosen: list[Rung] = []
    events = await collect(ladder_of(ScriptedProvider([text("hi")])), chosen)
    assert isinstance(events[0], TextDelta)
    assert [r.name for r in chosen] == ["fast"]


@pytest.mark.parametrize("error", [Throttled("t"), Unavailable("u")])
async def test_throttled_or_unavailable_moves_on_at_once(error: Exception) -> None:
    first = ScriptedProvider([error])
    second = ScriptedProvider([text("from smart")])
    chosen: list[Rung] = []
    events = await collect(ladder_of(first, second), chosen)
    assert [r.name for r in chosen] == ["smart"]
    assert isinstance(events[0], TextDelta)
    assert len(first.requests) == 1  # no same-rung retry


async def test_first_event_timeout_moves_on() -> None:
    slow = ScriptedProvider([text("late")], delay_s=1.0)
    quick = ScriptedProvider([text("quick")])
    chosen: list[Rung] = []
    await collect(ladder_of(slow, quick, timeout=0.05), chosen)
    assert [r.name for r in chosen] == ["smart"]


@pytest.mark.parametrize("error", [BadRequest("b"), AuthError("a")])
async def test_setup_errors_stop_the_ladder(error: Exception) -> None:
    second = ScriptedProvider([text("never")])
    with pytest.raises(LadderSetupError):
        await collect(ladder_of(ScriptedProvider([error]), second))
    assert second.requests == []


async def test_every_rung_failed() -> None:
    with pytest.raises(LadderExhausted) as info:
        await collect(
            ladder_of(ScriptedProvider([Throttled("x")]), ScriptedProvider([Unavailable("y")]))
        )
    assert [a[0] for a in info.value.attempts] == ["fast", "smart"]


async def test_offline_sends_nothing() -> None:
    class Down:
        async def reachable(self) -> bool:
            return False

        def note_failure(self) -> None:
            return None

    p = ScriptedProvider([text("x")])
    with pytest.raises(Offline):
        await collect(Ladder([Rung("fast", p)], Down()))
    assert p.requests == []


async def test_mid_stream_failure_becomes_done_error() -> None:
    flaky = ScriptedProvider([text("Half", "more")], raise_after=(1, Unavailable("dropped")))
    events = await collect(ladder_of(flaky))
    assert isinstance(events[-1], Done)
    assert events[-1].stop_reason == "error"
    assert events[-1].detail == "lost_connection"


async def test_rung_effort_and_cache_ttl_are_applied() -> None:
    p = ScriptedProvider([text("x")])
    ladder = Ladder([Rung("fast", p, p.model_id, "medium", "1h")])
    await collect(ladder)
    sent = p.requests[0]
    assert sent.effort == "medium"
    assert [b.ttl for b in sent.system if b.cache] == ["1h"]


async def test_cancelling_the_consumer_cancels_the_rung() -> None:
    p = ScriptedProvider(
        [
            [
                TextDelta(text="a"),
                TextDelta(text="b"),
                Usage(input_tokens=1, output_tokens=1),
                Done(stop_reason="end_turn"),
            ]
        ],
        hang_after=1,
    )

    async def consume() -> None:
        await collect(ladder_of(p))

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert p.cancelled == 1


async def test_dns_reachability_caches_and_notes_failure() -> None:
    now = [0.0]
    r = DnsReachability("localhost", clock=lambda: now[0])
    assert await r.reachable()
    r.note_failure()
    assert not await r.reachable()
    now[0] += 10
    assert await r.reachable()


def test_build_ladder_from_defaults_with_fake_factory() -> None:
    made: list[str] = []

    def factory(pid: str, settings: Any, secrets: Any) -> Any:
        def make(model: str) -> LLMProvider:
            made.append(model)
            return ScriptedProvider([text("x")], model_id=model)

        return make

    ladder = build_ladder(None, {}, factories={"bedrock": factory}, reachability=AlwaysReachable())
    assert [r.name for r in ladder.rungs] == ["fast", "smart", "fallback"]
    assert ladder.rungs[0].model_id == default_llm_config()["aliases"]["fast"]["model"]
    smart_first = build_ladder(
        None, {}, alias="smart", factories={"bedrock": factory}, reachability=AlwaysReachable()
    )
    assert [r.name for r in smart_first.rungs] == ["smart", "fast", "fallback"]


def test_bedrock_factory_needs_keys_for_access_keys() -> None:
    with pytest.raises(AuthError):
        build_ladder(None, {})
    ladder = build_ladder(
        None,
        {"aws_access_key_id": SecretStr("AKIA"), "aws_secret_access_key": SecretStr("s")},
    )
    assert ladder.rungs[0].provider.id == "bedrock"
    assert isinstance(ladder.reachability, DnsReachability)
    assert ladder.reachability.host == "bedrock-runtime.us-east-1.amazonaws.com"
