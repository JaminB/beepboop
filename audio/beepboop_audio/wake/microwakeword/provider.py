"""``WakeProvider`` for microWakeWord models (VOICE_PIPELINE §6).

A streaming MixConv TFLite network over 40-bin mel features at a 10 ms stride,
evaluated every 30 ms, synchronously on the capture thread. The model's own
sliding window is set to 1 so that :class:`~beepboop_audio.wake.decider.WakeDecider`
applies the configured window, cutoff and refractory period.

Models resolve in this order: an explicit ``path`` to a microWakeWord JSON
config; the ``hey_beepboop`` entry of ``models/manifest.yaml``; a
``pymicro-wakeword`` builtin (``okay_nabu`` …, for development only, never a G5
result).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymicro_wakeword
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures

from beepboop_audio.config import WakeModelConfig
from beepboop_audio.models import WAKE_HEY_BEEPBOOP, ModelMissingError, ModelStore
from beepboop_audio.types import PCM16, WakeHit
from beepboop_audio.wake.decider import WakeDecider

_BUILTINS = {m.value: m for m in pymicro_wakeword.Model}


def _tflite_lib() -> Path | None:
    lib_dir = Path(pymicro_wakeword.__file__).parent / "lib"
    return next(iter(sorted(lib_dir.glob("*tensorflowlite_c.*"))), None)


def resolve_config_json(model: WakeModelConfig, store: ModelStore | None) -> Path:
    """The microWakeWord JSON config for one configured model."""
    if model.path is not None:
        path = Path(model.path)
        if path.suffix == ".tflite":
            path = path.with_suffix(".json")
        if not path.is_file():
            raise ModelMissingError(f"wake model config {path} not found")
        return path
    if model.id in _BUILTINS:
        return Path(pymicro_wakeword.__file__).parent / "models" / f"{model.id}.json"
    if model.id == WAKE_HEY_BEEPBOOP and store is not None:
        return store.get(WAKE_HEY_BEEPBOOP).file("config")
    raise ModelMissingError(
        f"wake model {model.id!r} is neither installed nor a pymicro-wakeword builtin"
    )


@dataclass
class _Loaded:
    id: str
    model: MicroWakeWord
    features: MicroWakeWordFeatures
    decider: WakeDecider
    calls: int = 0


def _load(model: WakeModelConfig, store: ModelStore | None) -> _Loaded:
    config_path = resolve_config_json(model, store)
    lib = _tflite_lib()
    if lib is None:  # pragma: no cover - the wheel always bundles the library
        mww = MicroWakeWord.from_config(config_path)
    else:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        mww = MicroWakeWord(
            id=model.id,
            wake_word=str(raw.get("wake_word", model.id)),
            tflite_model=config_path.parent / str(raw["model"]),
            probability_cutoff=model.probability_cutoff,
            sliding_window_size=1,  # the decider owns the window
            trained_languages=[str(x) for x in raw.get("trained_languages", [])],
            libtensorflowlite_c_path=lib,
        )
    decider = WakeDecider(
        probability_cutoff=model.probability_cutoff,
        sliding_window_size=model.sliding_window_size,
        refractory_ms=model.refractory_ms,
    )
    return _Loaded(model.id, mww, MicroWakeWordFeatures(), decider)


class MicroWakeWordProvider:
    """One or more microWakeWord models evaluated on every 10 ms frame."""

    id = "microwakeword"
    license = "Apache-2.0"

    def __init__(self, models: list[WakeModelConfig], store: ModelStore | None = None) -> None:
        if not models:
            raise ValueError("at least one wake model is required")
        self._loaded = [_load(m, store) for m in models]

    def models(self) -> list[str]:
        return [m.id for m in self._loaded]

    def process(self, frame: PCM16) -> WakeHit | None:
        pcm = np.ascontiguousarray(frame, dtype=np.int16).tobytes()
        now = time.monotonic_ns()
        hit: WakeHit | None = None
        for loaded in self._loaded:
            for features in loaded.features.process_streaming(pcm):
                prob = loaded.model.process_streaming_prob(features)
                loaded.calls += 1
                if loaded.calls % max(1, int(loaded.model.stride)) != 0 or prob is None:
                    continue
                mean = loaded.decider.update(float(prob), now)
                if mean is not None and hit is None:
                    hit = WakeHit(model=loaded.id, prob=min(1.0, mean), t_wake_ns=now)
        return hit

    def reset(self) -> None:
        for loaded in self._loaded:
            loaded.model.reset()
            loaded.features.reset()
            loaded.decider.reset()
            loaded.calls = 0


__all__ = ["MicroWakeWordProvider", "resolve_config_json"]
