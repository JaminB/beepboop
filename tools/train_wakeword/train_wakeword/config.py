"""Pydantic schema for ``config/hey_beepboop.yaml``.

Every key the six steps read is declared here, with ``extra="forbid"`` so a typo in
the YAML fails ``train-wakeword check`` instead of silently using a default. The
``shipped`` flags and ``license_id`` keys are what ties the config to
``DATA_LICENSES.md`` (see :mod:`train_wakeword.licenses`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Augmentation names understood by ``microwakeword.audio.augmentation.Augmentation``.
AUGMENTATIONS: frozenset[str] = frozenset(
    {
        "SevenBandParametricEQ",
        "TanhDistortion",
        "PitchShift",
        "BandStopFilter",
        "AddColorNoise",
        "AddBackgroundNoise",
        "Gain",
        "GainTransition",
        "RIR",
    }
)

#: Validation metrics the trainer exposes for ``minimization_metric`` / ``maximization_metric``.
TRAINER_METRICS: frozenset[str] = frozenset(
    {
        "accuracy",
        "recall",
        "precision",
        "auc",
        "loss",
        "recall_at_no_faph",
        "cutoff_for_no_faph",
        "ambient_false_positives",
        "ambient_false_positives_per_hour",
        "average_viable_recall",
    }
)

Role = Literal["speech", "no_speech", "ambient_eval", "rir", "background"]
Profile = Literal["shipped", "research"]


class StrictModel(BaseModel):
    """Base: unknown keys are errors, instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Phrase(StrictModel):
    """One spelling of the wake phrase, as text (espeak phonemizes) or as IPA phonemes."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    text: str | None = None
    phonemes: str | None = None
    weight: float = Field(1.0, gt=0)

    @model_validator(mode="after")
    def _exactly_one_form(self) -> Self:
        if (self.text is None) == (self.phonemes is None):
            msg = f"phrase {self.id!r}: give exactly one of `text` or `phonemes`"
            raise ValueError(msg)
        return self


class Voice(StrictModel):
    """A Piper voice (``.onnx``) or the multi-speaker generator checkpoint (``.pt``)."""

    id: str
    kind: Literal["generator", "piper"]
    url: str
    license_id: str
    max_speakers: int | None = Field(None, ge=1)
    shipped: bool
    note: str = ""

    @property
    def filename(self) -> str:
        """Local file name: generator ids already carry ``.pt``; Piper voices get ``.onnx``."""
        return self.id if self.kind == "generator" else f"{self.id}.onnx"


class PositivesConfig(StrictModel):
    """Step 01: synthetic positives and TTS near-misses."""

    target_count: int = Field(ge=1)
    holdout_fraction: float = Field(0.1, gt=0, lt=0.5)
    #: Share of clips made by the multi-speaker generator; the rest cycle the Piper voices.
    generator_share: float = Field(0.7, ge=0, le=1)
    seed: int = 20261101
    batch_size: int = Field(32, ge=1)
    length_scales: list[float] = Field(min_length=1)
    noise_scales: list[float] = Field(min_length=1)
    noise_scale_ws: list[float] = Field(min_length=1)
    phrases: list[Phrase] = Field(min_length=1)
    voices: list[Voice] = Field(min_length=1)
    near_miss_count: int = Field(ge=0)
    near_misses: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [p.id for p in self.phrases] + [v.id for v in self.voices]
        if len(ids) != len(set(ids)):
            msg = "phrase and voice ids must be unique"
            raise ValueError(msg)
        return self


class Dataset(StrictModel):
    """A downloadable (or manually fetched) corpus and the role it plays in training."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    role: Role
    license_id: str
    shipped: bool
    archive: str
    url: str | None = None
    manual: bool = False
    sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    #: Glob patterns (relative to the extracted archive root) selecting the audio to convert.
    include: list[str] = Field(default_factory=lambda: ["**/*.wav", "**/*.flac"])
    exclude: list[str] = Field(default_factory=list)
    clip_filter: Literal["none", "fsd50k_permissive"] = "none"
    max_hours: float | None = Field(None, gt=0)
    note: str = ""

    @model_validator(mode="after")
    def _url_or_manual(self) -> Self:
        if self.manual == (self.url is not None):
            msg = f"dataset {self.id!r}: give `url` or set `manual: true`, not both"
            raise ValueError(msg)
        return self


