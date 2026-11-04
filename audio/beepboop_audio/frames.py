"""Frames, the capture clock and the RAM-only ring buffer (VOICE_PIPELINE §3).

Every PCM frame carries the ``time.monotonic_ns()`` stamp taken at the capture
callback; every event stamp (``t_wake``, ``t_eos`` ...) is one of these. The
ring buffer lives in process memory only and is wiped (zeroed, not just
forgotten) on mute and at the end of every turn (ADR 0018).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from beepboop_audio.types import PCM16, Float32, WakeHit

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 160  # 10 ms
FRAME_MS = 10
FRAME_NS = 10_000_000
RING_SECONDS = 3.0
PREROLL_MS = 1200
INT16_MAX = 32767


def ms_to_samples(ms: int) -> int:
    return ms * SAMPLE_RATE // 1000


def samples_to_ms(n: int) -> int:
    return n * 1000 // SAMPLE_RATE


@dataclass(frozen=True, slots=True)
class Frame:
    """One 10 ms capture frame as it leaves the capture thread.

    ``speech`` is the latest Silero decision covering this frame and ``wake``
    the hit (if any) the wake provider raised on it; both are computed on the
    capture thread by :class:`~beepboop_audio.pipeline.frontend.FrontEnd`.
    """

    pcm: PCM16
    t_ns: int
    speech: bool = False
    speech_prob: float = 0.0
    wake: WakeHit | None = None


def to_float32(pcm: PCM16) -> Float32:
    """int16 -> float32 in [-1, 1), the form sherpa-onnx wants."""
    return (pcm.astype(np.float32) / 32768.0).reshape(-1)


def to_int16(samples: Float32) -> PCM16:
    """float32 in [-1, 1] -> int16, clipped."""
    clipped = np.clip(samples, -1.0, 1.0) * INT16_MAX
    return clipped.astype(np.int16)


def concat(chunks: list[PCM16]) -> PCM16:
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks).astype(np.int16, copy=False)


def wipe(buf: PCM16) -> None:
    """Zero a PCM buffer in place (used when a turn ends; no PCM outlives its turn)."""
    buf.fill(0)


class RingBuffer:
    """Fixed-capacity ring of the most recent PCM samples, RAM only.

    Not thread-safe by itself: the capture thread is the only writer and
    readers copy out with :meth:`last` from the same thread (the front end)
    or after handing over through the frame queue.
    """

    def __init__(self, seconds: float = RING_SECONDS) -> None:
        self.capacity = int(seconds * SAMPLE_RATE)
        self._buf: PCM16 = np.zeros(self.capacity, dtype=np.int16)
        self._write = 0
        self._filled = 0

    def __len__(self) -> int:
        return self._filled

    def push(self, pcm: PCM16) -> None:
        data = pcm.reshape(-1)
        n = len(data)
        if n >= self.capacity:
            self._buf[:] = data[-self.capacity :]
            self._write = 0
            self._filled = self.capacity
            return
        end = self._write + n
        if end <= self.capacity:
            self._buf[self._write : end] = data
        else:
            first = self.capacity - self._write
            self._buf[self._write :] = data[:first]
            self._buf[: n - first] = data[first:]
        self._write = end % self.capacity
        self._filled = min(self.capacity, self._filled + n)

    def last(self, n_samples: int) -> PCM16:
        """A copy of the newest ``n_samples`` (fewer if the ring is not that full)."""
        n = min(n_samples, self._filled)
        if n <= 0:
            return np.zeros(0, dtype=np.int16)
        start = (self._write - n) % self.capacity
        if start + n <= self.capacity:
            return self._buf[start : start + n].copy()
        return np.concatenate([self._buf[start:], self._buf[: (start + n) % self.capacity]])

    def clear(self) -> None:
        """Zero the storage and forget every sample (mute, end of turn)."""
        self._buf.fill(0)
        self._write = 0
        self._filled = 0


__all__ = [
    "FRAME_MS",
    "FRAME_NS",
    "FRAME_SAMPLES",
    "PREROLL_MS",
    "RING_SECONDS",
    "SAMPLE_RATE",
    "Frame",
    "RingBuffer",
    "concat",
    "ms_to_samples",
    "samples_to_ms",
    "to_float32",
    "to_int16",
    "wipe",
]
