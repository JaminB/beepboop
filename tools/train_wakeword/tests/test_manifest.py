"""The manifest schema matches what pymicro-wakeword 2.5.0 and ESPHome read."""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from train_wakeword.config import PipelineConfig
from train_wakeword.export import build_manifest
from train_wakeword.manifest import PYMICRO_WAKEWORD_KEYS, Manifest, MicroSettings

# okay_nabu.json from OHF-Voice/pymicro-wakeword (the reference v2 manifest).
OKAY_NABU = {
    "type": "micro",
    "wake_word": "Okay Nabu",
    "author": "Kevin Ahrendt",
    "website": "https://www.kevinahrendt.com/",
    "model": "okay_nabu.tflite",
    "trained_languages": ["en", "nl", "fr", "de", "it", "es", "sv"],
    "version": 2,
    "micro": {
        "probability_cutoff": 0.85,
        "feature_step_size": 10,
        "sliding_window_size": 5,
        "tensor_arena_size": 37000,
        "minimum_esphome_version": "2024.7.0",
    },
}


def test_reference_manifest_validates() -> None:
    manifest = Manifest.model_validate(OKAY_NABU)
    assert manifest.model_dump() == OKAY_NABU


def test_build_and_round_trip(config: PipelineConfig, tmp_path: Path) -> None:
    manifest = build_manifest(config, "hey_beepboop_v1.tflite")
    path = tmp_path / "hey_beepboop_v1.json"
    manifest.dump(path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["type"] == "micro"
    assert loaded["version"] == 2
    assert loaded["model"] == "hey_beepboop_v1.tflite"
    assert loaded["micro"]["probability_cutoff"] == pytest.approx(0.97)
    assert Manifest.load(path) == manifest


def test_cutoff_override(config: PipelineConfig) -> None:
    assert (
        build_manifest(config, "m.tflite", probability_cutoff=0.9).micro.probability_cutoff == 0.9
    )


def test_covers_every_key_the_loader_reads() -> None:
    manifest = Manifest.model_validate(OKAY_NABU)
    assert manifest.flat_keys() >= PYMICRO_WAKEWORD_KEYS


@pytest.mark.parametrize(
    "bad",
    [
        {"model": "sub/dir.tflite"},
        {"model": "weights.onnx"},
        {"version": 1},
        {"type": "openwakeword"},
        {"trained_languages": []},
    ],
)
def test_rejects_malformed(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate({**OKAY_NABU, **bad})


def test_rejects_bad_cutoff() -> None:
    with pytest.raises(ValidationError):
        MicroSettings(
            probability_cutoff=1.5,
            feature_step_size=10,
            sliding_window_size=5,
            tensor_arena_size=1,
            minimum_esphome_version="2024.7.0",
        )


@pytest.mark.skipif(
    importlib.util.find_spec("pymicro_wakeword") is None, reason="pymicro-wakeword not installed"
)
def test_pinned_loader_reads_exactly_these_keys() -> None:
    """Guard against a pymicro-wakeword upgrade silently changing the manifest contract."""
    from pymicro_wakeword import MicroWakeWord

    source = inspect.getsource(MicroWakeWord.from_config)
    for key in PYMICRO_WAKEWORD_KEYS:
        leaf = key.rsplit(".", 1)[-1]
        assert f'"{leaf}"' in source, f"pymicro-wakeword no longer reads {key!r}"
