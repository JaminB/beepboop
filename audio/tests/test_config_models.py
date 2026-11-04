"""Configuration, model manifest and provider registration (ARCHITECTURE §9, VOICE_PIPELINE §17)."""

from __future__ import annotations

from pathlib import Path

import pytest

from beepboop_audio.config import AudioConfig, WakeModelConfig, load_config
from beepboop_audio.models import (
    SPEAKER_CAMPP,
    STT_ZIPFORMER,
    ModelMissingError,
    ModelStore,
)
from beepboop_audio.registry import KINDS, UnknownProviderError, available, provider_class

MODELS = Path(__file__).resolve().parents[2] / "models"


def test_defaults_without_a_file(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.yaml", env={})
    assert config == AudioConfig(models_dir=Path("/models"))
    assert config.audio.endpoint.hangover_ms == 250
    assert config.providers.wake.model_configs()[0].probability_cutoff == 0.97
    assert config.providers.stt.id == "sherpa-zipformer"


def test_file_and_env_overrides(tmp_path: Path) -> None:
    path = tmp_path / "beepboop.yaml"
    path.write_text(
        """
device: {id: kitchen, name: Kitchen}
locale: en-GB
audio:
  alsa_device: hw:CARD=X
  endpoint: {hangover_ms: 300}
providers:
  wake:
    models: [{id: okay_nabu, probability_cutoff: 0.9}, hey_beepboop]
ha: {url: http://127.0.0.1:8123}
""",
        encoding="utf-8",
    )
    config = load_config(
        path, env={"BB_ALSA_DEVICE": "hw:XVF3800", "BB_LOCALE": "en-US", "BB_MODELS_DIR": "/m"}
    )
    assert config.device.id == "kitchen"
    assert config.locale == "en-US"
    assert config.audio.alsa_device == "hw:XVF3800"
    assert config.audio.endpoint.hangover_ms == 300
    assert config.models_dir == Path("/m")
    models = config.providers.wake.model_configs()
    assert models == [
        WakeModelConfig(id="okay_nabu", probability_cutoff=0.9),
        WakeModelConfig(id="hey_beepboop"),
    ]


def test_bb_config_env_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("device: {id: den}\n", encoding="utf-8")
    assert load_config(env={"BB_CONFIG": str(path)}).device.id == "den"


def test_manifest_parsing(tmp_path: Path) -> None:
    (tmp_path / "speaker").mkdir()
    (tmp_path / "speaker" / "m.onnx").write_bytes(b"x")
    (tmp_path / "manifest.yaml").write_text(
        """
schema_version: 1
models:
  - {id: a, kind: speaker, dest: speaker/m.onnx, sha256: abc, license: MIT}
  - {id: b, kind: stt, dest: stt/b, files: {tokens: tokens.txt}, license: MIT}
  - not-a-mapping
  - {kind: tts}
""",
        encoding="utf-8",
    )
    store = ModelStore(tmp_path)
    assert store.get("a").file("model") == tmp_path / "speaker" / "m.onnx"
    assert store.get("a").sha256 == "abc"
    assert store.available("a")
    assert not store.available("b")
    with pytest.raises(ModelMissingError, match="not installed"):
        store.get("b").file("tokens")
    with pytest.raises(ModelMissingError, match="not in"):
        store.get("zzz")
    assert [e.id for e in store.by_kind("stt")] == ["b"]


def test_empty_root(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    assert store.by_kind("tts") == []


def test_repo_manifest_names_every_v01_model() -> None:
    store = ModelStore(MODELS)
    for model_id in (SPEAKER_CAMPP, STT_ZIPFORMER, "icefall-librispeech-bpe-500", "silero-vad"):
        assert store.get(model_id).license


def test_entry_points_registered() -> None:
    assert available("wake") == ["microwakeword"]
    assert available("speaker") == ["sherpa-campp"]
    assert available("stt") == ["sherpa-zipformer"]
    assert available("tts") == ["sherpa-piper"]
    for kind in KINDS:
        for pid in available(kind):
            cls = provider_class(kind, pid)
            assert cls.id == pid  # type: ignore[attr-defined]
            assert cls.license  # type: ignore[attr-defined]
    with pytest.raises(UnknownProviderError, match="installed"):
        provider_class("stt", "deepgram")
