"""Text -> espeak-ng phoneme codepoints for Piper voices (VOICE_PIPELINE §11.1).

sherpa-onnx >= 2.0 dropped its bundled espeak-ng frontend, so a Piper VITS
voice needs ``GenerationConfig.phoneme_codepoints`` from an external
phonemiser. The product TTS path uses ``piper_phonemize`` (espeak-ng,
GPL-3.0), a pinned runtime dependency of ``beepboop-audio`` (ADR 0020; the
project is GPL-3.0-or-later). It is imported lazily so that importing the
audio package stays cheap; :func:`available` is ``False`` only on an
environment that was not synced from ``uv.lock``.
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
    "no phonemiser: piper-phonemize is not installed; run `uv sync` "
    "(it is a pinned beepboop-audio dependency, ADR 0020)"
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
