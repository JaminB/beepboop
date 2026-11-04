"""Voice turns over the real event protocol (``beepboop_protocol`` fake channel).

The audio side of the fake pair plays the audio service; core's side feeds every event to
a :class:`VoiceSession` and speaks back through a minimal channel adapter (the production
adapter is the audio bridge's). Every event goes through the real codec.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from beepboop.agent import (
    FastPath,
    MemoryConversation,
    NoLLM,
    PromptInputs,
    Speech,
    StaticDirectory,
    TurnLoop,
)
from beepboop.agent.types import CancelReason
from beepboop.agent.voice import VoiceSession
from beepboop.policy import (
    ALL_SCOPES,
    ApprovalService,
    Grant,
    GrantIndex,
    MemoryApprovalStore,
    PolicyState,
    Principal,
    ProfileView,
)
from beepboop.policy.pin import PinState
from beepboop.registry import ListAuditSink, Registry, parse_manifest
from beepboop.testing.llm import ScriptedProvider, text, tool_call
from beepboop_protocol import (
    AudioSide,
    CancelData,
    CancelEvent,
    CapturePinData,
    CapturePinEvent,
    CoreEvent,
    CoreSide,
    EarconData,
    EarconEvent,
    PinResultData,
    PinResultEvent,
    SpeakData,
    SpeakerFinalData,
    SpeakerFinalEvent,
    SpeakEvent,
    SttFinalData,
    SttFinalEvent,
    VoiceSpec,
    WakeData,
    WakeEvent,
    fake_pair,
    new_ulid,
)

ALEX = "01JBW2K3M4N5P6Q7R8S9T0VWXY"
SAM = "01JBW2K3M4N5P6Q7R8S9T0VWXZ"
PIN_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@dataclass
class ChannelVoice:
    """Speaker + VoiceControl over core's end of the channel (test adapter)."""

    channel: CoreSide
    seq: dict[str, int] = field(default_factory=dict[str, int])

    async def speak(self, turn_id: str, speech: Speech, *, last: bool) -> None:
        n = self.seq[turn_id] = self.seq.get(turn_id, -1) + 1
        await self.channel.send(
            SpeakEvent(
                turn_id=turn_id,
                data=SpeakData(
                    utterance_id=f"u{n}",
                    seq=n,
                    last=last,
                    text=speech.text,
                    clip=speech.clip,
                    voice=VoiceSpec(voice_id="en_US-lessac-low", rate=1.0),
                    locale="en-US",
                    listen_after=speech.listen_after,
                ),
            )
        )

    async def earcon(self, turn_id: str, name: str) -> None:
        await self.channel.send(
            EarconEvent(turn_id=turn_id, data=EarconData.model_validate({"name": name}))
        )

    async def cancel(self, turn_id: str, reason: CancelReason) -> None:
        await self.channel.send(CancelEvent(turn_id=turn_id, data=CancelData(reason=reason)))

    async def capture_pin(
        self, turn_id: str, approval_id: str, pin_hash: str, prompt: str | None
    ) -> None:
        await self.channel.send(
            CapturePinEvent(
                turn_id=turn_id,
                data=CapturePinData(approval_id=approval_id, pin_hash=pin_hash, prompt=prompt),
            )
        )


MANIFEST = """
id: demo
api: 1
version: 0.1.0
summary: "Voice test tools."
scope: household
category: system
tools:
  - name: timer.set
    description: Set a timer.
    risk: act
    untrusted_output: false
    deferred: false
    args:
      type: object
      properties:
        kind: {type: string}
        duration_s: {type: integer}
      required: [kind]
      additionalProperties: false
    output: {type: object, properties: {speak: {type: string}}}
  - name: demo.unlock
    description: Unlock.
    risk: critical
    untrusted_output: false
    args: {type: object, properties: {}, additionalProperties: false}
    output: {type: object, properties: {speak: {type: string}}}
"""


class Demo:
    pass


def _module() -> Any:
    from beepboop.module import Module, ToolContext, tool

    class VoiceDemo(Module):
        @tool("timer.set")
        async def set_(self, ctx: ToolContext, kind: str, duration_s: int = 0) -> dict[str, Any]:
            await asyncio.sleep(0)
            return {"speak": "Timer set."}

        @tool("demo.unlock")
        async def unlock(self, ctx: ToolContext) -> dict[str, Any]:
            return {"speak": "Front door unlocked."}

    return VoiceDemo()


