"""Silero VAD on the capture thread (VOICE_PIPELINE §7.1).

A fixed internal component, not a provider. Silero consumes 32 ms (512-sample)
windows; capture produces 10 ms frames, so :class:`VadFramer` re-blocks the
frames and reports, for every frame, the latest window decision. Speech
threshold 0.5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np

from beepboop_audio.frames import to_float32
from beepboop_audio.types import PCM16, Float32

WINDOW = 512


class VadModel(Protocol):
    """One 512-sample float32 window in, speech decision out."""

    def window(self, samples: Float32) -> bool: ...

    def reset(self) -> None: ...


class SileroVad:
    """Silero through sherpa-onnx (``models/vad/silero_vad.onnx``)."""

    def __init__(self, model_path: Path, *, threshold: float = 0.5) -> None:
        from beepboop_audio._sherpa import SherpaSilero

        self._impl = SherpaSilero(model_path, threshold=threshold)

    def window(self, samples: Float32) -> bool:
        return self._impl.window(samples)

    def reset(self) -> None:
        self._impl.reset()


class VadFramer:
    """10 ms frames in, one speech flag per frame out (the newest 32 ms window's)."""

    def __init__(self, model: VadModel) -> None:
        self._model = model
        self._pending: Float32 = np.zeros(0, dtype=np.float32)
        self._speech = False

    @property
    def speech(self) -> bool:
        return self._speech

    def push(self, pcm: PCM16) -> bool:
        buf = to_float32(pcm)
        if len(self._pending):
            buf = np.concatenate([self._pending, buf])
        n_full = len(buf) // WINDOW
        for i in range(n_full):
            self._speech = self._model.window(buf[i * WINDOW : (i + 1) * WINDOW])
        self._pending = buf[n_full * WINDOW :].copy()
        return self._speech

    def reset(self) -> None:
        self._model.reset()
        self._pending = np.zeros(0, dtype=np.float32)
        self._speech = False


__all__ = ["WINDOW", "SileroVad", "VadFramer", "VadModel"]
