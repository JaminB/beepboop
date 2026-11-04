"""The audio service: providers, devices, threads, the pipeline and the event socket.

Start-up order (``beepboop-audio serve``): config → models → providers (by
entry point) → front end and devices → capture and playback threads → event
server on ``BB_AUDIO_SOCKET`` (audio listens, core connects; ARCHITECTURE §4.1)
→ ``ready`` earcon. One core connection is active at a time; a newer one
replaces the older. Events produced while no core is connected are dropped
(a turn without core cannot complete anyway).

Periodic work: ``health`` every 10 s (VOICE_PIPELINE §5), hardware-mute polling
every 100 ms (§13.2), the capture thread's heartbeat file for the compose
healthcheck.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import numpy as np
import structlog

from beepboop_audio.config import AudioConfig
from beepboop_audio.frames import Frame, RingBuffer
from beepboop_audio.models import VAD_SILERO, ModelStore
from beepboop_audio.pipeline.earcons import Earcons
from beepboop_audio.pipeline.frontend import FrontEnd
from beepboop_audio.pipeline.io import (
    CaptureDevice,
    CaptureThread,
    PlaybackDevice,
    PlaybackThread,
    heartbeat_touch,
)
from beepboop_audio.pipeline.leds import LedDriver, LedState, LogLed, MuteButton, NoMuteButton
from beepboop_audio.pipeline.machine import Pipeline
from beepboop_audio.pipeline.mixer import Mixer
from beepboop_audio.pipeline.vad import SileroVad, VadFramer, VadModel
from beepboop_audio.registry import provider_class
from beepboop_audio.speaker.identifier import SpeakerIdentifier
from beepboop_audio.tts.phrases import DIGITS, ClipCache, prerender_keys
from beepboop_audio.types import (
    SpeakerProvider,
    SttProvider,
    TtsProvider,
    VoiceRef,
    WakeProvider,
)
from beepboop_protocol import (
    PROTOCOL_MAJOR,
    AudioEvent,
    AudioEventServer,
    AudioHelloData,
    AudioHelloEvent,
    AudioSide,
    CoreHelloEvent,
    DeviceInfo,
    ErrorData,
    ErrorEvent,
    HealthData,
    HealthEvent,
    SocketAddress,
    SpeakerInfo,
    SttInfo,
    TtsInfo,
    WakeInfo,
)

log = structlog.get_logger(__name__)

HEALTH_INTERVAL_S = 10.0
MUTE_POLL_S = 0.1
FRAME_QUEUE_MAX = 500  # 5 s of frames; older ones are dropped if the loop stalls
THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")
#: ``cpu_temp_c`` when the host has no thermal sensor (development hosts).
NO_TEMPERATURE = -1.0


@dataclass
class Providers:
    wake: WakeProvider
    speaker: SpeakerProvider | None
    stt: SttProvider
    tts: TtsProvider


def load_providers(config: AudioConfig, store: ModelStore) -> Providers:
    """Construct the configured providers through their entry points."""
    threads = config.audio.threads
    p = config.providers
    wake_cls = provider_class("wake", p.wake.id)
    speaker_cls = provider_class("speaker", p.speaker.id)
    stt_cls = provider_class("stt", p.stt.id)
    tts_cls = provider_class("tts", p.tts.id)
    wake = cast(WakeProvider, wake_cls(p.wake.model_configs(), store))
    speaker = cast(SpeakerProvider, speaker_cls(store, threads=threads.speaker))
    stt = cast(
        SttProvider,
        stt_cls(
            store,
            threads=threads.stt,
            max_active_paths=p.stt.max_active_paths,
            hotwords_score=p.stt.hotwords_score,
        ),
    )
    tts = cast(TtsProvider, tts_cls(store, threads=threads.tts))
    return Providers(wake, speaker, stt, tts)


def load_vad(store: ModelStore) -> VadModel:
    return SileroVad(store.get(VAD_SILERO).file("model"))


def load_cohort(
    store: ModelStore, model_id: str
) -> np.ndarray[tuple[int, int], np.dtype[np.float32]] | None:
    """``models/speaker/cohort_<model_id>.npy`` if installed (VOICE_PIPELINE §8.3, §18 Q2)."""
    path = store.root / "speaker" / f"cohort_{model_id}.npy"
    if not path.is_file():
        return None
    data: object = np.load(path, allow_pickle=False)
    return np.asarray(data, dtype=np.float32)


def read_cpu_temp() -> float:
    try:
        return round(int(THERMAL_ZONE.read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return NO_TEMPERATURE


def read_throttled() -> bool:
    """``vcgencmd get_throttled`` bits 0x2/0x4/0x8 (capped, throttled, soft limit) right now."""
    exe = shutil.which("vcgencmd")
    if exe is None:
        return False
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, "get_throttled"], capture_output=True, text=True, timeout=2, check=False
        ).stdout
        value = int(out.strip().split("=", 1)[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False
    return bool(value & 0xE)


@dataclass
class Outbox:
    """Sends to the connected core, or drops (logged) while none is connected."""

    channel: AudioSide | None = None
    dropped: int = 0
    sent: list[AudioEvent] = field(default_factory=list[AudioEvent])
    keep: bool = False

    async def send(self, event: AudioEvent) -> None:
        if self.keep:
            self.sent.append(event)
        channel = self.channel
        if channel is None or channel.closed:
            self.dropped += 1
            log.debug("event.dropped_no_core", type=event.type, turn_id=event.turn_id)
            return
        await channel.send(event)


class AudioService:
    def __init__(
        self,
        config: AudioConfig,
        providers: Providers,
        *,
        vad: VadModel,
        capture: CaptureDevice,
        playback: PlaybackDevice,
        leds: LedDriver | None = None,
        mute_button: MuteButton | None = None,
        cohort: np.ndarray[tuple[int, int], np.dtype[np.float32]] | None = None,
        heartbeat: Path | None = None,
        theme_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.providers = providers
        self.leds = leds or LogLed()
        self.mute_button = mute_button or NoMuteButton()
        self.outbox = Outbox()
        self.mixer = Mixer()
        self.earcons = Earcons(theme_dir)
        self.clips = ClipCache()
        self.ring = RingBuffer()
        self.frontend = FrontEnd(
            providers.wake, VadFramer(vad), self.ring, vad_gate=config.providers.wake.vad_gate
        )
        speaker = providers.speaker
        embedder = (
            (speaker.model_id, speaker.model_sha) if speaker is not None else ("none", "none")
        )
        self.identifier = SpeakerIdentifier(embedder, cohort=cohort)
        models = providers.wake.models()
        self.pipeline = Pipeline(
            config=config,
            frontend=self.frontend,
            identifier=self.identifier,
            speaker=speaker,
            stt=providers.stt,
            tts=providers.tts,
            mixer=self.mixer,
            earcons=self.earcons,
            clips=self.clips,
            leds=self.leds,
            send=self.outbox.send,
            wake_model=models[0] if models else "hey_beepboop",
        )
        self._capture = capture
        self._playback = playback
        self._heartbeat = heartbeat
        self._frames: asyncio.Queue[Frame] = asyncio.Queue(maxsize=FRAME_QUEUE_MAX)
        self._capture_thread: CaptureThread | None = None
        self._playback_thread: PlaybackThread | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._device_lost = asyncio.Event()
        self.frames_dropped = 0
        self.models_loaded = True
        self._rendered: set[tuple[str, float]] = set()
        #: The bound socket address once :meth:`run` is listening (port 0 resolved).
        self.address: SocketAddress | None = None

    # -- hello / health ---------------------------------------------------------

    def hello(self) -> AudioHelloEvent:
        p = self.providers
        speaker = p.speaker
        return AudioHelloEvent(
            data=AudioHelloData(
                protocol=PROTOCOL_MAJOR,
                device=DeviceInfo(id=self.config.device.id, name=self.config.device.name),
                wake=WakeInfo(provider=p.wake.id, models=p.wake.models()),
                stt=SttInfo(provider=p.stt.id, locale=self.config.locale),
                tts=TtsInfo(
                    provider=p.tts.id, voices=[v.voice_id for v in p.tts.voices()] or ["none"]
                ),
                speaker=SpeakerInfo(
                    model_id=speaker.model_id if speaker else "none",
                    model_sha=speaker.model_sha if speaker else "none",
                    dim=speaker.dim if speaker else 1,
                ),
                # The bench aec result lives in core's database and gates barge-in (v0.3);
                # v0.1 audio never claims a verified AEC reference.
                aec_verified=False,
            )
        )

    def health(self) -> HealthEvent:
        rtf = float(getattr(self.providers.stt, "rtf", 0.0))
        return HealthEvent(
            data=HealthData(
                cpu_temp_c=read_cpu_temp(),
                throttled=read_throttled(),
                stt_rtf=max(0.0, rtf),
                playback_queue_ms=self.mixer.queue_ms(),
                models_loaded=self.models_loaded,
                aec_verified=False,
            )
        )

    # -- socket ------------------------------------------------------------------

    async def handle_core(self, channel: AudioSide, _hello: CoreHelloEvent) -> None:
        previous = self.outbox.channel
        self.outbox.channel = channel
        if previous is not None and previous is not channel:
            await previous.close()
        await channel.send(self.health())
        try:
            async for event in channel:
                await self.pipeline.handle(event)
                if event.type == "profiles.sync":
                    self._spawn_prerender()
        finally:
            if self.outbox.channel is channel:
                self.outbox.channel = None

    # -- clips -------------------------------------------------------------------

    def _voices_in_use(self) -> set[tuple[str, float]]:
        default = (self.config.providers.tts.default_voice, 1.0)
        voices = {default}
        known = {v.voice_id for v in self.providers.tts.voices()}
        for info in self.pipeline.profiles.values():
            if info.voice_id in known:
                voices.add((info.voice_id, round(info.rate, 2)))
        return {v for v in voices if v[0] in known}

    def _spawn_prerender(self) -> None:
        self._tasks.append(
            asyncio.get_running_loop().create_task(self.prerender(), name="prerender")
        )

    async def prerender(self) -> None:
        """Render the fixed clip set for every voice in use (VOICE_PIPELINE §11.3)."""
        for voice_id, rate in sorted(self._voices_in_use() - self._rendered):
            self._rendered.add((voice_id, rate))
            ref = VoiceRef(voice_id, rate, self.config.locale)
            vkey = ClipCache.voice_key(voice_id, rate)
            texts = list(prerender_keys().items()) + [(d, d) for d in DIGITS]
            for key, text in texts:
                chunks = [c async for c in self.providers.tts.synth(text, ref)]
                if not chunks:
                    continue
                pcm = np.concatenate(chunks).astype(np.int16)
                if key in DIGITS:
                    self.clips.put_sentence(vkey, text, pcm)
                else:
                    self.clips.put_clip(vkey, key, pcm)
            log.info("clips.rendered", voice=voice_id, rate=rate)

    # -- lifecycle ------------------------------------------------------------------

    def _sink(self, loop: asyncio.AbstractEventLoop) -> Callable[[Frame], None]:
        def put(frame: Frame) -> None:
            if self._frames.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._frames.get_nowait().pcm.fill(0)
                self.frames_dropped += 1
            self._frames.put_nowait(frame)

        def sink(frame: Frame) -> None:
            with contextlib.suppress(RuntimeError):  # the loop closed during shutdown
                loop.call_soon_threadsafe(put, frame)

        return sink

    async def _consume(self) -> None:
        while True:
            frame = await self._frames.get()
            await self.pipeline.on_frame(frame)

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_INTERVAL_S)
            await self.outbox.send(self.health())

    async def _mute_loop(self) -> None:
        while True:
            state = self.mute_button.poll()
            if state is not None:
                await self.pipeline.set_hardware_mute(state)
            await asyncio.sleep(MUTE_POLL_S)

    def start_devices(self) -> None:
        loop = asyncio.get_running_loop()

        def lost() -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._device_lost.set)

        self._capture_thread = CaptureThread(
            self._capture,
            self.frontend.process,
            self._sink(loop),
            on_lost=lost,
            heartbeat=heartbeat_touch(self._heartbeat) if self._heartbeat else None,
        )
        self._playback_thread = PlaybackThread(self._playback, self.mixer)
        self._capture_thread.start()
        self._playback_thread.start()

    async def run(self, address: SocketAddress, stop: asyncio.Event | None = None) -> int:
        """Serve until ``stop`` is set or the capture device is lost (exit code 1)."""
        stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        self.start_devices()
        self._tasks += [
            loop.create_task(self._consume(), name="frames"),
            loop.create_task(self._health_loop(), name="health"),
            loop.create_task(self._mute_loop(), name="mute"),
            loop.create_task(self.prerender(), name="prerender"),
        ]
        self.mixer.play_earcon(self.earcons.get("ready"))
        self.leds.show(LedState.READY)
        code = 0
        async with AudioEventServer(address, self.hello, self.handle_core) as server:
            self.address = server.address
            serve = loop.create_task(server.serve_forever(), name="socket")
            stopped = loop.create_task(stop.wait())
            lost = loop.create_task(self._device_lost.wait())
            done, _ = await asyncio.wait(
                {serve, stopped, lost}, return_when=asyncio.FIRST_COMPLETED
            )
            if lost in done:
                log.error("audio.device_lost")
                await self.outbox.send(
                    ErrorEvent(
                        data=ErrorData(
                            code="device_lost", message="capture device lost", fatal=True
                        )
                    )
                )
                code = 1
            # Close core's connection first: asyncio's server shutdown waits for open
            # connections, so cancelling serve_forever with core attached would hang.
            if self.outbox.channel is not None:
                await self.outbox.channel.close()
            for task in (serve, stopped, lost):
                task.cancel()
            await asyncio.gather(serve, stopped, lost, return_exceptions=True)
        await self.close()
        return code

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.pipeline.close()
        for thread in (self._capture_thread, self._playback_thread):
            if thread is not None:
                thread.stop()
        for thread in (self._capture_thread, self._playback_thread):
            if thread is not None and thread.is_alive():
                await asyncio.to_thread(thread.join, 1.0)
        self._capture.close()
        self._playback.close()
        self.ring.clear()
        for closer in (self.providers.stt, self.providers.tts):
            close = getattr(closer, "close", None)
            if callable(close):
                close()


__all__ = [
    "AudioService",
    "Outbox",
    "Providers",
    "load_cohort",
    "load_providers",
    "load_vad",
    "read_cpu_temp",
    "read_throttled",
]
