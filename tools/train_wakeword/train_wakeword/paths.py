"""On-disk layout of a pipeline run (everything under ``--data-dir``, git-ignored).

::

    data/
      models/                     voice checkpoints (.pt, .onnx + .onnx.json)
      third_party/piper-sample-generator/   pinned clone (make deps)
      positives/<phrase_id>/      synthetic positives used for training
      positives_holdout/<phrase_id>/   never augmented or trained on; 06_evaluate uses them
      near_misses/<slug>/         TTS hard negatives
      negatives/<dataset_id>/raw/      archives as downloaded
      negatives/<dataset_id>/wav16k/   16 kHz mono PCM16 WAVs (role rir: the IRs)
      features/<set>/{training,validation,testing}/<set>_mmap
      features/ambient/{validation_ambient,testing_ambient}/ambient_mmap
      trained_models/<model_name>/     trainer output (checkpoints, tflite_*)
      out/                        exported .tflite, manifest .json, provenance
      results/                    06_evaluate JSON reports
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Layout:
    """Paths derived from one data directory and model name."""

    root: Path
    model_name: str

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def sample_generator(self) -> Path:
        return self.root / "third_party" / "piper-sample-generator"

    @property
    def positives(self) -> Path:
        return self.root / "positives"

    @property
    def positives_holdout(self) -> Path:
        return self.root / "positives_holdout"

    @property
    def near_misses(self) -> Path:
        return self.root / "near_misses"

    def negative_raw(self, dataset_id: str) -> Path:
        return self.root / "negatives" / dataset_id / "raw"

    def negative_wav(self, dataset_id: str) -> Path:
        return self.root / "negatives" / dataset_id / "wav16k"

    @property
    def checksums(self) -> Path:
        return self.root / "negatives" / "checksums.json"

    @property
    def features(self) -> Path:
        return self.root / "features"

    def feature_set(self, name: str) -> Path:
        return self.features / name

    @property
    def train_dir(self) -> Path:
        return self.root / "trained_models" / self.model_name

    @property
    def training_yaml(self) -> Path:
        return self.train_dir / "training_parameters.yaml"

    @property
    def quantized_stream_tflite(self) -> Path:
        """Where ``model_train_eval.py`` writes the quantised streaming model."""
        return (
            self.train_dir
            / "tflite_stream_state_internal_quant"
            / "stream_state_internal_quant.tflite"
        )

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def tflite(self) -> Path:
        return self.out / f"{self.model_name}.tflite"

    @property
    def manifest(self) -> Path:
        return self.out / f"{self.model_name}.json"

    @property
    def provenance(self) -> Path:
        return self.out / f"{self.model_name}.provenance.json"

    @property
    def manifest_entry(self) -> Path:
        return self.out / f"{self.model_name}.models-manifest-entry.yaml"

    @property
    def results(self) -> Path:
        return self.root / "results"
