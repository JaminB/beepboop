"""Piper-format VITS voices on ``sherpa_onnx.OfflineTts`` (VOICE_PIPELINE §11.1).

Entry point ``sherpa-piper``.

``phonemize.py`` is the product phonemiser (piper-phonemize over espeak-ng, ADR 0020);
the bench re-exports it so it measures what the product runs.
"""

from beepboop_audio.tts.sherpa_piper.provider import PiperProvider

__all__ = ["PiperProvider"]