class AugmentConfig(StrictModel):
    """Step 03: augmentation parameters, mirroring ``Augmentation.__init__``."""

    duration_s: float = Field(3.2, gt=0)
    probabilities: dict[str, float]
    background_min_snr_db: int
    background_max_snr_db: int
    min_gain_db: float
    max_gain_db: float
    min_jitter_s: float = Field(ge=0)
    max_jitter_s: float = Field(ge=0)
    array_ir_dir: str | None = None
    workers: int = Field(8, ge=1)

    @model_validator(mode="after")
    def _known_augmentations(self) -> Self:
        unknown = set(self.probabilities) - AUGMENTATIONS
        if unknown:
            msg = f"unknown augmentations: {sorted(unknown)}"
            raise ValueError(msg)
        if any(not 0 <= p <= 1 for p in self.probabilities.values()):
            msg = "augmentation probabilities must be within [0, 1]"
            raise ValueError(msg)
        if self.background_min_snr_db > self.background_max_snr_db:
            msg = "background_min_snr_db must be <= background_max_snr_db"
            raise ValueError(msg)
        if self.min_jitter_s > self.max_jitter_s:
            msg = "min_jitter_s must be <= max_jitter_s"
            raise ValueError(msg)
        return self


class MixedNetConfig(StrictModel):
    """Flags for ``model_train_eval.py mixednet``; one list entry per MixConv block."""

    pointwise_filters: list[int] = Field(min_length=1)
    repeat_in_block: list[int] = Field(min_length=1)
    mixconv_kernel_sizes: list[list[int]] = Field(min_length=1)
    residual_connection: list[int] = Field(min_length=1)
    first_conv_filters: int = Field(ge=0)
    first_conv_kernel_size: int = Field(ge=1)
    stride: int = Field(ge=1)

    @model_validator(mode="after")
    def _same_block_count(self) -> Self:
        lengths = {
            len(self.pointwise_filters),
            len(self.repeat_in_block),
            len(self.mixconv_kernel_sizes),
            len(self.residual_connection),
        }
        if len(lengths) != 1:
            msg = "mixednet block lists must all have the same length"
            raise ValueError(msg)
        return self

    def cli_args(self) -> list[str]:
        """The argv tail for the trainer's ``mixednet`` subparser."""
        kernels = ", ".join(
            "[" + ",".join(str(k) for k in ks) + "]" for ks in self.mixconv_kernel_sizes
        )
        return [
            "mixednet",
            "--pointwise_filters",
            ",".join(str(f) for f in self.pointwise_filters),
            "--repeat_in_block",
            ",".join(str(r) for r in self.repeat_in_block),
            "--mixconv_kernel_sizes",
            kernels,
            "--residual_connection",
            ",".join(str(r) for r in self.residual_connection),
            "--first_conv_filters",
            str(self.first_conv_filters),
            "--first_conv_kernel_size",
            str(self.first_conv_kernel_size),
            "--stride",
            str(self.stride),
        ]


class FeatureSetWeights(StrictModel):
    """Per feature-set oversampling and loss weights (trainer ``features[]`` entries)."""

    sampling_weight: float = Field(ge=0)
    penalty_weight: float = Field(ge=0)


FeatureSet = Literal["positives", "near_misses", "speech", "no_speech"]


