"""The model manifest JSON (``hey_beepboop_vN.json``) next to the ``.tflite``.

The format is the ESPHome / ``micro-wake-word-models`` v2 manifest. Two consumers
read it:

* ``pymicro-wakeword`` 2.5.0 (``MicroWakeWord.from_config``) reads exactly
  :data:`PYMICRO_WAKEWORD_KEYS`; everything else is ignored by the Pi.
* ESPHome additionally uses ``micro.feature_step_size``, ``micro.tensor_arena_size``
  and ``micro.minimum_esphome_version`` (future satellites, ROADMAP v0.3+).

``probability_cutoff`` and ``sliding_window_size`` are the detection parameters of
VOICE_PIPELINE §6.2; the audio service's config file may override them per model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Keys ``pymicro_wakeword.MicroWakeWord.from_config`` reads (``micro.*`` are nested).
PYMICRO_WAKEWORD_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "wake_word",
        "trained_languages",
        "micro.probability_cutoff",
        "micro.sliding_window_size",
    }
)


class MicroSettings(BaseModel):
    """The ``micro`` block: detection parameters and the ESPHome runtime hints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    probability_cutoff: float = Field(gt=0, lt=1)
    feature_step_size: int = Field(ge=1)
    sliding_window_size: int = Field(ge=1)
    tensor_arena_size: int = Field(ge=1)
    minimum_esphome_version: str = Field(pattern=r"^\d{4}\.\d{1,2}\.\d+$")


class Manifest(BaseModel):
    """A v2 microWakeWord model manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["micro"] = "micro"
    wake_word: str = Field(min_length=1)
    author: str = Field(min_length=1)
    website: str = Field(min_length=1)
    model: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.tflite$")
    trained_languages: list[str] = Field(min_length=1)
    version: Literal[2] = 2
    micro: MicroSettings

    @model_validator(mode="after")
    def _model_is_bare_filename(self) -> Self:
        if "/" in self.model or "\\" in self.model:
            msg = "manifest.model must be a bare file name next to the manifest"
            raise ValueError(msg)
        return self

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """Read and validate a manifest file."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def dump(self, path: Path) -> None:
        """Write the manifest with stable key order and a trailing newline."""
        path.write_text(json.dumps(self.model_dump(), indent=2) + "\n", encoding="utf-8")

    def flat_keys(self) -> frozenset[str]:
        """Dotted key set, for checking against :data:`PYMICRO_WAKEWORD_KEYS`."""
        top = set(type(self).model_fields) - {"micro"}
        nested = {f"micro.{k}" for k in MicroSettings.model_fields}
        return frozenset(top | nested)