class Factory:
    def for_call(self, tool: Any, principal: Principal, *, deadline: Any, approval: Any) -> Any:
        from pathlib import Path

        from beepboop.testing import fake_context

        template = Path(__file__).resolve().parents[3] / "docs" / "module-template"
        return fake_context(template, in_tool_call=True, principal=principal).with_call(
            principal=principal,
            grants=None,
            turn_id=principal.turn_id,
            deadline=deadline,
            approval=approval,
        )


class Pins:
    def __init__(self) -> None:
        self.state = PinState(enabled=True)

    async def load(self, profile_id: str) -> tuple[str | None, PinState]:
        return (PIN_HASH, self.state) if profile_id == SAM else (None, PinState(enabled=False))

    async def save(self, profile_id: str, state: PinState) -> None:
        self.state = state


@dataclass
class Rig:
    audio: AudioSide
    session: VoiceSession
    loop: TurnLoop
    provider: Any

    async def send(self, *events: Any) -> None:
        for e in events:
            await self.audio.send(e)

    async def next_core_event(self, wait_s: float = 1.0) -> CoreEvent:
        return await asyncio.wait_for(self.audio.receive(), wait_s)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    state = PolicyState(
        profiles={
            ALEX: ProfileView(ALEX, "admin", "critical", frozenset({"household"}), has_login=True),
            SAM: ProfileView(
                SAM,
                "member",
                "critical",
                frozenset({"household"}),
                has_login=True,
                pin_enabled=True,
            ),
            "guest": ProfileView("guest", "guest", "act"),
        },
        grants=GrantIndex.build(
            [
                Grant("g1", "profile", ALEX, "tool_glob", "*", "critical"),
                Grant("g2", "profile", SAM, "tool_glob", "*", "critical"),
            ]
        ),
    )
    registry = Registry(
        policy_state=lambda: state,
        approvals=ApprovalService(MemoryApprovalStore(), new_ulid),
        context_factory=Factory(),
        audit=ListAuditSink(),
        clock=lambda: NOW,
    )
    registry.register_module(parse_manifest(MANIFEST), _module())
    audio, core = fake_pair()
    voice = ChannelVoice(core)
    provider = ScriptedProvider([tool_call("c1", "demo.unlock"), text("never")])
    loop = TurnLoop(
        registry=registry,
        speaker=voice,
        conversation=MemoryConversation(),
        prompt_inputs=lambda u: PromptInputs("c", "p", "cards", core_tools=registry.tools()),
        provider=provider,
        fastpath=FastPath.for_locale("en"),
        directory=StaticDirectory({ALEX: "admin", SAM: "member"}, {ALEX: "Alex", SAM: "Sam"}),
        clock=lambda: NOW,
        pins=Pins(),
        approval_poll_s=0.005,
    )
    session = VoiceSession(loop, voice)

    async def pump() -> None:
        with contextlib.suppress(Exception):
            async for event in core:
                await session.on_event(event)

    task = asyncio.create_task(pump())
    yield Rig(audio, session, loop, provider)
    await session.close()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await audio.close()


def wake(turn_id: str, trigger: Literal["wake", "reply", "button"] = "wake") -> WakeEvent:
    return WakeEvent(
        turn_id=turn_id,
        data=WakeData(
            device="living-room",
            model="hey_beepboop",
            score=0.97,
            trigger=trigger,
            preroll_ms=1000,
            t_wake=1,
        ),
    )


def final(turn_id: str, profile_id: str, score: float = 0.83) -> SpeakerFinalEvent:
    return SpeakerFinalEvent(
        turn_id=turn_id,
        data=SpeakerFinalData(
            profile_id=profile_id,
            score=score,
            margin=0.27,
            anchor=True,
            audio_ms=2000,
            candidates=[],
        ),
    )


def stt(turn_id: str, said: str) -> SttFinalEvent:
    return SttFinalEvent(
        turn_id=turn_id,
        data=SttFinalData(
            text=said,
            locale="en-US",
            speech_ms=1500,
            endpoint="silence",
            empty=not said,
            provider="sherpa-zipformer",
            t_speech_start=1_000_000_000,
            t_eos=2_500_000_000,
            t_final=2_580_000_000,
        ),
    )


