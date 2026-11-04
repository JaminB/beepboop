"""The audio service over a loopback socket with fake providers (ARCHITECTURE §4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from beepboop_audio.config import AudioConfig
from beepboop_audio.pipeline.io import (
    FileCapture,
    MemoryPlayback,
    NullPlayback,
    SilentCapture,
)
from beepboop_audio.pipeline.leds import FakeMuteButton, LedState, RecordingLed
from beepboop_audio.service import (
    AudioService,
    Outbox,
    Providers,
    load_cohort,
    read_cpu_temp,
    read_throttled,
)
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
from beepboop_protocol import (
    AudioEvent,
    AudioHelloEvent,
    CoreSide,
    HealthEvent,
    MicStateEvent,
    SpeakData,
    SpeakDoneEvent,
    SpeakEvent,
    SttFinalEvent,
    TcpAddress,
    VoiceSpec,
    core_channel,
    core_handshake,
    open_duplex,
)


class Running:
    def __init__(self, service: AudioService, capture: ScriptedCapture) -> None:
        self.service = service
        self.capture = capture
        self.stop = asyncio.Event()
        self.task: asyncio.Task[int] | None = None
        self.core: CoreSide | None = None
        self.hello: AudioHelloEvent | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.service.run(TcpAddress("127.0.0.1", 0), self.stop))
        for _ in range(500):
            if self.service.address is not None:
                break
            await asyncio.sleep(0.01)
        assert self.service.address is not None
        self.core = core_channel(await open_duplex(self.service.address))
        self.hello = await core_handshake(self.core)

    async def next(self, kind: str, wait_s: float = 10.0) -> AudioEvent:
        assert self.core is not None
        async with asyncio.timeout(wait_s):
            while True:
                event = await self.core.receive()
                if event.type == kind:
                    return event

    async def close(self) -> int:
        if self.core is not None:
            await self.core.close()
        self.stop.set()
        assert self.task is not None
        return await self.task


def make_service(
    *transcripts: str, mute: FakeMuteButton | None = None
) -> tuple[AudioService, ScriptedCapture, RecordingLed]:
    capture = ScriptedCapture()
    leds = RecordingLed()
    providers = Providers(FakeWake(), FakeEmbedder(), FakeStt(transcripts), FakeTts())
    service = AudioService(
        AudioConfig(),
        providers,
        vad=EnergyVad(),
        capture=capture,
        playback=NullPlayback(realtime=True),
        leds=leds,
        mute_button=mute,
    )
    return service, capture, leds


@pytest.fixture
async def running() -> AsyncIterator[Running]:
    service, capture, _ = make_service("turn off the kitchen lights")
    r = Running(service, capture)
    await r.start()
    yield r
    assert await r.close() == 0


async def test_hello_and_first_health(running: Running) -> None:
    hello = running.hello
    assert hello is not None
    assert hello.data.speaker.dim == 64  # the fake embedder's; CAM++ says 512
    assert hello.data.speaker.model_id == "fake-embedder"
    assert hello.data.wake.provider == "fake-wake"
    assert hello.data.stt.locale == "en-US"
    assert hello.data.aec_verified is False
    health = await running.next("health")
    assert isinstance(health, HealthEvent)
    assert health.data.models_loaded
    assert not health.data.aec_verified


async def test_turn_over_the_socket(running: Running) -> None:
    running.capture.play(voice(200, 1200), wake_at_end=True)
    running.capture.play(voice(200, 1200))
    running.capture.play(silence(800))
    wake = await running.next("wake")
    turn_id = wake.turn_id
    final = await running.next("stt.final")
    assert isinstance(final, SttFinalEvent)
    assert final.turn_id == turn_id
    assert final.data.text == "turn off the kitchen lights"
    assert running.core is not None
    await running.core.send(
        SpeakEvent(
            turn_id=turn_id,
            data=SpeakData(
                utterance_id="u1",
                seq=0,
                last=True,
                text="Done, the kitchen is dark.",
                voice=VoiceSpec(voice_id="en_US-lessac-low", rate=1.0),
                locale="en-US",
                listen_after=False,
            ),
        )
    )
    done = await running.next("speak.done")
    assert isinstance(done, SpeakDoneEvent)
    assert done.data.reason == "finished"
    assert done.data.audio_ms > 0


async def test_hardware_mute_is_polled() -> None:
    button = FakeMuteButton(False)
    service, capture, leds = make_service(mute=button)
    r = Running(service, capture)
    await r.start()
    try:
        button.muted = True
        state = await r.next("mic.state")
        assert isinstance(state, MicStateEvent)
        assert state.data.muted
        assert state.data.source == "hardware"
        assert LedState.MUTED in leds.states
    finally:
        assert await r.close() == 0


async def test_device_loss_is_fatal() -> None:
    service, capture, _ = make_service()
    r = Running(service, capture)
    await r.start()
    capture.close()  # read() now returns None: the device is gone
    assert r.task is not None
    assert await asyncio.wait_for(r.task, 5) == 1
    assert r.core is not None
    await r.core.close()


async def test_outbox_drops_without_core() -> None:
    outbox = Outbox(keep=True)
    health = HealthEvent.model_validate(
        {
            "type": "health",
            "data": {
                "cpu_temp_c": 1.0,
                "throttled": False,
                "stt_rtf": 0.0,
                "playback_queue_ms": 0,
                "models_loaded": True,
                "aec_verified": False,
            },
        }
    )
    await outbox.send(health)
    assert outbox.dropped == 1
    assert outbox.sent == [health]


def test_host_probes_never_raise(tmp_path: Path) -> None:
    assert isinstance(read_cpu_temp(), float)
    assert isinstance(read_throttled(), bool)
    from beepboop_audio.models import ModelStore

    assert load_cohort(ModelStore(tmp_path), "x") is None


# ------------------------------------------------------------------ devices ---


def test_file_capture_reads_then_silence(tmp_path: Path) -> None:
    import numpy as np
    import soundfile  # pyright: ignore[reportMissingTypeStubs]

    wav = tmp_path / "mic.wav"
    # A development fixture created by the test itself; the product never writes audio.
    soundfile.write(str(wav), np.full((800, 2), 0.5, dtype=np.float32), 8000)
    cap = FileCapture(wav, realtime=False)
    first = cap.read()
    assert first is not None
    assert len(first) == 160
    assert int(first[50]) > 10000
    reads = [cap.read() for _ in range(20)]
    assert cap.exhausted
    assert reads[-1] is not None
    assert not reads[-1].any()
    cap.close()
    assert cap.read() is None


def test_silent_and_memory_devices() -> None:
    cap = SilentCapture(realtime=False)
    frame = cap.read()
    assert frame is not None
    assert not frame.any()
    cap.close()
    assert cap.read() is None
    out = MemoryPlayback()
    out.write(voice(200, 20))
    assert len(out.audio()) == 320
    NullPlayback(realtime=False).write(voice(200, 20))


def test_alsa_unavailable_off_linux() -> None:
    import sys

    from beepboop_audio.pipeline.alsa import AlsaCapture, AlsaUnavailableError

    if sys.platform == "linux":
        pytest.skip("libasound may exist here; the Pi run is `-m hardware`")
    with pytest.raises(AlsaUnavailableError):
        AlsaCapture("hw:0")


def test_fake_wake_marks() -> None:
    w = FakeWake()
    pcm = silence(10)
    assert w.process(pcm) is None
    pcm[0] = FakeWake.MARK
    assert w.process(pcm) is not None
