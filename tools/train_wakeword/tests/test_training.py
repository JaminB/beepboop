"""The trainer YAML and command line are generated as microwakeword expects them."""

from __future__ import annotations

from pathlib import Path

import yaml

from train_wakeword.config import PipelineConfig
from train_wakeword.paths import Layout
from train_wakeword.training import trainer_argv, trainer_config

REQUIRED_YAML_KEYS = {
    "window_step_ms",
    "train_dir",
    "features",
    "training_steps",
    "learning_rates",
    "batch_size",
    "eval_step_interval",
    "clip_duration_ms",
    "target_minimization",
    "minimization_metric",
    "maximization_metric",
}


def _layout_with_features(tmp_path: Path, config: PipelineConfig) -> Layout:
    layout = Layout(tmp_path, config.model_name)
    for name in ("positives", "near_misses", "speech", "no_speech", "ambient"):
        layout.feature_set(name).mkdir(parents=True)
    return layout


def test_trainer_config_shape(config: PipelineConfig, tmp_path: Path) -> None:
    layout = _layout_with_features(tmp_path, config)
    cfg = trainer_config(config, layout)
    assert set(cfg) >= REQUIRED_YAML_KEYS
    features = cfg["features"]
    assert len(features) == 5
    truths = [f["truth"] for f in features]
    assert truths.count(True) == 1
    ambient = next(f for f in features if f["truncation_strategy"] == "split")
    assert ambient["sampling_weight"] == 0.0
    assert {f["type"] for f in features} == {"mmap"}
    assert yaml.safe_load(yaml.safe_dump(cfg)) == cfg


def test_missing_sets_are_skipped(config: PipelineConfig, tmp_path: Path) -> None:
    layout = Layout(tmp_path, config.model_name)
    layout.feature_set("positives").mkdir(parents=True)
    cfg = trainer_config(config, layout)
    assert [f["truth"] for f in cfg["features"]] == [True]


def test_trainer_argv(config: PipelineConfig, tmp_path: Path) -> None:
    layout = Layout(tmp_path, config.model_name)
    argv = trainer_argv(config, layout, restore=False)
    assert argv[1:3] == ["-m", "microwakeword.model_train_eval"]
    assert f"--training_config={layout.training_yaml}" in argv
    assert argv[argv.index("--restore_checkpoint") + 1] == "0"
    assert argv[argv.index("--test_tflite_streaming_quantized") + 1] == "1"
    assert "mixednet" in argv


def test_layout_paths(config: PipelineConfig, tmp_path: Path) -> None:
    layout = Layout(tmp_path, config.model_name)
    assert layout.tflite.name == "hey_beepboop_v1.tflite"
    assert layout.manifest.name == "hey_beepboop_v1.json"
    assert layout.quantized_stream_tflite.parts[-2:] == (
        "tflite_stream_state_internal_quant",
        "stream_state_internal_quant.tflite",
    )
