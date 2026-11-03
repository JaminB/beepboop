"""Smoke tests through the real models (skipped until ``python -m beepboop_bench.models fetch``).

These run on x86 too and prove the shims load sherpa-onnx / pymicro-wakeword
models with the configured thread caps; they make no claim about latency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from beepboop_bench import phonemize
from beepboop_bench.audio import SAMPLE_RATE, silence, to_pcm16
from beepboop_bench.models import load_manifest, manifest_path, missing, models_root

if TYPE_CHECKING:
    from beepboop_bench.stages import ModelSet

ROOT = models_root()
ALL_PRESENT = manifest_path(ROOT).is_file() and not missing(
    load_manifest(manifest_path(ROOT)), ROOT
)
needs_models = pytest.mark.skipif(
    not ALL_PRESENT, reason="models not fetched (python -m beepboop_bench.models fetch)"
)
needs_phonemizer = pytest.mark.skipif(not phonemize.available(), reason=phonemize.MISSING_REASON)


@pytest.fixture(scope="module")
def model_set() -> ModelSet:  # the stages import is heavy; keep it inside
    from beepboop_bench.stages import resolve_models

    return resolve_models(load_manifest(manifest_path(ROOT)), ROOT)


@needs_models
@needs_phonemizer
def test_tts_speaker_stt_vad_wake_round_trip(model_set: ModelSet) -> None:
    from beepboop_bench.runtime import (
        SpeakerEmbedder,
        SttRecognizer,
        Synthesizer,
        Vad,
        WakeDetector,
        cosine,
    )

    assert model_set.speaker is not None
    assert model_set.stt is not None
    assert model_set.vad is not None
    assert model_set.tts_voices
    model, tokens = next(iter(model_set.tts_voices.values()))
    tts = Synthesizer(model, tokens, threads=2)
    marks: list[float] = []
    audio = tts.synth("Turn off the kitchen lights.", on_chunk=lambda _n, t: marks.append(t))
    assert tts.sample_rate == SAMPLE_RATE
    assert 0.5 < len(audio) / SAMPLE_RATE < 4.0
    assert len(marks) == 1

    spk = SpeakerEmbedder(model_set.speaker, threads=2)
    e1 = spk.embed(audio)
    assert e1.shape == (spk.dim,)
    assert cosine(e1, spk.embed(audio)) == pytest.approx(1.0, abs=1e-4)

    stt = SttRecognizer(model_set.stt, threads=2)
    stream = stt.stream()
    stream.feed(audio)
    stream.decode()
    stream.feed(silence(0.3))
    stream.finish()
    stream.decode()
    text = stream.text().lower()
    assert "kitchen" in text or "light" in text

    vad = Vad(model_set.vad)
    flags = vad.push(np.concatenate([silence(0.5), audio, silence(0.5)]))
    assert any(flags)
    assert not flags[0]

    wake = WakeDetector(model_set.wake_json)
    probs = wake.process(to_pcm16(np.concatenate([silence(0.3), audio, silence(0.3)])))
    assert len(probs) > 50
    assert max(probs) < wake.probability_cutoff  # a command is not the wake word
