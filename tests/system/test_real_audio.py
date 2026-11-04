"""Core with the real ``beepboop-audio`` service (fake speech models) over the socket.

The audio service runs its real pipeline: capture thread, VAD gate, wake decider,
endpointing, ``SpeakerIdentifier`` scoring against the centroid core sends in
``profiles.sync``, STT stream, speak queue and mixer. Only the models are fakes
(``beepboop_audio.testing``: a pitch-sensitive embedder, scripted STT, tone TTS) and the
microphone is a scripted capture device. Nothing here is a latency or accuracy
measurement: those are the Pi gates (G1, G2, ROADMAP v0.1 #2, #3), pending on Pi.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from beepboop.db import repo
from beepboop.testing.llm import ScriptedProvider, text
from beepboop_audio.config import AudioConfig
from beepboop_audio.pipeline.io import NullPlayback
from beepboop_audio.pipeline.leds import RecordingLed
from beepboop_audio.service import AudioService, Providers
from beepboop_audio.testing import (
    EnergyVad,
    FakeEmbedder,
    FakeStt,
    FakeTts,
    FakeWake,
    ScriptedCapture,
    silence,
    voice,
)
from beepboop_protocol import TcpAddress

from .core_rig import WAIT_S, Voiceprint, boot_core

ALEX_PITCH_HZ = 200.0


async def test_spoken_fast_path_through_the_real_audio_service(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    tts = FakeTts()
    capture = ScriptedCapture()
    providers = Providers(FakeWake(), embedder, FakeStt(["turn off the kitchen lights"]), tts)
    audio = AudioService(
        AudioConfig(),
        providers,
        vad=EnergyVad(),
        capture=capture,
        playback=NullPlayback(realtime=True),
        leds=RecordingLed(),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(audio.run(TcpAddress("127.0.0.1", 0), stop))
    try:
        async with asyncio.timeout(WAIT_S):
            while audio.address is None:  # noqa: ASYNC110 - the server task sets it
                await asyncio.sleep(0.01)
        assert isinstance(audio.address, TcpAddress)
        # Alex enrolled with this embedder: the centroid of Alex's voice (a 200 Hz voice).
        centroid = [float(x) for x in embedder.embed(voice(ALEX_PITCH_HZ, 1200))]
        print_ = Voiceprint(embedder.model_id, embedder.model_sha, centroid)
        async with boot_core(tmp_path, audio.address, print_) as core:
            core.service.loop.provider = ScriptedProvider([text("Not on the fast path.")])
            bridge = core.service.audio
            assert bridge is not None
            async with asyncio.timeout(WAIT_S):
                while not audio.pipeline.profiles:  # profiles.sync arrived  # noqa: ASYNC110
                    await asyncio.sleep(0.01)

            # "Hey beepboop" (wake fires at the end of the pre-roll), the command, silence.
            capture.play(voice(ALEX_PITCH_HZ, 1200), wake_at_end=True)
            capture.play(voice(ALEX_PITCH_HZ, 1200))
            capture.play(silence(900))

            db = core.service.db
            async with asyncio.timeout(WAIT_S):
                while True:
                    rows = await db.read(lambda c: repo.turns.list_for_profile(c, core.alex))
                    if rows and rows[0].reply is not None:
                        break
                    await asyncio.sleep(0.02)
            row = rows[0]
            assert (row.path, row.via, row.id_stage) == ("fast", "voice", "final")
            assert row.transcript == "turn off the kitchen lights"
            assert core.ha_calls() == [
                ("light", "turn_off", ["light.kitchen_ceiling", "light.kitchen_counter"])
            ]
            # The reply crossed back over the socket and was synthesized by audio.
            async with asyncio.timeout(WAIT_S):
                while not tts.spoken:  # noqa: ASYNC110 - the speak worker fills it
                    await asyncio.sleep(0.01)
            assert ("speaker.final", row.id) in bridge.received
    finally:
        stop.set()
        assert await asyncio.wait_for(task, WAIT_S) == 0
