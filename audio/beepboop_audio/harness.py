"""A deterministic harness around :class:`~beepboop_audio.pipeline.machine.Pipeline`.

Fake providers (:mod:`beepboop_audio.testing`), no model files, no devices: frames
are generated in memory with an energy VAD and a scripted wake hit, the mixer is
pulled by hand, and every audio event is collected in :attr:`PipelineHarness.sent`.
Used by the audio tests and usable by core's audio-bridge tests.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from beepboop_audio.config import AudioConfig
from beepboop_audio.frames import RingBuffer
from beepboop_audio.pipeline.earcons import Earcons
from beepboop_audio.pipeline.frontend import FrontEnd
from beepboop_audio.pipeline.leds import RecordingLed
from beepboop_audio.pipeline.machine import Pipeline
from beepboop_audio.pipeline.mixer import Mixer
from beepboop_audio.pipeline.vad import VadFramer
from beepboop_audio.speaker.identifier import SpeakerIdentifier
from beepboop_audio.testing import (
    EnergyVad,
    FakeEmbedder,
    FakeStt,
    FakeTts,
    FakeWake,
    FrameClock,
    frames,
    voice,
)
from beepboop_audio.tts.phrases import ClipCache
from beepboop_audio.types import PCM16
from beepboop_protocol import (
    AudioEvent,
    Calibration,
    ProfilesSyncData,
    ProfilesSyncEvent,
    ProfileVoiceprint,
    SpeakData,
    SpeakEvent,
    TierThresholds,
    VoiceSpec,
    new_ulid,
)

ALEX = new_ulid()
SAM = new_ulid()
ALEX_HZ = 180.0
SAM_HZ = 420.0


class PipelineHarness:
    def __init__(self, *transcripts: str, config: AudioConfig | None = None) -> None:
        self.sent: list[AudioEvent] = []
        self.stt = FakeStt(transcripts)
        self.tts = FakeTts()
        self.embedder = FakeEmbedder()
        self.mixer = Mixer()
        self.leds = RecordingLed()
        self.ring = RingBuffer()
        self.frontend = FrontEnd(FakeWake(), VadFramer(EnergyVad()), self.ring)
        self.clock = FrameClock()
        self.executor = ThreadPoolExecutor(max_workers=1)

        async def send(event: AudioEvent) -> None:
            self.sent.append(event)

        self.pipeline = Pipeline(
            config=config or AudioConfig(),
            frontend=self.frontend,
            identifier=SpeakerIdentifier((self.embedder.model_id, self.embedder.model_sha)),
            speaker=self.embedder,
            stt=self.stt,
            tts=self.tts,
            mixer=self.mixer,
            earcons=Earcons(),
            clips=ClipCache(),
            leds=self.leds,
            send=send,
            executor=self.executor,
        )

    # -- driving --------------------------------------------------------------

    async def feed(self, pcm: PCM16, *, wake_at_end: bool = False) -> None:
        for frame in frames(pcm, self.clock, wake_at_end=wake_at_end):
            if not self.frontend.muted:
                self.ring.push(frame.pcm)
            await self.pipeline.on_frame(frame)
            await asyncio.sleep(0)

    async def wake(self, hz: float = ALEX_HZ) -> None:
        """A 1.2 s "Hey beepboop" with the hit on its last frame."""
        await self.feed(voice(hz, 1200), wake_at_end=True)

    async def play(self, periods: int = 50) -> None:
        for _ in range(periods):
            self.mixer.next_period()
            await asyncio.sleep(0)

    async def settle(self) -> None:
        await self.pipeline.settle()
        await asyncio.sleep(0)

    async def until_listening_for_pin(self, max_periods: int = 2000) -> None:
        """Play the PIN prompt until the pipeline listens for digits."""
        for _ in range(max_periods):
            pin = self.pipeline.pin
            if pin is not None and pin.listening:
                return
            self.mixer.next_period()
            await asyncio.sleep(0.001)
        raise AssertionError("the pipeline never started listening for the PIN")

    async def speak(
        self,
        turn_id: str,
        text: str | None = "Okay.",
        *,
        clip: str | None = None,
        seq: int = 0,
        last: bool = True,
        listen_after: bool = False,
        utterance_id: str = "u1",
    ) -> None:
        await self.pipeline.handle(
            SpeakEvent(
                turn_id=turn_id,
                data=SpeakData(
                    utterance_id=utterance_id,
                    seq=seq,
                    last=last,
                    text=text,
                    clip=clip,
                    voice=VoiceSpec(voice_id="en_US-lessac-low", rate=1.0),
                    locale="en-US",
                    listen_after=listen_after,
                ),
            )
        )

    async def sync_two_profiles(self, *, household: tuple[str, ...] = ("kitchen",)) -> None:
        alex = self.embedder.embed(voice(ALEX_HZ, 1200))
        sam = self.embedder.embed(voice(SAM_HZ, 1200))
        calib = Calibration(
            tau=TierThresholds(act=0.7, personal=0.9), margin=TierThresholds(act=0.1, personal=0.2)
        )

        def row(pid: str, emb: np.ndarray, hotwords: list[str]) -> ProfileVoiceprint:
            return ProfileVoiceprint(
                profile_id=pid,
                centroids=[[float(x) for x in emb]] if len(emb) else [],
                calib=calib,
                hotwords=hotwords,
                voice=VoiceSpec(voice_id="en_US-lessac-low", rate=1.0),
                locale="en-US",
            )

        await self.pipeline.handle(
            ProfilesSyncEvent(
                data=ProfilesSyncData(
                    profiles=[
                        row(ALEX, alex, ["alex lamp"]),
                        row(SAM, sam, ["sam lamp"]),
                        row("guest", np.zeros(0, dtype=np.float32), list(household)),
                    ],
                    cohort_sha="none",
                    model_id=self.embedder.model_id,
                )
            )
        )

    # -- inspection -------------------------------------------------------------

    def types(self) -> list[str]:
        return [e.type for e in self.sent]

    def of(self, kind: str) -> list[AudioEvent]:
        return [e for e in self.sent if e.type == kind]

    def of_type[E](self, cls: type[E]) -> list[E]:
        return [e for e in self.sent if isinstance(e, cls)]

    def close(self) -> None:
        self.executor.shutdown(wait=True)
