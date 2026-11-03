"""Step 04: write the microWakeWord trainer YAML and run ``model_train_eval``.

The trainer reads one YAML (``training_parameters.yaml``) and takes the model
architecture on the command line. Both are derived from ``config/hey_beepboop.yaml``
so the run is reproducible from this repository alone; the generated YAML is also
copied into the export's provenance file.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TypedDict

import yaml

from train_wakeword.config import FeatureSet, PipelineConfig
from train_wakeword.log import get_logger
from train_wakeword.paths import Layout

log = get_logger(__name__)

#: ``truncation_strategy`` per feature set: positives are aligned to the window end,
#: negatives are sampled anywhere, ambient streams are slid over (``split``).
TRUNCATION: dict[FeatureSet, str] = {
    "positives": "truncate_start",
    "near_misses": "truncate_start",
    "speech": "random",
    "no_speech": "random",
}


class FeatureEntry(TypedDict):
    """One ``features[]`` item as ``microwakeword.data.FeatureHandler`` reads it."""

    features_dir: str
    sampling_weight: float
    penalty_weight: float
    truth: bool
    truncation_strategy: str
    type: str


class TrainerYaml(TypedDict):
    """``training_parameters.yaml`` as ``microwakeword.train`` / ``model_train_eval`` read it."""

    window_step_ms: int
    train_dir: str
    features: list[FeatureEntry]
    training_steps: list[int]
    positive_class_weight: list[float]
    negative_class_weight: list[float]
    learning_rates: list[float]
    batch_size: int
    time_mask_max_size: list[int]
    time_mask_count: list[int]
    freq_mask_max_size: list[int]
    freq_mask_count: list[int]
    eval_step_interval: int
    clip_duration_ms: int
    target_minimization: float
    minimization_metric: str | None
    maximization_metric: str


def feature_entries(cfg: PipelineConfig, layout: Layout) -> list[FeatureEntry]:
    """The ``features`` list of the trainer YAML (only sets that exist on disk)."""
    entries: list[FeatureEntry] = []
    for name, weights in cfg.training.weights.items():
        features_dir = layout.feature_set(name)
        if not features_dir.is_dir():
            log.warning("train.feature_set_missing", set=name, path=str(features_dir))
            continue
        entries.append(
            FeatureEntry(
                features_dir=str(features_dir),
                sampling_weight=weights.sampling_weight,
                penalty_weight=weights.penalty_weight,
                truth=name == "positives",
                truncation_strategy=TRUNCATION[name],
                type="mmap",
            )
        )
    ambient = layout.feature_set("ambient")
    if ambient.is_dir():
        entries.append(
            FeatureEntry(
                features_dir=str(ambient),
                sampling_weight=0.0,
                penalty_weight=1.0,
                truth=False,
                truncation_strategy="split",
                type="mmap",
            )
        )
    return entries


def trainer_config(cfg: PipelineConfig, layout: Layout) -> TrainerYaml:
    """The complete trainer YAML as a dict (keys as ``microwakeword.train`` reads them)."""
    t = cfg.training
    return TrainerYaml(
        window_step_ms=t.window_step_ms,
        train_dir=str(layout.train_dir),
        features=feature_entries(cfg, layout),
        training_steps=list(t.training_steps),
        positive_class_weight=list(t.positive_class_weight),
        negative_class_weight=list(t.negative_class_weight),
        learning_rates=list(t.learning_rates),
        batch_size=t.batch_size,
        time_mask_max_size=list(t.time_mask_max_size),
        time_mask_count=list(t.time_mask_count),
        freq_mask_max_size=list(t.freq_mask_max_size),
        freq_mask_count=list(t.freq_mask_count),
        eval_step_interval=t.eval_step_interval,
        clip_duration_ms=t.clip_duration_ms,
        target_minimization=t.target_minimization,
        minimization_metric=t.minimization_metric,
        maximization_metric=t.maximization_metric,
    )


def trainer_argv(cfg: PipelineConfig, layout: Layout, *, restore: bool) -> list[str]:
    """Command line for ``python -m microwakeword.model_train_eval``."""
    return [
        sys.executable,
        "-m",
        "microwakeword.model_train_eval",
        f"--training_config={layout.training_yaml}",
        "--train",
        "1",
        "--restore_checkpoint",
        "1" if restore else "0",
        "--test_tf_nonstreaming",
        "0",
        "--test_tflite_nonstreaming",
        "0",
        "--test_tflite_nonstreaming_quantized",
        "0",
        "--test_tflite_streaming",
        "0",
        "--test_tflite_streaming_quantized",
        "1",
        "--use_weights",
        "best_weights",
        *cfg.training.model.cli_args(),
    ]


def run(cfg: PipelineConfig, layout: Layout, *, restore: bool = True, dry_run: bool = False) -> int:
    """Write the YAML and run the trainer (hours on a GPU; see README for estimates)."""
    config = trainer_config(cfg, layout)
    if not any(entry["truth"] for entry in config["features"]):
        log.error("train.no_positives", hint="run 03_augment first")
        return 1
    layout.train_dir.mkdir(parents=True, exist_ok=True)
    layout.training_yaml.write_text(yaml.safe_dump(dict(config), sort_keys=False), encoding="utf-8")
    argv = trainer_argv(cfg, layout, restore=restore)
    log.info("train.start", yaml=str(layout.training_yaml), argv=argv)
    if dry_run:
        return 0
    subprocess.run(argv, check=True)
    if not layout.quantized_stream_tflite.exists():
        log.error("train.no_tflite", expected=str(layout.quantized_stream_tflite))
        return 1
    log.info("train.done", tflite=str(layout.quantized_stream_tflite))
    return 0
