"""``bench aec`` (V3): wake-hit rate and STT WER during playback vs silence.

VOICE_PIPELINE §2.4 / §15.2: play a 10 s TTS clip through the XVF3800 while a
person at 2 m says the wake word five times, and compare with the same
utterances in silence. Pass: hit rate during playback >= 90 % of the quiet
rate and WER within 5 absolute points; then ``aec_verified`` may be set.

This needs the array, a speaker, a person and Linux ALSA (``arecord`` /
``aplay``), so it is **pending (owner with Pi)**. Off Linux, or without
``--device``, the section is ``pending`` with instructions; the procedure
itself is in ``tools/probes/aec_reference/README.md``. The live path records
through ``arecord`` (6 channels, channel 0 = ASR beam) straight into the wake
detector and recogniser; nothing is written to disk (ADR 0018).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

from beepboop_bench.audio import FRAME_SAMPLES, SAMPLE_RATE, Float32Array, to_pcm16, write_wav
from beepboop_bench.corpus import ASR_BEAM_CHANNEL
from beepboop_bench.schema import AecSection
from beepboop_bench.stages import ModelSet
from beepboop_bench.wer import wer_pct

PENDING_REASON = (
    "needs the XVF3800, a speaker, a person at 2 m and Linux ALSA; run on the Pi with "
    "--device hw:<card> (procedure: tools/probes/aec_reference/README.md)"
)
#: Utterances the person says five times in each phase (wake word + a command).
AEC_PHRASES: tuple[str, ...] = ("hey beepboop, turn off the kitchen lights",) * 5
PLAYBACK_TEXT = (
    "This is the assistant speaking for about ten seconds so the array's echo canceller "
    "has a playback reference to remove while you talk over it."
)
CAPTURE_CHANNELS = 6


def _capture(device: str, seconds: float) -> Float32Array:
    """Record ``seconds`` of the ASR beam via ``arecord`` (never touches disk)."""
    arecord = shutil.which("arecord")
    if arecord is None:
        msg = "arecord not found"
        raise FileNotFoundError(msg)
    cmd = [
        arecord, "-q", "-D", device, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
        "-c", str(CAPTURE_CHANNELS),
        "-t", "raw", "-d", str(int(seconds)), "-",
    ]  # fmt: skip
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=seconds + 10).stdout  # noqa: S603 fixed argv
    pcm = np.frombuffer(raw, dtype="<i2").reshape(-1, CAPTURE_CHANNELS)
    return (pcm[:, ASR_BEAM_CHANNEL].astype(np.float32)) / 32768.0


def _play(device: str, wav: Path) -> subprocess.Popen[bytes]:
    aplay = shutil.which("aplay")
    if aplay is None:
        msg = "aplay not found"
        raise FileNotFoundError(msg)
    return subprocess.Popen([aplay, "-q", "-D", device, str(wav)])  # noqa: S603 fixed argv


def _score(models: ModelSet, samples: Float32Array, expected: str) -> tuple[int, str]:
    from beepboop_bench.runtime import SttRecognizer, WakeDetector
    from beepboop_bench.stages import _wake_hits  # pyright: ignore[reportPrivateUsage]

    detector = WakeDetector(models.wake_json)
    hits = len(_wake_hits(detector, samples))
    text = ""
    if models.stt is not None:
        recognizer = SttRecognizer(models.stt, threads=2)
        stream = recognizer.stream()
        for start in range(0, len(samples), FRAME_SAMPLES):
            stream.feed(samples[start : start + FRAME_SAMPLES])
            stream.decode()
        stream.feed(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))
        stream.finish()
        stream.decode()
        text = stream.text()
    _ = to_pcm16  # the detector consumes PCM16 internally via _wake_hits
    return hits, text


def bench_aec(
    models: ModelSet, *, device: str | None, scratch: Path, phase_seconds: float = 30.0
) -> AecSection:
    if platform.system() != "Linux" or device is None:
        return AecSection(status="pending", reason=PENDING_REASON)
    if models.wake_json is None:
        return AecSection(
            status="pending", reason="stock wake model not installed; " + PENDING_REASON
        )
    from beepboop_bench import phonemize
    from beepboop_bench.runtime import Synthesizer

    if not models.tts_voices or not phonemize.available():
        return AecSection(
            status="skipped", reason="tts voice or phonemizer missing for the playback clip"
        )
    voice = next(iter(models.tts_voices.values()))
    synth = Synthesizer(voice[0], voice[1], threads=2)
    clip = scratch / "aec_playback.wav"
    write_wav(clip, synth.synth(PLAYBACK_TEXT), synth.sample_rate)
    expected = " ".join(AEC_PHRASES)

    quiet = _capture(device, phase_seconds)
    q_hits, q_text = _score(models, quiet, expected)
    player = _play(device, clip)
    time.sleep(0.5)
    loud = _capture(device, phase_seconds)
    player.wait(timeout=phase_seconds + 15)
    p_hits, p_text = _score(models, loud, expected)
    clip.unlink(missing_ok=True)

    n = len(AEC_PHRASES)
    q_rate, p_rate = q_hits / n, p_hits / n
    q_wer = wer_pct([(expected, q_text)])
    p_wer = wer_pct([(expected, p_text)])
    verified = (
        q_rate > 0
        and p_rate >= 0.9 * q_rate
        and q_wer is not None
        and p_wer is not None
        and p_wer <= q_wer + 5.0
    )
    return AecSection(
        status="measured",
        quiet_hit_rate=q_rate,
        playback_hit_rate=p_rate,
        quiet_wer_pct=q_wer,
        playback_wer_pct=p_wer,
        aec_verified=verified,
        notes=[f"{n} utterances per phase; a person must have spoken them live in both phases"],
    )
