"""Text -> espeak phoneme codepoints for Piper voices (bench-only).

sherpa-onnx 2.0 dropped its bundled espeak-ng frontend: a Piper VITS voice
now needs ``GenerationConfig.phoneme_codepoints`` from an external
phonemizer. The bench uses ``piper_phonemize`` (espeak-ng, GPL-3.0) through
the optional ``phonemize`` extra; it is imported lazily here and nowhere
else, so the rest of the bench, and the product, never depend on it.

If the extra is missing, :func:`available` is ``False`` and every
TTS-dependent stage reports ``skipped`` with :data:`MISSING_REASON`.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# Reason: piper_phonemize is untyped; this is its only import site.

from __future__ import annotations

import importlib
from collections.abc import Callable
from functools import cache
from typing import cast

MISSING_REASON = (
    "no phonemizer: install the bench-only extra with "
    "`uv sync --all-packages --all-extras` (piper-phonemize, GPL-3.0, never in the audio image)"
)

#: ``phonemize_espeak(text, voice)`` -> one list of phoneme strings per sentence.
Phonemizer = Callable[[str, str], list[list[str]]]


@cache
def _load() -> Phonemizer | None:
    try:
        module = importlib.import_module("piper_phonemize")
    except ImportError:
        return None
    fn = getattr(module, "phonemize_espeak", None)
    return cast("Phonemizer", fn) if callable(fn) else None


def available() -> bool:
    return _load() is not None


def phonemize(text: str, voice: str = "en-us") -> list[list[int]]:
    """Sentences of espeak phoneme codepoints, as ``GenerationConfig.phoneme_codepoints`` wants."""
    fn = _load()
    if fn is None:
        raise RuntimeError(MISSING_REASON)
    sentences: list[list[str]] = [[str(p) for p in sentence] for sentence in fn(text, voice)]
    out: list[list[int]] = []
    for sentence in sentences:
        codepoints = [ord(ch) for phoneme in sentence for ch in phoneme]
        if codepoints:
            out.append(codepoints)
    return out
