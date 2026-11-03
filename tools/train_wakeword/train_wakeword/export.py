"""Step 05: export ``<model_name>.tflite`` + manifest JSON + provenance.

Outputs in ``out/``:

* ``hey_beepboop_v1.tflite``: the int8 streaming model from the trainer, byte-identical.
* ``hey_beepboop_v1.json``: the v2 manifest ``pymicro-wakeword`` loads
  (:mod:`train_wakeword.manifest`).
* ``hey_beepboop_v1.provenance.json``: config hash, profile, every voice and dataset
  with its licence row, package versions, git commit, date.
* ``hey_beepboop_v1.models-manifest-entry.yaml``: the row to paste into
  ``models/manifest.yaml`` (url is PENDING until the owner publishes the file).

A model built under ``--profile research`` is refused unless ``--allow-research`` is
given, and then it is named ``<model_name>_research`` so it can never be mistaken for
the shippable artefact.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import cast

import yaml

from train_wakeword.config import PipelineConfig
from train_wakeword.licenses import LicenseRow, load_license_table
from train_wakeword.log import get_logger
from train_wakeword.manifest import Manifest, MicroSettings
from train_wakeword.paths import Layout

log = get_logger(__name__)

PACKAGES = (
    "tensorflow",
    "pymicro-wakeword",
    "pymicro-features",
    "piper-tts",
    "audiomentations",
    "numpy",
)


def build_manifest(
    cfg: PipelineConfig, model_file: str, *, probability_cutoff: float | None = None
) -> Manifest:
    """The manifest for this config; ``probability_cutoff`` overrides the config's target."""
    e = cfg.export
    return Manifest(
        wake_word=cfg.wake_word,
        author=e.author,
        website=e.website,
        model=model_file,
        trained_languages=list(e.trained_languages),
        micro=MicroSettings(
            probability_cutoff=probability_cutoff
            if probability_cutoff is not None
            else e.probability_cutoff,
            feature_step_size=e.feature_step_size,
            sliding_window_size=e.sliding_window_size,
            tensor_arena_size=e.tensor_arena_size,
            minimum_esphome_version=e.minimum_esphome_version,
        ),
    )


def _git_commit(repo: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(
            [git, "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError:
        return None
    return out.stdout.strip()


def _versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def provenance(
    cfg: PipelineConfig,
    config_path: Path,
    layout: Layout,
    profile: str,
    table: dict[str, LicenseRow],
) -> dict[str, object]:
    """Everything needed to say where the model came from."""
    training_yaml = (
        layout.training_yaml.read_text(encoding="utf-8") if layout.training_yaml.exists() else None
    )
    features_manifest = layout.features / "MANIFEST.json"
    return {
        "model": layout.model_name,
        "wake_word": cfg.wake_word,
        "profile": profile,
        "exported_at": datetime.now(UTC).isoformat(),
        "config_path": str(config_path),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "tflite_sha256": hashlib.sha256(layout.tflite.read_bytes()).hexdigest(),
        "tflite_bytes": layout.tflite.stat().st_size,
        "git_commit": _git_commit(config_path.parent),
        "packages": _versions(),
        "trainer_yaml": training_yaml,
        "features_manifest": (
            json.loads(features_manifest.read_text(encoding="utf-8"))
            if features_manifest.exists()
            else None
        ),
        "voices": [
            {"id": v.id, "license_id": v.license_id, "licence": table[v.license_id].licence}
            for v in cfg.voices("research" if profile == "research" else "shipped")
        ],
        "datasets": [
            {
                "id": d.id,
                "role": d.role,
                "license_id": d.license_id,
                "licence": table[d.license_id].licence,
            }
            for d in cfg.datasets_for("research" if profile == "research" else "shipped")
        ],
    }


def models_manifest_entry(cfg: PipelineConfig, layout: Layout, sha256: str, size: int) -> str:
    """YAML snippet for ``models/manifest.yaml`` (OPERATIONS §13 fields)."""
    entry = {
        "id": layout.model_name,
        "kind": "wake",
        "url": f"PENDING: publish out/{layout.model_name}.tflite and .json as a release asset",
        "sha256": sha256,
        "size": size,
        "license": "Apache-2.0",
        "attribution": (
            "Trained by tools/train_wakeword/; training-data licences in "
            "tools/train_wakeword/DATA_LICENSES.md"
        ),
        "source": "tools/train_wakeword/",
        "notes": f"Wake word {cfg.wake_word!r}; manifest {layout.model_name}.json ships next to it",
    }
    return yaml.safe_dump([entry], sort_keys=False)


def run(
    cfg: PipelineConfig,
    config_path: Path,
    layout: Layout,
    licenses_path: Path,
    *,
    probability_cutoff: float | None = None,
    allow_research: bool = False,
) -> int:
    """Export the trained model; return 0 on success."""
    features_manifest = layout.features / "MANIFEST.json"
    profile = "unknown"
    if features_manifest.exists():
        loaded: object = json.loads(features_manifest.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            profile = str(cast("dict[str, object]", loaded).get("profile", "unknown"))
    if profile != "shipped":
        if not allow_research:
            log.error(
                "export.refused",
                profile=profile,
                hint="features were not built under --profile shipped",
            )
            return 1
        layout = Layout(layout.root, f"{layout.model_name}_research")
    source = Layout(layout.root, cfg.model_name).quantized_stream_tflite
    if not source.exists():
        log.error("export.no_model", expected=str(source), hint="run 04_train first")
        return 1
    layout.out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, layout.tflite)
    manifest = build_manifest(cfg, layout.tflite.name, probability_cutoff=probability_cutoff)
    manifest.dump(layout.manifest)
    table = load_license_table(licenses_path)
    prov = provenance(cfg, config_path, layout, profile, table)
    layout.provenance.write_text(json.dumps(prov, indent=2) + "\n", encoding="utf-8")
    layout.manifest_entry.write_text(
        models_manifest_entry(
            cfg, layout, str(prov["tflite_sha256"]), int(str(prov["tflite_bytes"]))
        ),
        encoding="utf-8",
    )
    _verify_loads(layout)
    log.info(
        "export.done", tflite=str(layout.tflite), manifest=str(layout.manifest), profile=profile
    )
    return 0


def _verify_loads(layout: Layout) -> None:
    """Load the exported pair with pymicro-wakeword, the Pi's inference library."""
    try:
        from pymicro_wakeword import MicroWakeWord
    except ImportError:
        log.warning("export.verify_skipped", reason="pymicro-wakeword not installed")
        return
    MicroWakeWord.from_config(layout.manifest)
    log.info("export.verified", loader="pymicro-wakeword")
