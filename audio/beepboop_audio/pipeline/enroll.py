"""Enrollment clips and their quality gates (VOICE_PIPELINE §9.2).

The browser drives; the Pi's own array records. Each clip is gated and, if
accepted, embedded; the PCM is wiped as soon as that is done and only the
embedding and the quality numbers cross to core in ``enroll.clip``. Gates
(targets):

| Gate | Value | Re-prompt (spoken by the UI) |
|---|---|---|
| wake model fired on the clip (``mode: wake``) | required | "Say Hey beepboop …" |
| speech (``wake``) / duration (``free``) | ≥ 0.6 s / ≥ 1 s | "I didn't catch that" |
| SNR against the 500 ms before the clip | ≥ 15 dB | "It's a bit noisy …" |
| clipped samples | < 0.1 % | "A little quieter …" |
| VAD speech ratio | ≥ 0.6 | "I didn't catch that" |
| cosine to the running mean | ≥ 0.55, or 2nd try in a row | "That sounded different …" |
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from beepboop_audio.frames import samples_to_ms
from beepboop_audio.types import PCM16, Embedding

MIN_SNR_DB = 15.0
MAX_CLIPPING = 0.001
MIN_SPEECH_RATIO = 0.6
MIN_WAKE_SPEECH_MS = 600
MIN_FREE_MS = 1000
MIN_COSINE = 0.55
CLIP_LEVEL = 32767 * 0.999
SNR_CAP_DB = 99.0

RejectReason = Literal[
    "no_wake", "too_short", "noisy", "clipping", "no_speech", "different", "embed_failed"
]


@dataclass(frozen=True, slots=True)
class Quality:
    snr_db: float
    clipping: float
    speech_ratio: float
    duration_ms: int
    speech_ms: int


def _power(x: PCM16) -> float:
    if not len(x):
        return 0.0
    f = x.astype(np.float64)
    return float(np.mean(f * f))


def measure(clip: PCM16, noise: PCM16, speech_frames: int, total_frames: int) -> Quality:
    p_clip = _power(clip)
    p_noise = _power(noise)
    if p_noise <= 0:
        snr = SNR_CAP_DB
    elif p_clip <= p_noise:
        snr = 0.0
    else:
        snr = min(SNR_CAP_DB, 10.0 * math.log10((p_clip - p_noise) / p_noise))
    clipped = float(np.mean(np.abs(clip.astype(np.int32)) >= CLIP_LEVEL)) if len(clip) else 0.0
    ratio = speech_frames / total_frames if total_frames else 0.0
    return Quality(
        snr_db=round(snr, 2),
        clipping=round(clipped, 6),
        speech_ratio=round(min(1.0, ratio), 4),
        duration_ms=samples_to_ms(len(clip)),
        speech_ms=speech_frames * 10,
    )


def gate(
    quality: Quality, *, mode: Literal["wake", "free"], wake_fired: bool
) -> RejectReason | None:
    """The first failing gate (before embedding), or None."""
    if mode == "wake":
        if not wake_fired:
            return "no_wake"
        if quality.speech_ms < MIN_WAKE_SPEECH_MS:
            return "too_short"
    elif quality.duration_ms < MIN_FREE_MS:
        return "too_short"
    if quality.snr_db < MIN_SNR_DB:
        return "noisy"
    if quality.clipping >= MAX_CLIPPING:
        return "clipping"
    if mode == "free" and quality.speech_ratio < MIN_SPEECH_RATIO:
        return "no_speech"
    return None


@dataclass
class EnrollmentSession:
    session_id: str
    profile_id: str
    mode: Literal["wake", "free"]
    index: int = 0
    accepted: list[Embedding] = field(default_factory=list[Embedding])
    _different_once: bool = False

    def consistency(self, emb: Embedding) -> RejectReason | None:
        """The running-mean cosine gate; a second dissimilar clip in a row is accepted."""
        if not self.accepted:
            return None
        mean = np.mean(np.stack(self.accepted), axis=0)
        norm = float(np.linalg.norm(mean)) * float(np.linalg.norm(emb))
        cos = float(np.dot(mean, emb) / norm) if norm > 0 else 0.0
        if cos >= MIN_COSINE:
            self._different_once = False
            return None
        if self._different_once:
            self._different_once = False
            return None
        self._different_once = True
        return "different"

    def next_index(self) -> int:
        i = self.index
        self.index += 1
        return i


__all__ = [
    "MIN_COSINE",
    "MIN_SNR_DB",
    "EnrollmentSession",
    "Quality",
    "RejectReason",
    "gate",
    "measure",
]
