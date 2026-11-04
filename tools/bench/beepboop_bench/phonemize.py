"""Text -> espeak phoneme codepoints for Piper voices: the product phonemiser.

The bench uses exactly what the audio service uses
(:mod:`beepboop_audio.tts.sherpa_piper.phonemize`, ``piper-phonemize`` over
espeak-ng, a pinned ``beepboop-audio`` dependency per ADR 0020), so the
measured TTS first-chunk latency includes the product's phonemisation.
This module only re-exports it for the bench's call sites.
"""

from __future__ import annotations

from beepboop_audio.tts.sherpa_piper.phonemize import (
    MISSING_REASON,
    Phonemizer,
    available,
    phonemize,
)

__all__ = ["MISSING_REASON", "Phonemizer", "available", "phonemize"]
