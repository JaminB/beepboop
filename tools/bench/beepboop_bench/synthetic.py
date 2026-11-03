"""Synthetic corpus for CI (M0 step 6): clips generated with the local TTS voice.

The corpus has the same manifest shape as a real recording so every stage
benchmark runs end-to-end on x86, but it is flagged ``synthetic: true`` and
``recorded_through: synthetic-tts`` so ``--gates`` refuses it. Its numbers are
always labelled "synthetic / x86 / not a gate result".

What it cannot stand in for: distinct speakers (one voice, three speeds, so
the speaker EER is meaningless), room acoustics and distance (``distance_m``
is nominal), the stock wake word (the clips say the builtin model's phrase
until ``hey_beepboop`` is installed), and the 24 h soak (a few minutes of
TTS "radio" instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from beepboop_bench import phonemize
from beepboop_bench.audio import (
    SAMPLE_RATE,
    Float32Array,
    duration_s,
    silence,
    trim_trailing_silence,
    write_wav,
)
from beepboop_bench.corpus import Clip, CorpusManifest, Speaker, write_manifest
from beepboop_bench.runtime import Synthesizer, WakeDetector
from beepboop_bench.schema import Scenario
from beepboop_bench.stages import ModelSet

LEAD_S = 0.3
TAIL_S = 0.7

#: (transcript, scenario). ``command_1s`` clips are the G6 "1 s command" case.
COMMANDS: tuple[tuple[str, Scenario], ...] = (
    ("lights off", "command_1s"),
    ("stop", "command_1s"),
    ("turn off the kitchen lights", "fast_path"),
    ("turn on the living room lamp", "fast_path"),
    ("set a timer for five minutes", "fast_path"),
    ("add milk to the shopping list", "fast_path"),
    ("what is the weather like tomorrow", "no_tool"),
    ("tell me a short joke about robots", "no_tool"),
    ("how long do you boil an egg", "no_tool"),
    ("is the garage door open", "tool"),
    ("what is the temperature in the bedroom", "tool"),
    ("lock the front door and dim the hallway", "tool"),
)

SOAK_TEXT: tuple[str, ...] = (
    "And now the news. Markets closed slightly higher today after a quiet session.",
    "Tomorrow will be cloudy with a chance of showers in the afternoon and a light breeze.",
    "In sport, the home side won by two goals to one in front of a record crowd.",
    "Coming up next, a documentary about lighthouses and the people who kept them.",
    "Remember to check your smoke alarms this weekend and replace the batteries.",
    "The road between the bridge and the station is closed for repairs until Monday.",
)

#: Three "speakers": the same voice at three speaking rates. Not real speakers.
SPEEDS: dict[str, float] = {"syn_a": 0.9, "syn_b": 1.0, "syn_c": 1.15}


@dataclass(frozen=True)
class SyntheticOptions:
    small: bool = False


def _pad(audio: Float32Array) -> tuple[Float32Array, float]:
    speech = trim_trailing_silence(audio, keep_s=0.05)
    padded = np.concatenate([silence(LEAD_S), speech, silence(TAIL_S)])
    return padded, LEAD_S + duration_s(speech)


def generate(models: ModelSet, root: Path, opts: SyntheticOptions | None = None) -> CorpusManifest:
    """Write ``root/manifest.yaml`` plus clips; returns the manifest."""
    opts = opts or SyntheticOptions()
    if not models.tts_voices:
        msg = "no tts voice installed; run python -m beepboop_bench.models fetch"
        raise FileNotFoundError(msg)
    if not phonemize.available():
        raise RuntimeError(phonemize.MISSING_REASON)
    model, tokens = next(iter(models.tts_voices.values()))
    synth = Synthesizer(model, tokens, threads=2)
    wake_phrase = WakeDetector(models.wake_json).wake_word
    speeds = dict(list(SPEEDS.items())[:2]) if opts.small else SPEEDS
    commands = COMMANDS[:6] if opts.small else COMMANDS
    clips: list[Clip] = []

    for speaker, speed in speeds.items():
        for i, (text, scenario) in enumerate(commands):
            audio, end = _pad(synth.synth(text + ".", speed=speed))
            path = f"clips/{speaker}_cmd{i:02d}.wav"
            write_wav(root / path, audio)
            clips.append(
                Clip(
                    id=f"{speaker}_cmd{i:02d}",
                    path=path,
                    kind="command",
                    speaker=speaker,
                    transcript=text,
                    scenario=scenario,
                    speech_end_s=round(end, 3),
                    duration_s=round(duration_s(audio), 3),
                    note="synthetic TTS; speech_end_s from the TTS output boundary",
                )
            )
        for i, distance in enumerate((1.0, 3.0, 1.0, 3.0) if not opts.small else (1.0, 3.0)):
            audio, end = _pad(synth.synth(wake_phrase + ".", speed=speed))
            path = f"clips/{speaker}_wake{i:02d}.wav"
            write_wav(root / path, audio)
            clips.append(
                Clip(
                    id=f"{speaker}_wake{i:02d}",
                    path=path,
                    kind="wake",
                    speaker=speaker,
                    transcript=wake_phrase.lower(),
                    distance_m=distance,
                    phrase_end_s=round(end, 3),
                    duration_s=round(duration_s(audio), 3),
                    note="synthetic; distance_m is nominal, not acoustic",
                )
            )
        audio, end = _pad(synth.synth("My voice is my passport, verify me. " * 2, speed=speed))
        path = f"clips/{speaker}_enroll.wav"
        write_wav(root / path, audio)
        clips.append(
            Clip(
                id=f"{speaker}_enroll",
                path=path,
                kind="enroll",
                speaker=speaker,
                transcript="my voice is my passport verify me my voice is my passport verify me",
                speech_end_s=round(end, 3),
                duration_s=round(duration_s(audio), 3),
            )
        )

    soak_sentences = SOAK_TEXT[:2] if opts.small else SOAK_TEXT
    for i, text in enumerate(soak_sentences):
        audio, _ = _pad(synth.synth(text, speed=1.0))
        path = f"clips/soak{i:02d}.wav"
        write_wav(root / path, audio)
        clips.append(
            Clip(
                id=f"soak{i:02d}",
                path=path,
                kind="soak",
                transcript=text,
                duration_s=round(duration_s(audio), 3),
                note="synthetic 'radio' speech with no wake word",
            )
        )
    audio, end = _pad(synth.synth("Turn off the kitchen lights.", speed=0.8))
    write_wav(root / "clips/negative00.wav", audio)
    clips.append(
        Clip(
            id="negative00",
            path="clips/negative00.wav",
            kind="negative",
            speaker="stranger",
            transcript="turn off the kitchen lights",
            speech_end_s=round(end, 3),
            duration_s=round(duration_s(audio), 3),
            note="same voice at 0.8x; not a real impostor",
        )
    )

    manifest = CorpusManifest(
        name="synthetic-tts",
        synthetic=True,
        recorded_through="synthetic-tts",
        sample_rate=SAMPLE_RATE,
        capture_channel=0,
        channels=1,
        consent="none needed: generated speech, no person recorded",
        recorded_on="",
        speakers=[
            Speaker(id=s, kind="synthetic", note=f"en_US-lessac-low at speed {v}")
            for s, v in speeds.items()
        ],
        clips=clips,
    )
    write_manifest(root, manifest)
    return manifest
