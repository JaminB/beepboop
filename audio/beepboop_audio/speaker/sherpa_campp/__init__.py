"""CAM++ speaker embeddings through sherpa-onnx (VOICE_PIPELINE §8.1).

Entry point ``sherpa-campp``.
"""

from beepboop_audio.speaker.sherpa_campp.provider import CamppProvider

__all__ = ["CamppProvider"]
