"""Resolve model files through ``models/manifest.yaml`` (OPERATIONS §13).

The manifest is the single source of truth for every downloaded artifact; the
fetcher and its schema live in ``tools/bench/beepboop_bench/models.py``. The
audio service only *reads* it: each provider asks for an entry by id and for a
named file inside it, and gets a :class:`ModelMissingError` with a useful
message when the weights are not installed (``beepboop models verify``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from beepboop_audio.config import as_list, as_mapping

MANIFEST = "manifest.yaml"

# The manifest ids the v0.1 providers use.
SPEAKER_CAMPP = "wespeaker-en-voxceleb-campp"
STT_ZIPFORMER = "sherpa-onnx-streaming-zipformer-en-20m-2023-02-17"
STT_ZIPFORMER_BPE = "icefall-librispeech-bpe-500"
VAD_SILERO = "silero-vad"
WAKE_HEY_BEEPBOOP = "hey_beepboop"


class ModelMissingError(FileNotFoundError):
    """A model the configuration needs is not installed."""


@dataclass(frozen=True, slots=True)
class ModelEntry:
    id: str
    kind: str
    dest: Path
    sha256: str | None
    license: str
    status: str
    files: dict[str, str] = field(default_factory=dict[str, str])

    def file(self, key: str) -> Path:
        """A named file inside an archive entry, or the entry itself for single files."""
        path = self.dest / self.files[key] if key in self.files else self.dest
        if not path.exists():
            raise ModelMissingError(
                f"model {self.id!r} is not installed ({path}); run `beepboop models update`"
            )
        return path

    @property
    def installed(self) -> bool:
        return self.dest.exists()


class ModelStore:
    """The manifest under one models root (``/models`` in the container)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._entries: dict[str, ModelEntry] = {}
        manifest = root / MANIFEST
        if manifest.is_file():
            self._load(manifest)

    def _load(self, manifest: Path) -> None:
        loaded = as_mapping(yaml.safe_load(manifest.read_text(encoding="utf-8")))
        for raw in as_list(loaded.get("models")):
            row = as_mapping(raw)
            if "id" not in row:
                continue
            files = {k: str(v) for k, v in as_mapping(row.get("files")).items()}
            entry = ModelEntry(
                id=str(row["id"]),
                kind=str(row.get("kind", "")),
                dest=self.root / str(row.get("dest", row["id"])),
                sha256=str(row["sha256"]) if row.get("sha256") else None,
                license=str(row.get("license", "")),
                status=str(row.get("status", "published")),
                files=files,
            )
            self._entries[entry.id] = entry

    def get(self, model_id: str) -> ModelEntry:
        try:
            return self._entries[model_id]
        except KeyError:
            raise ModelMissingError(
                f"model {model_id!r} is not in {self.root / MANIFEST}"
            ) from None

    def by_kind(self, kind: str) -> list[ModelEntry]:
        return [e for e in self._entries.values() if e.kind == kind]

    def available(self, model_id: str) -> bool:
        entry = self._entries.get(model_id)
        return entry is not None and entry.installed


__all__ = [
    "SPEAKER_CAMPP",
    "STT_ZIPFORMER",
    "STT_ZIPFORMER_BPE",
    "VAD_SILERO",
    "WAKE_HEY_BEEPBOOP",
    "ModelEntry",
    "ModelMissingError",
    "ModelStore",
]
