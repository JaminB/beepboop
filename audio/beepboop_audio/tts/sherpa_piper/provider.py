"""``TtsProvider`` for Piper-format VITS voices in sherpa-onnx (VOICE_PIPELINE §11.1).

text → :func:`~beepboop_audio.tts.normalize_en.normalize` → ``piper-phonemize``
(espeak-ng, reading the voice's ``espeak-ng-data``) → phoneme codepoints →
``sherpa_onnx.OfflineTts`` → PCM. Synthesis runs on the provider's worker
thread; chunks reach the caller as soon as the engine produces them, cut to
≤ 100 ms and resampled to 16 kHz with ``soxr`` when the voice is not 16 kHz
native. ``rate`` (0.7-1.4) maps to ``length_scale = 1 / rate``, which is
sherpa-onnx's ``speed``.

Voices are the ``kind: tts`` entries of ``models/manifest.yaml``; each loads
lazily on first use and stays loaded.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import structlog

from beepboop_audio._audiofile import Resampler
from beepboop_audio._sherpa import SherpaVits
from beepboop_audio.frames import SAMPLE_RATE, to_int16
from beepboop_audio.models import ModelEntry, ModelStore
from beepboop_audio.tts.normalize_en import normalize
from beepboop_audio.tts.sherpa_piper.phonemize import phonemize
from beepboop_audio.types import PCM16, Float32, VoiceRef

log = structlog.get_logger(__name__)

CHUNK_SAMPLES = SAMPLE_RATE // 10  # 100 ms
MIN_RATE = 0.7
MAX_RATE = 1.4


def voice_id_of(entry: ModelEntry) -> str:
    """The voice id: the model file stem (``en_US-lessac-low``) or the entry id without prefix."""
    model = entry.files.get("model")
    if model:
        return model.rsplit(".", 1)[0]
    return entry.id.removeprefix("vits-piper-")


def locale_of(voice_id: str) -> str:
    head = voice_id.split("-", 1)[0]  # en_US
    return head.replace("_", "-")


def espeak_voice(locale: str) -> str:
    """BCP-47 → espeak-ng voice name (``en-US`` → ``en-us``)."""
    return locale.lower()


def clamp_rate(rate: float) -> float:
    return min(MAX_RATE, max(MIN_RATE, rate))


def chunk(samples: PCM16, size: int = CHUNK_SAMPLES) -> list[PCM16]:
    return [samples[i : i + size] for i in range(0, len(samples), size)]


@dataclass
class _Voice:
    ref: VoiceRef
    entry: ModelEntry
    engine: SherpaVits | None = None


class PiperProvider:
    id = "sherpa-piper"
    license = "Apache-2.0"  # the runtime; each voice's licence is in models/LICENSES.md

    def __init__(self, store: ModelStore, *, threads: int = 2) -> None:
        self._threads = threads
        self._voices: dict[str, _Voice] = {}
        for entry in store.by_kind("tts"):
            if not entry.installed:
                continue
            vid = voice_id_of(entry)
            self._voices[vid] = _Voice(VoiceRef(vid, 1.0, locale_of(vid)), entry)
        self.locales: tuple[str, ...] = tuple(sorted({v.ref.locale for v in self._voices.values()}))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bb-tts")
        self._load_lock = threading.Lock()

    def voices(self) -> list[VoiceRef]:
        return [v.ref for v in self._voices.values()]

    def has_voice(self, voice_id: str) -> bool:
        return voice_id in self._voices

    def _engine(self, voice_id: str) -> SherpaVits:
        voice = self._voices.get(voice_id)
        if voice is None:
            raise KeyError(f"voice {voice_id!r} is not installed")
        with self._load_lock:
            if voice.engine is None:
                voice.engine = SherpaVits(
                    voice.entry.file("model"), voice.entry.file("tokens"), threads=self._threads
                )
            return voice.engine

    def warm(self, voice_id: str) -> None:
        """Load a voice ahead of the first sentence (sync; call on a worker thread)."""
        self._engine(voice_id)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def synth(self, text: str, voice: VoiceRef) -> AsyncIterator[PCM16]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[PCM16 | BaseException | None] = asyncio.Queue()
        stop = threading.Event()
        spoken = normalize(text)
        if not spoken:
            return

        def put(item: PCM16 | BaseException | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def work() -> None:
            try:
                engine = self._engine(voice.voice_id)
                codepoints = phonemize(spoken, espeak_voice(voice.locale))
                if not codepoints:
                    return
                resampler = (
                    Resampler(engine.sample_rate, SAMPLE_RATE)
                    if engine.sample_rate != SAMPLE_RATE
                    else None
                )

                def on_chunk(samples: Float32) -> bool:
                    if stop.is_set():
                        return False
                    out = samples
                    if resampler is not None:
                        out = np.asarray(resampler.chunk(samples), dtype=np.float32)
                    for piece in chunk(to_int16(out)):
                        put(piece)
                    return not stop.is_set()

                engine.generate(codepoints, speed=clamp_rate(voice.rate), on_chunk=on_chunk)
                if resampler is not None and not stop.is_set():
                    tail = np.asarray(
                        resampler.chunk(np.zeros(0, dtype=np.float32), last=True),
                        dtype=np.float32,
                    )
                    for piece in chunk(to_int16(tail)):
                        put(piece)
            except BaseException as exc:
                put(exc)
            finally:
                put(None)

        future = loop.run_in_executor(self._executor, work)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                if len(item):
                    yield item
        finally:
            stop.set()
            if not future.done():
                future.add_done_callback(lambda _f: None)


__all__ = ["PiperProvider", "chunk", "clamp_rate", "espeak_voice", "locale_of", "voice_id_of"]