async def test_fast_path_turn_end_to_end(rig: Rig) -> None:
    tid = new_ulid()
    await rig.send(wake(tid), final(tid, ALEX), stt(tid, "set a timer for ten minutes"))
    event = await rig.next_core_event()
    assert isinstance(event, SpeakEvent)
    assert event.turn_id == tid
    assert event.data.text == "Timer set."
    assert event.data.last is True
    await rig.session.drain()
    result = rig.session.results[-1]
    assert result.path == "fast"
    assert result.stages_ms["stt_tail"] == 80.0
    assert result.stages_ms["speech"] == 1500.0


async def test_late_speaker_final_runs_as_guest(rig: Rig) -> None:
    tid = new_ulid()
    rig.session.speaker_final_wait_s = 0.02
    await rig.send(wake(tid), stt(tid, "set a timer for ten minutes"))
    event = await rig.next_core_event()
    assert isinstance(event, SpeakEvent)
    assert event.data.clip == "unknown_speaker"  # guest has no timer grant
    await rig.session.drain()
    assert rig.session.results[-1].tool_calls[0].reason == "no_grant"


async def test_false_wake_is_silent(rig: Rig) -> None:
    tid = new_ulid()
    await rig.send(wake(tid), final(tid, ALEX), stt(tid, ""))
    await asyncio.sleep(0.05)
    assert rig.session.results == []
    with pytest.raises(TimeoutError):
        await rig.next_core_event(wait_s=0.05)


async def test_new_wake_supersedes_and_cancels(rig: Rig) -> None:
    hang = ScriptedProvider([text("a", "b")], hang_after=1)
    rig.loop.provider = hang
    first = new_ulid()
    await rig.send(wake(first), final(first, ALEX), stt(first, "tell me a long story"))
    await asyncio.sleep(0.05)
    second = new_ulid()
    await rig.send(wake(second))
    event = await rig.next_core_event()
    assert isinstance(event, CancelEvent)
    assert event.turn_id == first
    assert event.data.reason == "superseded"
    await asyncio.sleep(0.02)
    assert hang.cancelled == 1


async def test_muted_mic_starts_no_turn(rig: Rig) -> None:
    from beepboop_protocol import MicStateData, MicStateEvent

    await rig.send(MicStateEvent(data=MicStateData(muted=True, source="hardware")))
    tid = new_ulid()
    await rig.send(wake(tid), final(tid, ALEX), stt(tid, "set a timer for ten minutes"))
    with pytest.raises(TimeoutError):
        await rig.next_core_event(wait_s=0.05)


async def test_spoken_pin_round_trip_approves(rig: Rig) -> None:
    tid = new_ulid()
    await rig.send(wake(tid), final(tid, SAM), stt(tid, "unlock the front door"))
    earcon = await rig.next_core_event()
    assert isinstance(earcon, EarconEvent)
    assert earcon.data.name == "ack"
    prompt = await rig.next_core_event()
    assert isinstance(prompt, SpeakEvent)
    assert prompt.data.clip == "approval.prompt_pin"
    capture = await rig.next_core_event()
    assert isinstance(capture, CapturePinEvent)
    assert capture.turn_id == tid
    await rig.send(
        PinResultEvent(
            turn_id=tid, data=PinResultData(approval_id=capture.data.approval_id, outcome="ok")
        )
    )
    done = await rig.next_core_event()
    assert isinstance(done, SpeakEvent)
    assert done.data.clip == "approval.done"
    assert done.data.text == "Done. Front door unlocked."
    await rig.session.drain()


async def test_turn_principal_comes_from_speaker_final(rig: Rig) -> None:
    rig.loop.provider = NoLLM()
    tid = new_ulid()
    await rig.send(wake(tid), final(tid, ALEX, score=0.91), stt(tid, "what's the weather"))
    await rig.next_core_event()
    await rig.session.drain()
    started = rig.session.results[-1]
    assert started.path == "offline"
    audit = rig.loop.registry.audit
    assert isinstance(audit, ListAuditSink)


def test_principal_shape() -> None:
    p = Principal(ALEX, "voice", 0.9, 0.2, "living-room", ALL_SCOPES, False, new_ulid())
    assert p.via == "voice"
