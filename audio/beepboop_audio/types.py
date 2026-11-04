"""Provider Protocols and their dataclasses (VOICE_PIPELINE §17 is the only spec).

Every model stage is a provider behind one of four Protocols: ``WakeProvider``,
``SpeakerProvider``, ``SttProvider`` and ``TtsProvider``. Inference is
synchronous on worker threads; the two streaming Protocols are async.
``SpeakerIdentifier`` is deliberately *not* a Protocol: it is core logic and
lives in :mod:`beepboop_audio.speaker.identifier`.

No provider may write PCM to disk or keep it beyond the call that received it
(ADR 0018, CLAUDE.md hard rule 4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt

#: int16, mono, 16 kHz (VOICE_PIPELINE §3).
type PCM16 = npt.NDArray[np.int16]
#: float32 samples in [-1, 1] (what sherpa-onnx consumes and produces).
type Float32 = npt.NDArray[np.float32]
#: An L2-normalised float32 speaker embedding of shape ``(dim,)``.
type Embedding = npt.NDArray[np.float32]
#: BCP-47, e.g. ``"en-US"``.
type Locale = str

SttMode = Literal["command", "pin"]
IdentityStage = Literal["provisional", "final"]


@dataclass(frozen=True, slots=True)
class WakeHit:
    model: str
    prob: float
    t_wake_ns: int  # capture stamp of the triggering frame


@dataclass(frozen=True, slots=True)
class Identity:
    profile_id: str  # "guest" (the built-in profile's fixed id) when unrecognized
    score: float  # calibrated confidence of top-1, 0..1
    margin: float  # conf(top1) - conf(top2)
    stage: IdentityStage
    anchor: bool  # True if a wake phrase was in the audio
    candidates: tuple[tuple[str, float], ...]  # top-3 (profile_id, conf) for the presence prior
    embedder: tuple[str, str]  # (model_id, model_sha)


@dataclass(frozen=True, slots=True)
class SttContext:
    locale: Locale
    hotwords: tuple[str, ...]  # already capped and lower-cased
    hotwords_score: float
    mode: SttMode  # pin = digits only, nothing emitted


@dataclass(frozen=True, slots=True)
class Partial:
    text: str
    stable: bool
    t_ns: int


@dataclass(frozen=True, slots=True)
class Final:
    text: str
    confidence: float
    words: tuple[tuple[str, int, int], ...]  # (word, t0_ns, t1_ns)
    t_eos_ns: int
    t_final_ns: int


@dataclass(frozen=True, slots=True)
class VoiceRef:
    voice_id: str  # e.g. "en_US-lessac-low"
    rate: float  # 0.7..1.4
    locale: Locale


class WakeProvider(Protocol):
    id: str
    license: str

    def models(self) -> list[str]: ...

    def process(self, frame: PCM16) -> WakeHit | None:
        """One 10 ms frame, sync, on the capture thread; returns well under 10 ms."""
        ...

    def reset(self) -> None: ...


class SpeakerProvider(Protocol):
    id: str
    license: str
    model_id: str
    model_sha: str
    dim: int

    def embed(self, audio: PCM16) -> Embedding:
        """L2-normalised, shape ``(dim,)``; runs on the speaker threads."""
        ...


class SttProvider(Protocol):
    id: str
    license: str
    streaming: bool
    locales: tuple[Locale, ...]

    def stream(
        self, audio: AsyncIterator[PCM16], ctx: SttContext
    ) -> AsyncIterator[Partial | Final]:
        """Honour ``aclose()`` within 50 ms; emit exactly one ``Final`` (text may be empty)."""
        ...


class TtsProvider(Protocol):
    id: str
    license: str
    locales: tuple[Locale, ...]

    def voices(self) -> list[VoiceRef]: ...

    def synth(self, text: str, voice: VoiceRef) -> AsyncIterator[PCM16]:
        """16 kHz chunks of at most 100 ms, the first as early as the engine allows."""
        ...


__all__ = [
    "PCM16",
    "Embedding",
    "Final",
    "Float32",
    "Identity",
    "IdentityStage",
    "Locale",
    "Partial",
    "SpeakerProvider",
    "SttContext",
    "SttMode",
    "SttProvider",
    "TtsProvider",
    "VoiceRef",
    "WakeHit",
    "WakeProvider",
]
