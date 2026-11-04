"""Normalisation for the ear and the system-phrase table (VOICE_PIPELINE §11.2, §11.3, §11.6)."""

from __future__ import annotations

import numpy as np
import pytest

from beepboop_audio.tts.normalize_en import normalize, number_words, ordinal_words, time_words
from beepboop_audio.tts.phrases import (
    ACK_CLIPS,
    SYSTEM_PHRASES,
    ClipCache,
    has_placeholder,
    prerender_keys,
    spoken_text,
)
from beepboop_audio.tts.sherpa_piper.provider import chunk, clamp_rate, espeak_voice, locale_of


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("10:30", "ten thirty"),
        ("It's 7:05 pm.", "It's seven oh five P M."),
        ("at 6:00", "at six o'clock"),
        ("72°F outside", "seventy-two degrees outside"),
        ("-3°C", "minus three degrees"),
        ("Dr. Smith", "doctor Smith"),
        ("50% done", "fifty percent done"),
        ("costs $4.50", "costs four dollars and fifty cents"),
        ("the 21st", "the twenty-first"),
        ("3.25 km", "three point two five km"),
        ("10 km away", "ten kilometres away"),
        ("1,250 steps", "one thousand two hundred and fifty steps"),
        ("no numbers here", "no numbers here"),
    ],
)
def test_normalize(text: str, spoken: str) -> None:
    assert normalize(text) == spoken


def test_number_words() -> None:
    assert number_words(0) == "zero"
    assert number_words(115) == "one hundred and fifteen"
    assert number_words(2_000_003) == "two million and three"
    assert ordinal_words(12) == "twelfth"
    assert ordinal_words(40) == "fortieth"
    assert ordinal_words(103) == "one hundred and third"
    assert time_words(9, 0) == "nine o'clock"


def test_phrase_table_matches_spec() -> None:
    # VOICE_PIPELINE §11.6 keys; tier names are never spoken (rule 2).
    assert {"unknown_speaker", "pin.prompt", "approval.prompt", "personal.unsure", "cancel"} <= set(
        SYSTEM_PHRASES
    )
    for text in SYSTEM_PHRASES.values():
        for tier in (" act ", "personal tier", "critical"):
            assert tier not in text.lower()
    assert set(ACK_CLIPS) == {"done", "ok", "sure", "cancelled", "timer_set"}
    keys = prerender_keys()
    assert "approval.prompt" not in keys  # has a placeholder
    assert keys["pin.prompt"] == "Please say your PIN."
    assert spoken_text("done") == "Done."  # spoken key wins over the earcon name
    assert spoken_text("cancel") == "Cancelled."
    assert spoken_text("wake") is None
    assert has_placeholder("{name}")
    assert not has_placeholder("plain")


def test_clip_cache_lru() -> None:
    cache = ClipCache(lru_size=2)
    v = ClipCache.voice_key("en_US-lessac-low", 1.0)
    pcm = np.zeros(10, dtype=np.int16)
    for t in ("a", "b", "c"):
        cache.put_sentence(v, t, pcm)
    assert cache.sentence(v, "a") is None
    assert cache.sentence(v, "c") is not None
    cache.put_clip(v, "done", pcm)
    assert cache.clip(v, "done") is not None
    assert cache.has_voice(v)
    assert cache.clip(ClipCache.voice_key("other", 1.0), "done") is None


def test_piper_helpers() -> None:
    assert clamp_rate(2.0) == 1.4
    assert clamp_rate(0.1) == 0.7
    assert locale_of("en_US-lessac-low") == "en-US"
    assert espeak_voice("en-US") == "en-us"
    assert [len(c) for c in chunk(np.zeros(3500, dtype=np.int16))] == [1600, 1600, 300]
