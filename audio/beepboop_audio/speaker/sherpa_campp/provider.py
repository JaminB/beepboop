"""``SpeakerProvider`` for the WeSpeaker CAM++ VoxCeleb export (VOICE_PIPELINE §8.1).

fp32, 2 intra-op threads, L2-normalised output. ``model_id``/``model_sha`` come
from ``models/manifest.yaml`` and travel in ``hello.speaker`` with ``dim``
(read from the model, 512 for this export; ADR 0019), so a model change is
visible to core, which then marks every voiceprint stale (no audio exists to
re-embed, ADR 0018).
"""

from __future__ import annotations

from beepboop_audio._sherpa import SherpaEmbedder
from beepboop_audio.frames import to_float32
from beepboop_audio.models import SPEAKER_CAMPP, ModelStore
from beepboop_audio.types import PCM16, Embedding


class CamppProvider:
    id = "sherpa-campp"
    license = "CC-BY-4.0"

    def __init__(self, store: ModelStore, *, threads: int = 2) -> None:
        entry = store.get(SPEAKER_CAMPP)
        self._embedder = SherpaEmbedder(entry.file("model"), threads=threads)
        self.model_id: str = entry.id
        self.model_sha: str = entry.sha256 or "unknown"
        self.dim: int = self._embedder.dim

    def embed(self, audio: PCM16) -> Embedding:
        return self._embedder.embed(to_float32(audio))


__all__ = ["CamppProvider"]
