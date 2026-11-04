"""Streaming zipformer transducer through sherpa-onnx (VOICE_PIPELINE §10.1).

Entry point ``sherpa-zipformer``.
"""

from beepboop_audio.stt.sherpa_zipformer.provider import ZipformerProvider

__all__ = ["ZipformerProvider"]
