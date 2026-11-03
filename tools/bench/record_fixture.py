#!/usr/bin/env python3
"""Record the bench fixture corpus through the XVF3800 (M0 step 9, owner action).

Run this on the **owner's bench machine** (a laptop with the array plugged in),
never on a beepboop device: ADR 0018 says the product never writes audio, and
these recordings are the one sanctioned exception, kept on the recording
machine and git-ignored (``tools/bench/corpus/``). The result is a corpus in
the ``manifest.yaml`` shape of ``beepboop_bench.corpus`` that ``beepboop bench``
replays.

What it records (ROADMAP M0 scope, VOICE_PIPELINE §16)
    * ``command``  >= 200 utterances over >= 6 speakers: the prompt list below,
      each at 1 m and 3 m, quiet; labelled with ``speech_end_s`` from the
      recording's VAD-free length (you then hand-check ends in the manifest)
    * ``wake``     "Hey beepboop" at 1 m and 3 m, quiet and with a TV at 60 dB;
      ``phrase_end_s`` is the recorded length minus the tail you leave
    * ``enroll``   two enrolment sentences per speaker
    * ``soak``     a 24 h living-room recording with no wake word
      (``--soak-hours 24``), chunked into 10-minute files

Usage (Linux, ALSA; the array is the 6-channel 16 kHz USB device)::

    arecord -l                                   # find the card, e.g. hw:1
    uv run python tools/bench/record_fixture.py --device hw:1 --speaker alex --kind adult
    uv run python tools/bench/record_fixture.py --device hw:1 --soak-hours 24
    uv run python tools/bench/record_fixture.py --finish      # validates the manifest

Each take: the script prints the prompt, waits for Enter, records
``--take-seconds`` of 6-channel audio with ``arecord``, keeps **channel 0**
(the processed ASR beam, VOICE_PIPELINE §2.2) in ``clips/`` and the raw
6-channel take in ``raw/`` (channels 2-5 are only read by ``bench aec``).
Nothing is uploaded anywhere.

Consent (print this and get a yes from every speaker before recording)::

    These recordings of your voice are used only to benchmark beepboop's speech
    pipeline. They stay on this machine, are never committed to the repository or
    copied to a beepboop device, and are deleted on request. Children are recorded
    only with a parent's consent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from beepboop_bench.audio import SAMPLE_RATE, duration_s, trim_trailing_silence, write_wav
from beepboop_bench.corpus import (
    Ambient,
    Clip,
    ClipKind,
    Corpus,
    CorpusManifest,
    Speaker,
    default_corpus_root,
    write_manifest,
)
from beepboop_bench.schema import Scenario

CHANNELS = 6
CONSENT = (
    "These recordings of your voice are used only to benchmark beepboop's speech pipeline. "
    "They stay on this machine, are never committed to the repository or copied to a beepboop "
    "device, and are deleted on request. Children are recorded only with a parent's consent."
)

#: (transcript, scenario). ~35 prompts x 6 speakers x 2 distances > 200 utterances.
PROMPTS: tuple[tuple[str, Scenario], ...] = (
    ("lights off", "command_1s"),
    ("stop", "command_1s"),
    ("turn off the kitchen lights", "fast_path"),
    ("turn on the living room lamp", "fast_path"),
    ("dim the bedroom lights to twenty percent", "fast_path"),
    ("set a timer for five minutes", "fast_path"),
    ("set a timer for twelve minutes for the pasta", "fast_path"),
    ("cancel the timer", "fast_path"),
    ("add milk to the shopping list", "fast_path"),
    ("add paper towels to the household list", "fast_path"),
    ("what is on my shopping list", "fast_path"),
    ("pause the music", "fast_path"),
    ("turn the volume down", "fast_path"),
    ("lock the front door", "fast_path"),
    ("what time is it", "fast_path"),
    ("what is the weather like tomorrow", "no_tool"),
    ("tell me a short joke about robots", "no_tool"),
    ("how long do you boil an egg", "no_tool"),
    ("what is the capital of portugal", "no_tool"),
    ("explain how a thermostat works in one sentence", "no_tool"),
    ("remind me why the sky is blue", "no_tool"),
    ("how many grams are in an ounce", "no_tool"),
    ("is the garage door open", "tool"),
    ("what is the temperature in the bedroom", "tool"),
    ("lock the front door and dim the hallway", "tool"),
    ("is anyone home right now", "tool"),
    ("when does my next meeting start", "tool"),
    ("what is left on my to do list for today", "tool"),
    ("turn everything off downstairs and set the alarm", "tool"),
    ("how warm is it in the office and should i open a window", "tool"),
    ("switch the fan on if the living room is above twenty four degrees", "tool"),
    ("play something quiet in the kitchen", "tool"),
    ("what did i add to the list yesterday", "tool"),
    ("set the thermostat to nineteen and remind me at nine", "tool"),
    ("close the garage and tell me if the back door is locked", "tool"),
)
WAKE_PHRASE = "hey beepboop"
ENROLL_SENTENCES = (
    "my voice is my passport verify me",
    "the quick brown fox jumps over the lazy dog near the river bank",
)
DISTANCES_M = (1.0, 3.0)


def arecord_take(device: str, seconds: float) -> np.ndarray:
    exe = shutil.which("arecord")
    if exe is None:
        sys.exit("arecord not found: this script needs Linux ALSA (alsa-utils)")
    cmd = [
        exe, "-q", "-D", device, "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", str(CHANNELS),
        "-t", "raw", "-d", str(int(seconds)), "-",
    ]  # fmt: skip
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=seconds + 15).stdout  # noqa: S603 fixed argv
    return np.frombuffer(raw, dtype="<i2").reshape(-1, CHANNELS).astype(np.float32) / 32768.0


def load_or_new(root: Path) -> CorpusManifest:
    if (root / "manifest.yaml").is_file():
        return Corpus.load(root).manifest
    return CorpusManifest(
        name="household-xvf3800",
        synthetic=False,
        recorded_through="xvf3800",
        sample_rate=SAMPLE_RATE,
        capture_channel=0,
        channels=1,
        consent=CONSENT,
        recorded_on=datetime.now(UTC).date().isoformat(),
    )


def record_speaker(
    root: Path,
    manifest: CorpusManifest,
    device: str,
    speaker: str,
    kind: str,
    take_s: float,
    ambient: Ambient,
) -> CorpusManifest:
    speakers = list(manifest.speakers)
    if speaker not in {s.id for s in speakers}:
        speakers.append(Speaker(id=speaker, kind="child" if kind == "child" else "adult"))
    clips = list(manifest.clips)
    sys.stdout.write(f"\n{CONSENT}\n\nSpeaker {speaker!r}: type yes to confirm consent: ")
    sys.stdout.flush()
    if input().strip().lower() != "yes":
        sys.exit("no consent recorded; nothing captured")
    plan: list[tuple[ClipKind, str, Scenario | None, float]] = []
    for distance in DISTANCES_M:
        plan.extend(("command", text, scenario, distance) for text, scenario in PROMPTS)
        plan.extend(("wake", WAKE_PHRASE, None, distance) for _ in range(5))
    plan.extend(("enroll", text, None, 1.0) for text in ENROLL_SENTENCES)
    for i, (kind_, text, scenario, distance) in enumerate(plan):
        clip_id = f"{speaker}_{ambient}_{kind_}{i:03d}"
        if any(c.id == clip_id for c in clips):
            continue
        sys.stdout.write(
            f'\n[{i + 1}/{len(plan)}] stand at {distance:.0f} m and say: "{text}"  '
            f"(Enter to record {take_s:.0f} s) "
        )
        sys.stdout.flush()
        input()
        take = arecord_take(device, take_s)
        beam = np.ascontiguousarray(take[:, 0])
        speech = trim_trailing_silence(beam, threshold=0.02)
        rel = f"clips/{clip_id}.wav"
        write_wav(root / rel, beam)
        (root / "raw").mkdir(exist_ok=True)
        np.save(root / "raw" / f"{clip_id}.6ch.npy", take)  # all 6 channels, for bench aec
        end = round(duration_s(speech), 3)
        clips.append(
            Clip(
                id=clip_id,
                path=rel,
                kind=kind_,
                speaker=speaker,
                transcript=text,
                scenario=scenario,
                distance_m=distance,
                ambient=ambient,
                speech_end_s=end if kind_ != "wake" else None,
                phrase_end_s=end if kind_ == "wake" else None,
                duration_s=round(duration_s(beam), 3),
                note="speech_end_s/phrase_end_s from an energy trim: HAND-CHECK before a gate run",
            )
        )
        manifest = manifest.model_copy(update={"speakers": speakers, "clips": clips})
        write_manifest(root, manifest)
    return manifest


def record_soak(
    root: Path, manifest: CorpusManifest, device: str, hours: float, chunk_min: float
) -> CorpusManifest:
    clips = list(manifest.clips)
    n = int(hours * 60 / chunk_min)
    sys.stdout.write(
        f"recording {n} x {chunk_min:.0f} min soak chunks ({hours:.1f} h); "
        "do not say the wake word\n"
    )
    for i in range(n):
        clip_id = f"soak{i:04d}"
        if any(c.id == clip_id for c in clips):
            continue
        take = arecord_take(device, chunk_min * 60)
        rel = f"clips/{clip_id}.wav"
        write_wav(root / rel, np.ascontiguousarray(take[:, 0]))
        clips.append(
            Clip(
                id=clip_id,
                path=rel,
                kind="soak",
                duration_s=round(take.shape[0] / SAMPLE_RATE, 3),
                note=f"living-room soak chunk {i}",
            )
        )
        manifest = manifest.model_copy(update={"clips": clips})
        write_manifest(root, manifest)
        time.sleep(0.2)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=default_corpus_root())
    parser.add_argument("--device", default="hw:1", help="ALSA capture device of the XVF3800")
    parser.add_argument(
        "--speaker", help="speaker id to record (one session per speaker and ambient)"
    )
    parser.add_argument("--kind", choices=("adult", "child"), default="adult")
    parser.add_argument("--ambient", choices=("quiet", "tv60"), default="quiet")
    parser.add_argument("--take-seconds", type=float, default=4.0)
    parser.add_argument("--soak-hours", type=float, default=0.0)
    parser.add_argument("--soak-chunk-minutes", type=float, default=10.0)
    parser.add_argument(
        "--finish", action="store_true", help="validate the manifest and print counts"
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    root.mkdir(parents=True, exist_ok=True)
    manifest = load_or_new(root)
    if args.speaker:
        ambient: Ambient = "tv60" if args.ambient == "tv60" else "quiet"
        manifest = record_speaker(
            root, manifest, args.device, args.speaker, args.kind, args.take_seconds, ambient
        )
    if args.soak_hours > 0:
        manifest = record_soak(
            root, manifest, args.device, args.soak_hours, args.soak_chunk_minutes
        )
    if args.finish or not (args.speaker or args.soak_hours):
        corpus = Corpus.load(root)
        m = corpus.manifest
        soak_h = sum(c.duration_s or 0.0 for c in m.of_kind("soak")) / 3600
        sys.stdout.write(
            f"{root}: {len(m.speakers)} speakers, {len(m.commands())} commands, "
            f"{len(m.of_kind('wake'))} wake clips, {soak_h:.1f} h soak; real={m.real}\n"
        )
        if len(m.speakers) < 6 or len(m.commands()) < 200 or soak_h < 24:
            sys.stdout.write(
                "NOT YET a gate corpus: need >= 6 speakers, >= 200 commands, >= 24 h soak\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
