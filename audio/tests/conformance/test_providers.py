"""Audio provider conformance (VOICE_PIPELINE §17, CLAUDE.md §7.1) against the real v0.1 models.

Run one provider with ``uv run pytest -m conformance audio/tests -k <id>``. Checks:
load, warm-up, streaming, cancellation mid-stream, locale handling and a memory
ceiling. The ceilings here are x86 smoke bounds that catch a leak or a wrong
model, **not** Pi measurements; RAM on the Pi is ``bench ram`` (ROADMAP G4,
pending on Pi). Each test skips when its model is not installed
(``python -m beepboop_bench.models fetch``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import psutil
import pytest

from beepboop_audio._audiofile import read_audio, resample
from beepboop_audio.config import WakeModelConfig
from beepboop_audio.frames import FRAME_SAMPLES, SAMPLE_RATE, to_int16
from beepboop_audio.models import (
    SPEAKER_CAMPP,
    STT_ZIPFORMER,
    STT_ZIPFORMER_BPE,
    VAD_SILERO,
    ModelStore,
)
from beepboop_audio.testing import silence, split_frames, voice
from beepboop_audio.types import PCM16, Final, Partial, SttContext, VoiceRef

pytestmark = pytest.mark.conformance

MODELS = Path(__file__).resolve().parents[3] / "models"
STORE = ModelStore(MODELS)
MB = 1024 * 1024


def needs(*ids: str) -> pytest.MarkDecorator:
    missing = [i for i in ids if not STORE.available(i)]
    return pytest.mark.skipif(bool(missing), reason=f"model not installed: {', '.join(missing)}")


def rss() -> int:
    return int(psutil.Process().memory_info().rss)


def load_wav(name: str) -> PCM16:
    data, rate = read_audio(STORE.get(STT_ZIPFORMER).dest / "test_wavs" / name)
    return to_int16(resample(np.asarray(data[:, 0], dtype=np.float32), rate, SAMPLE_RATE))


async def frames_of(pcm: PCM16, *, delay: float = 0.0) -> AsyncIterator[PCM16]:
    for frame in split_frames(pcm):
        if delay:
            await asyncio.sleep(delay)
        yield frame


def ctx(*hotwords: str, mode: str = "command") -> SttContext:
    return SttContext(
        locale="en-US",
        hotwords=hotwords,
        hotwords_score=1.5,
        mode="pin" if mode == "pin" else "command",
    )


# ------------------------------------------------------------- microwakeword --


def test_microwakeword_load_stream_reset() -> None:
    from beepboop_audio.wake.microwakeword import MicroWakeWordProvider

    before = rss()
    provider = MicroWakeWordProvider([WakeModelConfig(id="okay_nabu")])
    assert rss() - before < 100 * MB
    frames = split_frames(voice(200, 2000)) + split_frames(silence(1000))
    for f in frames[:50]:  # warm-up
        provider.process(f)
    start = time.perf_counter()
    for f in frames:
        provider.process(f)
    per_frame_ms = (time.perf_counter() - start) * 1000 / len(frames)
    assert per_frame_ms < 10.0  # must return within its 10 ms frame (x86 smoke)
    provider.reset()


# --------------------------------------------------------------- sherpa-campp --


@needs(SPEAKER_CAMPP)
def test_sherpa_campp_embeds_normalised_vectors() -> None:
    from beepboop_audio.speaker.sherpa_campp import CamppProvider

    before = rss()
    provider = CamppProvider(STORE, threads=2)
    assert rss() - before < 400 * MB
    assert provider.dim == 512
    assert provider.model_id == SPEAKER_CAMPP
    assert len(provider.model_sha) == 64
    a = load_wav("0.wav")[: 2 * SAMPLE_RATE]
    e1 = provider.embed(a)
    e2 = provider.embed(a)
    assert e1.shape == (512,)
    assert e1.dtype == np.float32
    assert float(np.linalg.norm(e1)) == pytest.approx(1.0, abs=1e-4)
    assert float(e1 @ e2) == pytest.approx(1.0, abs=1e-4)  # deterministic
    with pytest.raises(ValueError, match="too short"):
        provider.embed(np.zeros(10, dtype=np.int16))


# ----------------------------------------------------------- sherpa-zipformer --


@pytest.fixture(scope="module")
def zipformer() -> object:
    if not STORE.available(STT_ZIPFORMER):
        pytest.skip("zipformer not installed")
    from beepboop_audio.stt.sherpa_zipformer import ZipformerProvider

    return ZipformerProvider(STORE, threads=2)


async def collect(stream: AsyncIterator[Partial | Final]) -> tuple[list[Partial], list[Final]]:
    partials: list[Partial] = []
    finals: list[Final] = []
    async for item in stream:
        (partials if isinstance(item, Partial) else finals).append(item)  # type: ignore[arg-type]
    return partials, finals


async def test_sherpa_zipformer_streams_one_final(zipformer: object) -> None:
    from beepboop_audio.stt.sherpa_zipformer import ZipformerProvider

    assert isinstance(zipformer, ZipformerProvider)
    assert "en-US" in zipformer.locales
    assert zipformer.streaming
    partials, finals = await collect(zipformer.stream(frames_of(load_wav("0.wav")), ctx()))
    assert len(finals) == 1
    final = finals[0]
    assert "yellow lamps" in final.text
    assert final.text == final.text.lower()
    assert final.words
    assert final.words[0][1] <= final.words[-1][2]
    assert final.t_eos_ns <= final.t_final_ns
    assert partials
    assert all(p.text for p in partials)
    assert 0.0 < zipformer.rtf < 1.0


async def test_sherpa_zipformer_empty_audio(zipformer: object) -> None:
    from beepboop_audio.stt.sherpa_zipformer import ZipformerProvider

    assert isinstance(zipformer, ZipformerProvider)
    _, finals = await collect(zipformer.stream(frames_of(silence(500)), ctx()))
    assert len(finals) == 1
    assert finals[0].text == ""
    assert finals[0].confidence == 0.0


@needs(STT_ZIPFORMER_BPE)
async def test_sherpa_zipformer_hotwords_bias(zipformer: object) -> None:
    """The pinned bpe.model makes per-profile hotwords work (VOICE_PIPELINE §10.2)."""
    from beepboop_audio.stt.sherpa_zipformer import ZipformerProvider

    assert isinstance(zipformer, ZipformerProvider)
    assert zipformer.hotwords_enabled
    # LibriSpeech "... THE SQUALID QUARTER OF THE BROTHELS"; the 20M model alone says "braffls".
    audio = load_wav("0.wav")
    _, plain = await collect(zipformer.stream(frames_of(audio), ctx()))
    _, biased = await collect(zipformer.stream(frames_of(audio), ctx("quarter of the brothels")))
    assert "brothels" not in plain[0].text
    assert biased[0].text.endswith("quarter of the brothels")


async def test_sherpa_zipformer_pin_mode_emits_no_partials(zipformer: object) -> None:
    from beepboop_audio.stt.sherpa_zipformer import ZipformerProvider

    assert isinstance(zipformer, ZipformerProvider)
    partials, finals = await collect(
        zipformer.stream(frames_of(load_wav("0.wav")[: 3 * SAMPLE_RATE]), ctx(mode="pin"))
    )
    assert partials == []
    assert len(finals) == 1


async def test_sherpa_zipformer_cancels_mid_stream(zipformer: object) -> None:
    from beepboop_audio.stt.sherpa_zipformer import ZipformerProvider

    assert isinstance(zipformer, ZipformerProvider)
    stream = zipformer.stream(frames_of(load_wav("1.wav"), delay=0.001), ctx())
    assert isinstance(stream, AsyncIterator)
    first = await anext(stream)
    assert isinstance(first, Partial)
    start = time.perf_counter()
    await stream.aclose()  # type: ignore[attr-defined]
    assert (time.perf_counter() - start) < 0.05


# --------------------------------------------------------------- sherpa-piper --


@pytest.fixture(scope="module")
def piper() -> object:
    from beepboop_audio.tts.sherpa_piper import PiperProvider

    provider = PiperProvider(STORE, threads=2)
    if not provider.voices():
        pytest.skip("no Piper voice installed")
    return provider


async def test_sherpa_piper_synthesises_16k_chunks(piper: object) -> None:
    from beepboop_audio.tts.sherpa_piper import PiperProvider

    assert isinstance(piper, PiperProvider)
    voice_ref = piper.voices()[0]
    assert voice_ref.locale in piper.locales
    before = rss()
    chunks = [c async for c in piper.synth("The kitchen lights are off.", voice_ref)]
    assert rss() - before < 400 * MB
    assert chunks
    assert all(c.dtype == np.int16 and 0 < len(c) <= SAMPLE_RATE // 10 for c in chunks)
    total = sum(len(c) for c in chunks)
    assert 0.5 * SAMPLE_RATE < total < 5 * SAMPLE_RATE


async def test_sherpa_piper_rate_changes_length(piper: object) -> None:
    from beepboop_audio.tts.sherpa_piper import PiperProvider

    assert isinstance(piper, PiperProvider)
    vid = piper.voices()[0].voice_id
    text = "Set a timer for ten minutes."
    slow = [len(c) async for c in piper.synth(text, VoiceRef(vid, 0.7, "en-US"))]
    fast = [len(c) async for c in piper.synth(text, VoiceRef(vid, 1.4, "en-US"))]
    assert sum(fast) < sum(slow)


async def test_sherpa_piper_cancels_mid_stream(piper: object) -> None:
    from beepboop_audio.tts.sherpa_piper import PiperProvider

    assert isinstance(piper, PiperProvider)
    long_text = "This is a long sentence that keeps going for quite a while so we can cancel it."
    stream = piper.synth(long_text, piper.voices()[0])
    first = await anext(stream)
    assert len(first) > 0
    start = time.perf_counter()
    await stream.aclose()  # type: ignore[attr-defined]
    assert (time.perf_counter() - start) < 0.05


async def test_sherpa_piper_unknown_voice_raises(piper: object) -> None:
    from beepboop_audio.tts.sherpa_piper import PiperProvider

    assert isinstance(piper, PiperProvider)
    with pytest.raises(KeyError):
        _ = [c async for c in piper.synth("hello", VoiceRef("nope", 1.0, "en-US"))]


# ---------------------------------------------------------------------- VAD ---


@needs(VAD_SILERO)
def test_silero_vad_separates_speech_from_silence() -> None:
    from beepboop_audio.pipeline.vad import SileroVad, VadFramer

    framer = VadFramer(SileroVad(STORE.get(VAD_SILERO).file("model")))
    speech_flags = [framer.push(f) for f in split_frames(load_wav("0.wav")[: 2 * SAMPLE_RATE])]
    framer.reset()
    silent_flags = [framer.push(f) for f in split_frames(silence(1000))]
    assert sum(speech_flags) > len(speech_flags) // 2
    assert not any(silent_flags)
    assert FRAME_SAMPLES == 160
