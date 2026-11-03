"""The shipped config is valid and says what VOICE_PIPELINE §6 says."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from train_wakeword.config import MixedNetConfig, PipelineConfig

SPEC_NEAR_MISSES = ("hey people", "beat loop", "he bumped", "hey, be quick")  # VOICE_PIPELINE §6.4


def test_loads(config: PipelineConfig) -> None:
    assert config.wake_word == "Hey beepboop"
    assert config.model_name == "hey_beepboop_v1"


def test_phrase_variants(config: PipelineConfig) -> None:
    phrases = config.positives.phrases
    assert len(phrases) >= 3
    assert any(p.text for p in phrases)
    assert any(p.phonemes for p in phrases)
    assert {"hˈeɪ bˈiːpbuːp", "hˈeɪ bˈiːp bˈuːp"} <= {p.phonemes for p in phrases if p.phonemes}  # noqa: RUF001 # IPA


def test_speed_range_matches_spec(config: PipelineConfig) -> None:
    scales = config.positives.length_scales
    assert min(scales) == pytest.approx(0.8)
    assert max(scales) == pytest.approx(1.3)


def test_near_misses_include_spec_examples(config: PipelineConfig) -> None:
    joined = " ".join(config.positives.near_misses).lower()
    for phrase in SPEC_NEAR_MISSES:
        assert phrase in joined


def test_shipped_profile_excludes_nc_voices(config: PipelineConfig) -> None:
    shipped = {v.id for v in config.voices("shipped")}
    assert "en_US-hfc_female-medium" not in shipped
    assert "en_US-ryan-medium" not in shipped
    assert len(config.voices("research")) > len(shipped)
    assert any(v.kind == "generator" for v in config.voices("shipped"))


def test_dataset_roles_cover_training_needs(config: PipelineConfig) -> None:
    roles = {d.role for d in config.datasets_for("shipped")}
    assert {"speech", "no_speech", "rir", "background", "ambient_eval"} <= roles


def test_inference_cadence_is_30ms(config: PipelineConfig) -> None:
    """VOICE_PIPELINE §6.1: 10 ms feature stride, evaluated every 30 ms."""
    assert config.training.window_step_ms == 10
    assert config.training.model.stride * config.training.window_step_ms == 30
    assert config.export.feature_step_size == 10


def test_detection_defaults_match_spec(config: PipelineConfig) -> None:
    """VOICE_PIPELINE §6.2 / §17.1 targets."""
    assert config.export.probability_cutoff == pytest.approx(0.97)
    assert config.export.sliding_window_size == 5
    assert config.evaluate.refractory_ms == 2000
    assert config.evaluate.max_false_accepts_per_hour == pytest.approx(0.5 / 24, rel=0.01)
    assert config.evaluate.max_false_reject_rate == pytest.approx(0.05)


def test_mixednet_cli_args(config: PipelineConfig) -> None:
    args = config.training.model.cli_args()
    assert args[0] == "mixednet"
    assert args[args.index("--mixconv_kernel_sizes") + 1] == "[5], [7,11], [9,15], [23]"
    assert args[args.index("--stride") + 1] == "3"


def test_mixednet_rejects_ragged_blocks() -> None:
    with pytest.raises(ValidationError):
        MixedNetConfig(
            pointwise_filters=[64, 64],
            repeat_in_block=[1],
            mixconv_kernel_sizes=[[5], [7]],
            residual_connection=[0, 0],
            first_conv_filters=32,
            first_conv_kernel_size=5,
            stride=3,
        )


def _mutated(config_path: Path, tmp_path: Path, mutate: str) -> Path:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if mutate == "extra_key":
        raw["positives"]["unknown_key"] = 1
    elif mutate == "phrase_both":
        raw["positives"]["phrases"][0] = {"id": "bad", "text": "x", "phonemes": "y"}
    elif mutate == "dataset_url_and_manual":
        raw["datasets"][0]["manual"] = True
    elif mutate == "step_mismatch":
        raw["export"]["feature_step_size"] = 20
    elif mutate == "bad_metric":
        raw["training"]["maximization_metric"] = "f1"
    out = tmp_path / "cfg.yaml"
    out.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return out


@pytest.mark.parametrize(
    "mutate", ["extra_key", "phrase_both", "dataset_url_and_manual", "step_mismatch", "bad_metric"]
)
def test_invalid_configs_rejected(config_path: Path, tmp_path: Path, mutate: str) -> None:
    with pytest.raises(ValidationError):
        PipelineConfig.load(_mutated(config_path, tmp_path, mutate))
