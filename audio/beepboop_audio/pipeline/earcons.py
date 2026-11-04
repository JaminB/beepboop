"""Earcons (VOICE_PIPELINE §13.1): sine tones generated in-process, -12 dBFS, ≤ 250 ms, 16 kHz.

The name list is owned by VOICE_PIPELINE §13.1 (``EarconName`` in the
protocol). The tones are synthesised at start-up from the table below, so there
are no asset files and no licence question. A household theme replaces any of
them with ``<theme_dir>/<name>.wav`` (read-only; resampled to 16 kHz mono).
``alarm`` is a 1 s pattern the alert lane loops.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import get_args

import numpy as np
import structlog

from beepboop_audio._audiofile import read_audio, resample
from beepboop_audio.frames import SAMPLE_RATE, to_int16
from beepboop_audio.types import PCM16, Float32
from beepboop_protocol import EarconName

log = structlog.get_logger(__name__)

EARCON_NAMES: tuple[str, ...] = get_args(EarconName)
LEVEL_DBFS = -12.0
SOFT_DBFS = -18.0
FADE_MS = 5

#: (frequency Hz or 0 for a gap, duration ms); durations sum to the §13.1 lengths.
_TONES: dict[str, tuple[tuple[float, int], ...]] = {
    "wake": ((660.0, 60), (880.0, 60)),
    "ack": ((880.0, 80),),
    "done": ((880.0, 50), (660.0, 50)),
    "prompt": ((660.0, 80),),
    "cancel": ((880.0, 60), (587.0, 60)),
    "error": ((220.0, 70), (0.0, 10), (220.0, 70)),
    "offline": ((196.0, 200),),
    "pin": ((523.0, 80),),
    "mute": ((440.0, 50), (330.0, 50)),
    "unmute": ((330.0, 50), (440.0, 50)),
    "alarm": ((988.0, 150), (0.0, 100), (988.0, 150), (0.0, 600)),
    "ready": ((523.0, 80), (659.0, 80), (784.0, 90)),
    "too_long": ((784.0, 66), (659.0, 66), (523.0, 68)),
}
_SOFT = frozenset({"done", "prompt"})


def _tone(freq: float, ms: int, level_dbfs: float) -> Float32:
    n = ms * SAMPLE_RATE // 1000
    if freq <= 0:
        return np.zeros(n, dtype=np.float32)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    amp = 10.0 ** (level_dbfs / 20.0)
    wave = (amp * np.sin(2.0 * math.pi * freq * t)).astype(np.float32)
    fade = min(n // 2, FADE_MS * SAMPLE_RATE // 1000)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        wave[:fade] *= ramp
        wave[-fade:] *= ramp[::-1]
    return wave


def synthesize(name: str) -> PCM16:
    tones = _TONES[name]
    level = SOFT_DBFS if name in _SOFT else LEVEL_DBFS
    return to_int16(np.concatenate([_tone(f, ms, level) for f, ms in tones]))


def _load_theme(path: Path) -> PCM16:
    data, rate = read_audio(path)
    mono = np.asarray(data.mean(axis=1), dtype=np.float32)
    return to_int16(resample(mono, rate, SAMPLE_RATE))


class Earcons:
    """Every earcon as int16 PCM, built once."""

    def __init__(self, theme_dir: Path | None = None) -> None:
        self._pcm: dict[str, PCM16] = {name: synthesize(name) for name in EARCON_NAMES}
        if theme_dir is not None and theme_dir.is_dir():
            for name in EARCON_NAMES:
                path = theme_dir / f"{name}.wav"
                if path.is_file():
                    try:
                        self._pcm[name] = _load_theme(path)
                    except (OSError, RuntimeError, ValueError) as exc:
                        log.warning("earcon.theme_invalid", name=name, error=str(exc))

    def __contains__(self, name: object) -> bool:
        return name in self._pcm

    def get(self, name: str) -> PCM16:
        return self._pcm[name]


__all__ = ["EARCON_NAMES", "Earcons", "synthesize"]
