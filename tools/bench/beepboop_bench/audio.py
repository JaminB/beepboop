"""WAV in/out and small PCM helpers for the bench (16 kHz mono float32).

Bench fixtures are the one place beepboop ever writes audio to disk, by the
person running the bench on their own machine (VOICE_PIPELINE §16). The
product path never imports this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import numpy as np
import soundfile as sf  # pyright: ignore[reportMissingTypeStubs] no stubs upstream
from numpy.typing import NDArray

SAMPLE_RATE = 16000
FRAME_MS = 10
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

Float32Array = NDArray[np.float32]


def read_wav(path: Path, *, channel: int = 0, sample_rate: int = SAMPLE_RATE) -> Float32Array:
    """Read one channel as float32 in [-1, 1] at ``sample_rate`` (linear resample if needed)."""
    data, rate = cast(
        "tuple[NDArray[np.float32], int]",
        sf.read(str(path), dtype="float32", always_2d=True),
    )
    samples: Float32Array = np.ascontiguousarray(data[:, channel], dtype=np.float32)
    if rate != sample_rate:
        samples = resample(samples, int(rate), sample_rate)
    return samples


def write_wav(path: Path, samples: Float32Array, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(samples, -1.0, 1.0), sample_rate, subtype="PCM_16")


def resample(samples: Float32Array, src_rate: int, dst_rate: int) -> Float32Array:
    """Linear-interpolation resample; good enough for bench fixtures, not for the product."""
    if src_rate == dst_rate:
        return samples
    n_out = round(len(samples) * dst_rate / src_rate)
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def to_pcm16(samples: Float32Array) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def silence(seconds: float, sample_rate: int = SAMPLE_RATE) -> Float32Array:
    return np.zeros(int(seconds * sample_rate), dtype=np.float32)


def duration_s(samples: Float32Array, sample_rate: int = SAMPLE_RATE) -> float:
    return len(samples) / sample_rate


def frames(samples: Float32Array, frame: int = FRAME_SAMPLES) -> list[Float32Array]:
    """Split into ``frame``-sample chunks; the last one is zero-padded."""
    n = len(samples)
    out: list[Float32Array] = []
    for start in range(0, n, frame):
        chunk = samples[start : start + frame]
        if len(chunk) < frame:
            chunk = np.concatenate([chunk, np.zeros(frame - len(chunk), dtype=np.float32)])
        out.append(chunk)
    return out


def last_seconds(
    samples: Float32Array, seconds: float, sample_rate: int = SAMPLE_RATE
) -> Float32Array:
    """The trailing ``seconds`` of audio, zero-padded at the front if the clip is shorter."""
    n = int(seconds * sample_rate)
    if len(samples) >= n:
        return samples[-n:]
    return np.concatenate([np.zeros(n - len(samples), dtype=np.float32), samples])


def trim_trailing_silence(
    samples: Float32Array, threshold: float = 0.01, keep_s: float = 0.0
) -> Float32Array:
    """Cut trailing samples below ``threshold`` absolute amplitude (plus ``keep_s`` of tail)."""
    above = np.flatnonzero(np.abs(samples) > threshold)
    if len(above) == 0:
        return samples[:0]
    end = int(above[-1]) + 1 + int(keep_s * SAMPLE_RATE)
    return samples[: min(end, len(samples))]
