"""A scripted audio service for the system tests: the audio end of the real socket.

It listens like ``beepboop-audio`` (``AudioEventServer`` on loopback TCP), says ``hello``
with an 8-dimensional fake speaker model, records every core event and plays voice turns
in the ARCHITECTURE §4.3 order (``wake`` → ``speaker.provisional`` → ``speaker.final`` →
``stt.final``), answering each finished reply with ``speak.done``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from beepboop_protocol import (
    PROTOCOL_MAJOR,
    AudioEventServer,
    AudioHelloData,
    AudioHelloEvent,
    AudioSide,
    CoreEvent,
    CoreHelloEvent,
    DeviceInfo,
    SpeakDoneData,
    SpeakDoneEvent,
    SpeakerCandidate,
    SpeakerFinalData,
    SpeakerFinalEvent,
    SpeakerInfo,
    SpeakerProvisionalData,
    SpeakerProvisionalEvent,
    SpeakEvent,
    SttFinalData,
    SttFinalEvent,
    SttInfo,
    TcpAddress,
    TtsInfo,
    WakeData,
    WakeEvent,
    WakeInfo,
    new_ulid,
)

from .core_rig import WAIT_S

DIM = 8
MODEL_ID = "fake-embedder"


@dataclass
class FakeAudio:
    """The audio service's end of the socket: listens, says ``hello``, plays scripted turns."""

    server: AudioEventServer | None = None
    channel: AudioSide | None = None
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    inbox: asyncio.Queue[CoreEvent] = field(default_factory=asyncio.Queue[CoreEvent])
    log: list[CoreEvent] = field(default_factory=list[CoreEvent])
    t_ns: int = 1_000_000_000

    def hello(self) -> AudioHelloEvent:
        return AudioHelloEvent(
            data=AudioHelloData(
                protocol=PROTOCOL_MAJOR,
                device=DeviceInfo(id="main", name="Living room"),
                wake=WakeInfo(provider="fake-wake", models=["hey_beepboop"]),
                stt=SttInfo(provider="fake-stt", locale="en-US"),
                tts=TtsInfo(provider="fake-tts", voices=["en_US-lessac-low"]),
                speaker=SpeakerInfo(model_id=MODEL_ID, model_sha="0" * 16, dim=DIM),
                aec_verified=False,
            )
        )

    async def start(self) -> TcpAddress:
        self.server = AudioEventServer(TcpAddress("127.0.0.1", 0), self.hello, self._handle)
        await self.server.start()
        address = self.server.address
        assert isinstance(address, TcpAddress)
        return address

    async def _handle(self, channel: AudioSide, _hello: CoreHelloEvent) -> None:
        self.channel = channel
        self.connected.set()
        async for event in channel:
            self.log.append(event)
            await self.inbox.put(event)
            if isinstance(event, SpeakEvent) and event.data.last:
                # Playback "finished": what the real service reports per utterance.
                await channel.send(
                    SpeakDoneEvent(
                        turn_id=event.turn_id,
                        data=SpeakDoneData(
                            utterance_id=event.data.utterance_id,
                            reason="finished",
                            audio_ms=300,
                            t_first_audio=self._tick(),
                            t_last_audio=self._tick(),
                        ),
                    )
                )

    def _tick(self, ms: int = 10) -> int:
        self.t_ns += ms * 1_000_000
        return self.t_ns

    async def next(
        self, kind: type[CoreEvent], *, turn_id: str | None = None, wait_s: float = WAIT_S
    ) -> Any:
        """The next event of ``kind`` (for ``turn_id``), skipping others."""
        async with asyncio.timeout(wait_s):
            while True:
                event = await self.inbox.get()
                if isinstance(event, kind) and (turn_id is None or event.turn_id == turn_id):
                    return event

    async def speak_reply(self, turn_id: str) -> list[SpeakEvent]:
        """Every ``speak`` of the turn up to and including the one marked ``last``."""
        out: list[SpeakEvent] = []
        while True:
            event: SpeakEvent = await self.next(SpeakEvent, turn_id=turn_id)
            out.append(event)
            if event.data.last:
                return out

    async def say(self, words: str, *, profile_id: str, score: float = 0.9) -> str:
        """One voice turn as the real pipeline emits it (ARCHITECTURE §4.3 order)."""
        assert self.channel is not None
        turn_id = new_ulid()
        ch = self.channel
        await ch.send(
            WakeEvent(
                turn_id=turn_id,
                data=WakeData(
                    device="main",
                    model="hey_beepboop",
                    score=0.97,
                    trigger="wake",
                    preroll_ms=1040,
                    t_wake=self._tick(),
                ),
            )
        )
        await ch.send(
            SpeakerProvisionalEvent(
                turn_id=turn_id,
                data=SpeakerProvisionalData(
                    profile_id=profile_id, score=score - 0.1, margin=0.2, audio_ms=1040
                ),
            )
        )
        t_start = self._tick()
        await ch.send(
            SpeakerFinalEvent(
                turn_id=turn_id,
                data=SpeakerFinalData(
                    profile_id=profile_id,
                    score=score,
                    margin=0.3,
                    anchor=True,
                    audio_ms=2990,
                    candidates=[SpeakerCandidate(profile_id=profile_id, score=score)],
                ),
            )
        )
        t_eos = self._tick(1900)
        await ch.send(
            SttFinalEvent(
                turn_id=turn_id,
                data=SttFinalData(
                    text=words,
                    locale="en-US",
                    speech_ms=1900,
                    endpoint="silence",
                    empty=False,
                    provider="fake-stt",
                    t_speech_start=t_start,
                    t_eos=t_eos,
                    t_final=self._tick(80),
                ),
            )
        )
        return turn_id

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()
