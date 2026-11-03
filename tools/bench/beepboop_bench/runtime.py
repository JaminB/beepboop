"""Typed shims over sherpa-onnx and pymicro-wakeword, the only module that imports them.

The two speech runtimes ship no type information (pybind11 classes and an
untyped Python package), so this file alone relaxes pyright's *unknown-type*
rules; everything it exports is fully typed, and the rest of the bench stays
strict. Thread caps are explicit everywhere (G1/G2 are "2 threads").

Loading a model is slow (hundreds of ms to seconds); the loaders are called
once per worker thread by the stage benchmarks, never per clip.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# Reason: sherpa_onnx and pymicro_wakeword are untyped; this is the one shim module.

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymicro_wakeword
import sherpa_onnx
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures

from beepboop_bench.audio import SAMPLE_RATE, Float32Array
from beepboop_bench.phonemize import phonemize

# ---- speaker ------------------------------------------------------------------


class SpeakerEmbedder:
    """``sherpa_onnx.SpeakerEmbeddingExtractor`` with a thread cap."""

    def __init__(self, model_path: Path, threads: int = 2) -> None:
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=threads, debug=False, provider="cpu"
        )
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.dim: int = int(self._extractor.dim)
        self.threads = threads

    def embed(self, samples: Float32Array, sample_rate: int = SAMPLE_RATE) -> Float32Array:
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate, samples)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            msg = "clip too short for a speaker embedding"
            raise ValueError(msg)
        vec = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec


def cosine(a: Float32Array, b: Float32Array) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


# ---- stt --------------------------------------------------------------------


@dataclass(frozen=True)
class SttFiles:
    tokens: Path
    encoder: Path
    decoder: Path
    joiner: Path
    bpe_vocab: Path | None = None


class SttStream:
    """One streaming recognition session."""

    def __init__(
        self, recognizer: sherpa_onnx.OnlineRecognizer, stream: sherpa_onnx.OnlineStream
    ) -> None:
        self._recognizer = recognizer
        self._stream = stream

    def feed(self, samples: Float32Array, sample_rate: int = SAMPLE_RATE) -> None:
        self._stream.accept_waveform(sample_rate, samples)

    def decode(self) -> int:
        """Run every ready decoding step; returns how many ran."""
        steps = 0
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
            steps += 1
        return steps

    def finish(self) -> None:
        self._stream.input_finished()

    def text(self) -> str:
        return str(self._recognizer.get_result(self._stream))


class SttRecognizer:
    """Streaming zipformer transducer (VOICE_PIPELINE §10.1 settings by default)."""

    def __init__(
        self,
        files: SttFiles,
        *,
        threads: int = 2,
        decoding_method: str = "modified_beam_search",
        max_active_paths: int = 4,
        hotwords_file: Path | None = None,
        hotwords_score: float = 1.5,
    ) -> None:
        if hotwords_file is None:
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(files.tokens),
                encoder=str(files.encoder),
                decoder=str(files.decoder),
                joiner=str(files.joiner),
                num_threads=threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=80,
                decoding_method=decoding_method,
                max_active_paths=max_active_paths,
                enable_endpoint_detection=False,
                provider="cpu",
            )
        else:
            if files.bpe_vocab is None:
                msg = "hotwords need the model's bpe.model (bpe_vocab); this export has none"
                raise ValueError(msg)
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(files.tokens),
                encoder=str(files.encoder),
                decoder=str(files.decoder),
                joiner=str(files.joiner),
                num_threads=threads,
                sample_rate=SAMPLE_RATE,
                feature_dim=80,
                decoding_method=decoding_method,
                max_active_paths=max_active_paths,
                enable_endpoint_detection=False,
                provider="cpu",
                hotwords_file=str(hotwords_file),
                hotwords_score=hotwords_score,
                modeling_unit="bpe",
                bpe_vocab=str(files.bpe_vocab),
            )
        self.threads = threads
        self.decoding_method = decoding_method
        self.max_active_paths = max_active_paths

    def stream(self) -> SttStream:
        return SttStream(self._recognizer, self._recognizer.create_stream())


# ---- tts --------------------------------------------------------------------


class Synthesizer:
    """Piper-format VITS voice in ``sherpa_onnx.OfflineTts``.

    Text is phonemised by :mod:`beepboop_bench.phonemize` (sherpa-onnx 2.0 has
    no built-in espeak frontend); the measured first-chunk latency includes
    that phonemisation, as the product's would.
    """

    def __init__(self, model: Path, tokens: Path, *, threads: int = 2) -> None:
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=str(model), lexicon="", tokens=str(tokens)
        )
        model_config = sherpa_onnx.OfflineTtsModelConfig(
            vits=vits, num_threads=threads, provider="cpu"
        )
        config = sherpa_onnx.OfflineTtsConfig(model=model_config, max_num_sentences=1)
        self._tts = sherpa_onnx.OfflineTts(config)
        self.sample_rate: int = int(self._tts.sample_rate)
        self.voice: str = str(self._tts.lang) or "en-us"
        self.threads = threads

    def synth(
        self,
        text: str,
        *,
        speed: float = 1.0,
        on_chunk: Callable[[int, float], None] | None = None,
    ) -> Float32Array:
        """Synthesise ``text``; ``on_chunk(n_samples, t_perf)`` fires per generated sentence."""
        config = sherpa_onnx.GenerationConfig()
        config.sid = 0
        config.speed = speed
        config.phoneme_codepoints = phonemize(text, _espeak_voice(self.voice))

        def callback(samples, progress) -> int:  # pybind callback, untyped
            if on_chunk is not None:
                on_chunk(len(samples), time.perf_counter())
            return 1

        audio = self._tts.generate("", config, callback)
        return np.asarray(audio.samples, dtype=np.float32)


def _espeak_voice(model_lang: str) -> str:
    """Model metadata says e.g. ``English``; espeak wants ``en-us``."""
    lowered = model_lang.lower()
    return lowered if "-" in lowered else {"english": "en-us"}.get(lowered, "en-us")


# ---- vad --------------------------------------------------------------------


class Vad:
    """Silero VAD through sherpa-onnx, 512-sample (32 ms) windows.

    ``min_silence_duration`` is 0 so ``is_speech()`` flips as soon as a window is
    judged silent; the bench applies the VOICE_PIPELINE §7.2 hangover itself and
    so can report the last VAD-positive frame (clock zero) exactly.
    """

    WINDOW = 512

    def __init__(self, model_path: Path, *, threshold: float = 0.5, threads: int = 1) -> None:
        silero = sherpa_onnx.SileroVadModelConfig(
            model=str(model_path),
            threshold=threshold,
            min_silence_duration=0.0,
            min_speech_duration=0.05,
            window_size=self.WINDOW,
            max_speech_duration=30.0,
        )
        config = sherpa_onnx.VadModelConfig(
            silero_vad=silero, sample_rate=SAMPLE_RATE, num_threads=threads, provider="cpu"
        )
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)
        self._pending: Float32Array = np.zeros(0, dtype=np.float32)

    def push(self, samples: Float32Array) -> list[bool]:
        """Feed audio; returns one speech flag per complete 32 ms window consumed."""
        buf = np.concatenate([self._pending, samples]) if len(self._pending) else samples
        flags: list[bool] = []
        n_full = len(buf) // self.WINDOW
        for i in range(n_full):
            self._vad.accept_waveform(buf[i * self.WINDOW : (i + 1) * self.WINDOW])
            flags.append(bool(self._vad.is_speech_detected()))
            while not self._vad.empty():
                self._vad.pop()
        self._pending = buf[n_full * self.WINDOW :]
        return flags

    def reset(self) -> None:
        self._vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)


# ---- wake -------------------------------------------------------------------


class WakeDetector:
    """microWakeWord model (stock ``hey_beepboop`` or a pymicro-wakeword builtin)."""

    def __init__(self, config_json: Path | None) -> None:
        if config_json is None:
            self._model = MicroWakeWord.from_builtin(pymicro_wakeword.Model.OKAY_NABU)
            self.stock = False
        else:
            self._model = MicroWakeWord.from_config(config_json)
            self.stock = True
        self._features = MicroWakeWordFeatures()
        self.model_id: str = str(self._model.id)
        self.wake_word: str = str(self._model.wake_word)
        self.probability_cutoff: float = float(self._model.probability_cutoff)
        self.sliding_window_size: int = int(self._model.sliding_window_size)

    def process(self, pcm16: bytes) -> list[float]:
        """Feed 16 kHz 16-bit mono PCM; returns the probability after each 10 ms feature step."""
        probs: list[float] = []
        for features in self._features.process_streaming(pcm16):
            prob = self._model.process_streaming_prob(features)
            probs.append(float(prob) if prob is not None else 0.0)
        return probs

    def reset(self) -> None:
        self._model.reset()
        self._features.reset()
