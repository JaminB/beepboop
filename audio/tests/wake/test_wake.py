"""Wake decision rule and the microWakeWord provider (VOICE_PIPELINE §6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from beepboop_audio.config import WakeModelConfig
from beepboop_audio.models import ModelMissingError, ModelStore
from beepboop_audio.testing import silence, split_frames
from beepboop_audio.wake.decider import WakeDecider
from beepboop_audio.wake.microwakeword.provider import MicroWakeWordProvider, resolve_config_json

MS = 1_000_000


def test_window_mean_must_exceed_cutoff() -> None:
    d = WakeDecider(probability_cutoff=0.9, sliding_window_size=3, refractory_ms=2000)
    assert d.update(1.0, 0) is None
    assert d.update(1.0, 30 * MS) is None  # window not full
    assert d.update(0.6, 60 * MS) is None  # mean 0.87
    assert d.update(1.0, 90 * MS) is None  # mean 0.87 (1.0, 0.6, 1.0)
    assert d.update(1.0, 120 * MS) is None  # mean 0.87 (0.6, 1.0, 1.0)
    hit = d.update(1.0, 150 * MS)
    assert hit == pytest.approx(1.0)


def test_refractory_period() -> None:
    d = WakeDecider(probability_cutoff=0.5, sliding_window_size=1, refractory_ms=2000)
    assert d.update(0.9, 0) is not None
    assert d.update(0.9, 1000 * MS) is None
    assert d.update(0.9, 2001 * MS) is not None
    d.reset()


def test_resolve_config(tmp_path: Path) -> None:
    builtin = resolve_config_json(WakeModelConfig(id="okay_nabu"), None)
    assert builtin.name == "okay_nabu.json"
    with pytest.raises(ModelMissingError):
        resolve_config_json(WakeModelConfig(id="hey_beepboop"), ModelStore(tmp_path))
    with pytest.raises(ModelMissingError):
        resolve_config_json(WakeModelConfig(id="x", path=str(tmp_path / "x.tflite")), None)
    cfg = tmp_path / "x.json"
    cfg.write_text("{}", encoding="utf-8")
    assert (
        resolve_config_json(WakeModelConfig(id="x", path=str(tmp_path / "x.tflite")), None) == cfg
    )


def test_builtin_model_stays_quiet_on_silence() -> None:
    provider = MicroWakeWordProvider([WakeModelConfig(id="okay_nabu")])
    assert provider.models() == ["okay_nabu"]
    assert provider.id == "microwakeword"
    hits = [provider.process(f) for f in split_frames(silence(3000))]
    assert not any(hits)
    provider.reset()


def test_needs_a_model() -> None:
    with pytest.raises(ValueError, match="at least one"):
        MicroWakeWordProvider([])