class TrainingConfig(StrictModel):
    """Step 04: the trainer's YAML, typed. Lists are per training stage."""

    window_step_ms: int = Field(10, ge=1)
    clip_duration_ms: int = Field(ge=100)
    batch_size: int = Field(ge=1)
    training_steps: list[int] = Field(min_length=1)
    learning_rates: list[float] = Field(min_length=1)
    positive_class_weight: list[float] = Field(min_length=1)
    negative_class_weight: list[float] = Field(min_length=1)
    time_mask_max_size: list[int] = Field(min_length=1)
    time_mask_count: list[int] = Field(min_length=1)
    freq_mask_max_size: list[int] = Field(min_length=1)
    freq_mask_count: list[int] = Field(min_length=1)
    eval_step_interval: int = Field(ge=1)
    minimization_metric: str | None
    target_minimization: float
    maximization_metric: str
    weights: dict[FeatureSet, FeatureSetWeights]
    model: MixedNetConfig

    @model_validator(mode="after")
    def _metrics_and_weights(self) -> Self:
        for name in (self.minimization_metric, self.maximization_metric):
            if name is not None and name not in TRAINER_METRICS:
                msg = f"unknown trainer metric {name!r}"
                raise ValueError(msg)
        required: set[FeatureSet] = {"positives", "near_misses", "speech", "no_speech"}
        missing = required - set(self.weights)
        if missing:
            msg = f"training.weights is missing {sorted(missing)}"
            raise ValueError(msg)
        return self


class ExportConfig(StrictModel):
    """Step 05: manifest fields that are not derived from the training run."""

    author: str
    website: str
    trained_languages: list[str] = Field(min_length=1)
    probability_cutoff: float = Field(gt=0, lt=1)
    sliding_window_size: int = Field(ge=1)
    feature_step_size: int = Field(ge=1)
    tensor_arena_size: int = Field(ge=1)
    minimum_esphome_version: str


class EvalConfig(StrictModel):
    """Step 06: the sweep and the pass/fail targets (G5 expressed per hour)."""

    cutoffs: list[float] = Field(min_length=1)
    sliding_window_sizes: list[int] = Field(min_length=1)
    refractory_ms: int = Field(ge=0)
    max_false_accepts_per_hour: float = Field(ge=0)
    max_false_reject_rate: float = Field(ge=0, le=1)


class PipelineConfig(StrictModel):
    """The whole ``config/hey_beepboop.yaml``."""

    wake_word: str
    model_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)
    positives: PositivesConfig
    datasets: list[Dataset] = Field(min_length=1)
    augment: AugmentConfig
    training: TrainingConfig
    export: ExportConfig
    evaluate: EvalConfig

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [d.id for d in self.datasets]
        if len(ids) != len(set(ids)):
            msg = "dataset ids must be unique"
            raise ValueError(msg)
        if self.export.feature_step_size != self.training.window_step_ms:
            msg = "export.feature_step_size must equal training.window_step_ms"
            raise ValueError(msg)
        if not any(d.role == "ambient_eval" for d in self.datasets):
            msg = "at least one dataset must have role ambient_eval"
            raise ValueError(msg)
        return self

    @classmethod
    def load(cls, path: Path) -> PipelineConfig:
        """Parse and validate a YAML config file."""
        with path.open(encoding="utf-8") as fh:
            raw: object = yaml.safe_load(fh)
        return cls.model_validate(raw)

    @property
    def model_name(self) -> str:
        """``hey_beepboop_v1``: the stem of the exported ``.tflite`` and ``.json``."""
        return f"{self.model_id}_v{self.version}"

    def voices(self, profile: Profile) -> list[Voice]:
        """Voices usable under ``profile`` (``shipped`` drops every non-shipped voice)."""
        return [v for v in self.positives.voices if profile == "research" or v.shipped]

    def datasets_for(self, profile: Profile, role: Role | None = None) -> list[Dataset]:
        """Datasets usable under ``profile``, optionally filtered by role."""
        return [
            d
            for d in self.datasets
            if (profile == "research" or d.shipped) and (role is None or d.role == role)
        ]
