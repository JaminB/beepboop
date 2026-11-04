"""Fakes and signal helpers for tests, the bench and ``serve --fake-providers``.

Every fake implements the real Protocol (VOICE_PIPELINE §17) with
deterministic behaviour and no model files, so the pipeline, the service and
the event protocol can be tested on any host. Like everything else here, no
fake writes audio anywhere.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Iterable, Iterator

import numpy as np

from beepboop_audio.frames import (
    FRAME_NS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    Frame,
    ms_to_samples,
    to_int16,
)
from beepboop_audio.types import (
    PCM16,
    Embedding,
    Final,
    Float32,
    Partial,
    SttContext,
    VoiceRef,
    WakeHit,
)

SPEECH_AMPLITUDE = 0.3
VAD_RMS = 0.02


def tone(freq: float, ms: int, amplitude: float = SPEECH_AMPLITUDE) -> PCM16:
    n = ms_to_samples(ms)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    return to_int16((amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32))


def voice(freq: float, ms: int, amplitude: float = SPEECH_AMPLITUDE) -> PCM16:
    """A crude "voice": a fundamental plus two harmonics (distinct per ``freq``)."""
    n = ms_to_samples(ms)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    wave = sum((amplitude / k) * np.sin(2 * math.pi * freq * k * t) for k in (1, 2, 3))
    return to_int16(np.asarray(wave, dtype=np.float32))


def silence(ms: int) -> PCM16:
    return np.zeros(ms_to_samples(ms), dtype=np.int16)


def split_frames(pcm: PCM16) -> list[PCM16]:
    n = len(pcm) // FRAME_SAMPLES
    return [pcm[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES].copy() for i in range(n)]


class FrameClock:
    """Hands out consecutive 10 ms capture stamps."""

    def __init__(self, start_ns: int = 1_000_000_000) -> None:
        self.t = start_ns

    def tick(self) -> int:
        self.t += FRAME_NS
        return self.t


def is_speech(pcm: PCM16) -> bool:
    if not len(pcm):
        return False
    rms = float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768.0) ** 2)))
    return rms >= VAD_RMS


def frames(
    pcm: PCM16,
    clock: FrameClock,
    *,
    wake_at_end: bool = False,
    wake_model: str = "hey_beepboop",
) -> list[Frame]:
    """Frames with energy-VAD flags; optionally a wake hit on the last frame."""
    out: list[Frame] = []
    chunks = split_frames(pcm)
    for i, chunk in enumerate(chunks):
        t = clock.tick()
        hit = WakeHit(wake_model, 0.99, t) if wake_at_end and i == len(chunks) - 1 else None
        out.append(Frame(pcm=chunk, t_ns=t, speech=is_speech(chunk), wake=hit))
    return out


class EnergyVad:
    """``VadModel`` by RMS threshold (stands in for Silero)."""

    def window(self, samples: Float32) -> bool:
        return float(np.sqrt(np.mean(samples * samples))) >= VAD_RMS if len(samples) else False

    def reset(self) -> None:
        return None


class FakeWake:
    """``WakeProvider`` that fires on frames whose first sample is :data:`MARK`."""

    MARK = 12345
    id = "fake-wake"
    license = "GPL-3.0-or-later"

    def __init__(self, model: str = "hey_beepboop") -> None:
        self._model = model
        self.processed = 0

    def models(self) -> list[str]:
        return [self._model]

    def process(self, frame: PCM16) -> WakeHit | None:
        self.processed += 1
        if len(frame) and int(frame[0]) == self.MARK:
            return WakeHit(self._model, 0.99, 0)
        return None

    def reset(self) -> None:
        return None


class FakeEmbedder:
    """``SpeakerProvider``: a pooled magnitude spectrum, so different pitches separate."""

    id = "fake-embedder"
    license = "GPL-3.0-or-later"
    model_id = "fake-embedder"
    model_sha = "0" * 16

    def __init__(self, dim: int = 64, min_samples: int = ms_to_samples(300)) -> None:
        self.dim = dim
        self.min_samples = min_samples
        self.calls = 0

    def embed(self, audio: PCM16) -> Embedding:
        self.calls += 1
        if len(audio) < self.min_samples:
            raise ValueError("clip too short for a speaker embedding")
        spectrum = np.abs(np.fft.rfft(audio.astype(np.float32), n=4096))[: 4096 // 2]
        pooled = spectrum[: self.dim * 16].reshape(self.dim, 16).sum(axis=1).astype(np.float32)
        norm = float(np.linalg.norm(pooled))
        return (pooled / norm if norm > 0 else pooled).astype(np.float32)


class FakeStt:
    """``SttProvider`` returning scripted transcripts, one per stream, in order."""

    id = "fake-stt"
    license = "GPL-3.0-or-later"
    streaming = True
    locales: tuple[str, ...] = ("en-US",)

    def __init__(self, transcripts: Iterable[str] = ()) -> None:
        self.transcripts: deque[str] = deque(transcripts)
        self.contexts: list[SttContext] = []
        self.samples_seen = 0
        self.rtf = 0.0

    def script(self, *texts: str) -> None:
        self.transcripts.extend(texts)

    async def stream(
        self, audio: AsyncIterator[PCM16], ctx: SttContext
    ) -> AsyncIterator[Partial | Final]:
        self.contexts.append(ctx)
        text = self.transcripts.popleft() if self.transcripts else ""
        words = text.split()
        n = 0
        emitted = 0
        async for frame in audio:
            n += len(frame)
            self.samples_seen += len(frame)
            if (
                ctx.mode == "command"
                and words
                and emitted < len(words)
                and n >= (emitted + 1) * 1600
            ):
                emitted += 1
                yield Partial(" ".join(words[:emitted]), False, 0)
        await asyncio.sleep(0)
        yield Final(text=text, confidence=1.0 if text else 0.0, words=(), t_eos_ns=0, t_final_ns=0)


class FakeTts:
    """``TtsProvider``: a quiet tone, 30 ms per character, in 100 ms chunks."""

    id = "fake-tts"
    license = "GPL-3.0-or-later"
    locales: tuple[str, ...] = ("en-US",)

    def __init__(
        self, voices: Iterable[str] = ("en_US-lessac-low",), ms_per_char: int = 30
    ) -> None:
        self._voices = [VoiceRef(v, 1.0, "en-US") for v in voices]
        self.ms_per_char = ms_per_char
        self.spoken: list[str] = []

    def voices(self) -> list[VoiceRef]:
        return list(self._voices)

    async def synth(self, text: str, voice: VoiceRef) -> AsyncIterator[PCM16]:
        self.spoken.append(text)
        pcm = tone(440.0, max(100, len(text) * self.ms_per_char), 0.05)
        for i in range(0, len(pcm), SAMPLE_RATE // 10):
            await asyncio.sleep(0)
            yield pcm[i : i + SAMPLE_RATE // 10]


class ScriptedCapture:
    """A ``CaptureDevice`` that plays queued PCM, then silence; paced at ``frame_delay_s``.

    ``play(pcm)`` may be called from any thread while the capture thread runs.
    """

    def __init__(self, frame_delay_s: float = 0.001) -> None:
        self._frames: deque[PCM16] = deque()
        self._delay = frame_delay_s
        self._closed = False

    def play(self, pcm: PCM16, *, wake_at_end: bool = False) -> None:
        chunks = split_frames(pcm)
        if wake_at_end and chunks:
            chunks[-1][0] = FakeWake.MARK
        self._frames.extend(chunks)

    @property
    def pending(self) -> int:
        return len(self._frames)

    def read(self) -> PCM16 | None:
        if self._closed:
            return None
        time.sleep(self._delay)
        if self._frames:
            return self._frames.popleft()
        return np.zeros(FRAME_SAMPLES, dtype=np.int16)

    def close(self) -> None:
        self._closed = True


def chunks_of(pcm: PCM16, size: int = FRAME_SAMPLES) -> Iterator[PCM16]:
    for i in range(0, len(pcm), size):
        yield pcm[i : i + size]


__all__ = [
    "EnergyVad",
    "FakeEmbedder",
    "FakeStt",
    "FakeTts",
    "FakeWake",
    "FrameClock",
    "ScriptedCapture",
    "chunks_of",
    "frames",
    "is_speech",
    "silence",
    "split_frames",
    "tone",
    "voice",
]
