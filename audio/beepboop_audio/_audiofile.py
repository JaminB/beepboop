"""Typed shims over ``soundfile`` (read-only) and ``soxr``, the only module that imports them.

Both libraries ship without type information; this file alone relaxes pyright's
unknown-type rules. Only *reading* is exposed: nothing in ``beepboop_audio``
may write an audio file (ADR 0018; a test greps the package for writers).
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# Reason: soundfile and soxr are untyped; this is the one shim module.

from __future__ import annotations

from pathlib import Path

import numpy as np
import numpy.typing as npt
import soundfile
import soxr

from beepboop_audio.types import Float32

type Float32Frames = npt.NDArray[np.float32]  # (n, channels)


def read_audio(path: Path) -> tuple[Float32Frames, int]:
    """``(samples[n, channels] float32, sample_rate)`` from a WAV/FLAC file."""
    data, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
    return np.asarray(data, dtype=np.float32), int(rate)


def resample(samples: Float32, rate_in: int, rate_out: int) -> Float32:
    if rate_in == rate_out:
        return samples
    return np.asarray(soxr.resample(samples, rate_in, rate_out), dtype=np.float32)


class Resampler:
    """Streaming ``soxr`` resampler (float32 or int16, mono)."""

    def __init__(self, rate_in: int, rate_out: int, dtype: str = "float32") -> None:
        self._stream = soxr.ResampleStream(rate_in, rate_out, 1, dtype=dtype)
        self._dtype = np.float32 if dtype == "float32" else np.int16

    def chunk(
        self, samples: npt.NDArray[np.generic], *, last: bool = False
    ) -> npt.NDArray[np.generic]:
        out = self._stream.resample_chunk(samples, last=last)
        return np.asarray(out, dtype=self._dtype)


__all__ = ["Float32Frames", "Resampler", "read_audio", "resample"]
