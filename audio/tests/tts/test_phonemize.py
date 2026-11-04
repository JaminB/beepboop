"""The product phonemiser (piper-phonemize is a pinned audio dependency, ADR 0020)."""

from __future__ import annotations

from beepboop_audio.tts.sherpa_piper import phonemize


def test_piper_phonemize_is_installed() -> None:
    # A real runtime dependency now, not an optional extra: a synced env always has it.
    assert phonemize.available(), phonemize.MISSING_REASON


def test_phonemize_returns_codepoint_sentences() -> None:
    out = phonemize.phonemize("Hello there. How are you?", "en-us")
    assert len(out) == 2
    assert all(sentence and all(isinstance(cp, int) for cp in sentence) for sentence in out)
