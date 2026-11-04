"""Typed shims over sherpa-onnx, the only module in ``beepboop_audio`` that imports it.

sherpa-onnx ships pybind11 classes with no type information, so this file alone
relaxes pyright's unknown-type rules; everything it exports is fully typed.
Every object is created with an explicit thread count (VOICE_PIPELINE §3) and
``provider="cpu"``. sherpa-onnx is imported lazily so that importing the audio
package (and the fakes the tests use) never loads the runtime.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportAttributeAccessIssue=false
# Reason: sherpa_onnx is untyped; this is the one shim module.

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from beepboop_audio.frames import SAMPLE_RATE
from beepboop_audio.types import Embedding, Float32


@cache
def _so() -> Any:  # the untyped runtime module
    return importlib.import_module("sherpa_onnx")


# ---- speaker ------------------------------------------------------------------


class SherpaEmbedder:
    """``SpeakerEmbeddingExtractor`` with a thread cap."""

    def __init__(self, model_path: Path, threads: int = 2) -> None:
        so = _so()
        config = so.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=threads, debug=False, provider="cpu"
        )
        self._extractor = so.SpeakerEmbeddingExtractor(config)
        self.dim: int = int(self._extractor.dim)

    def embed(self, samples: Float32) -> Embedding:
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            msg = "clip too short for a speaker embedding"
            raise ValueError(msg)
        vec = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec


# ---- vad --------------------------------------------------------------------


class SherpaSilero:
    """Silero VAD, 512-sample (32 ms) windows; returns the speech probability per window.

    sherpa's ``VoiceActivityDetector`` applies its own min-silence/min-speech
    smoothing; the endpointing rules of VOICE_PIPELINE §7.2 are applied by
    :mod:`beepboop_audio.pipeline.endpoint` instead, so smoothing is set to the
    minimum here and ``is_speech_detected`` is read after every window.
    """

    WINDOW = 512

    def __init__(self, model_path: Path, *, threshold: float = 0.5) -> None:
        so = _so()
        silero = so.SileroVadModelConfig(
            model=str(model_path),
            threshold=threshold,
            min_silence_duration=0.0,
            min_speech_duration=0.0,
            window_size=self.WINDOW,
            max_speech_duration=30.0,
        )
        config = so.VadModelConfig(
            silero_vad=silero, sample_rate=SAMPLE_RATE, num_threads=1, provider="cpu"
        )
        self._vad = so.VoiceActivityDetector(config, buffer_size_in_seconds=2)

    def window(self, samples: Float32) -> bool:
        self._vad.accept_waveform(samples)
        speech = bool(self._vad.is_speech_detected())
        while not self._vad.empty():  # drop the copied segments; PCM is not kept
            self._vad.pop()
        return speech

    def reset(self) -> None:
        self._vad.reset()


# ---- stt --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZipformerFiles:
    tokens: Path
    encoder: Path
    decoder: Path
    joiner: Path


class SherpaOnlineStream:
    """One streaming recognition session; PCM is fed and never retained here."""

    def __init__(self, recognizer: Any, stream: Any) -> None:
        self._recognizer = recognizer
        self._stream = stream

    def feed(self, samples: Float32) -> None:
        self._stream.accept_waveform(SAMPLE_RATE, samples)

    def decode(self) -> int:
        steps = 0
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
            steps += 1
        return steps

    def finish(self) -> None:
        self._stream.input_finished()

    def is_endpoint(self) -> bool:
        return bool(self._recognizer.is_endpoint(self._stream))

    def text(self) -> str:
        return str(self._recognizer.get_result(self._stream))

    def tokens_and_times(self) -> tuple[list[str], list[float]]:
        result = self._recognizer.get_result_all(self._stream)
        return [str(t) for t in result.tokens], [float(t) for t in result.timestamps]


class SherpaZipformer:
    """Streaming zipformer transducer with the VOICE_PIPELINE §10.1/§7.2 settings.

    Hotwords use ``modeling_unit="bpe"``: sherpa-onnx segments each per-stream
    hotword phrase with the SentencePiece vocabulary in ``bpe_vocab`` (written
    from the pinned ``bpe.model`` by ``beepboop_audio.stt.sherpa_zipformer.bpe``).
    Without ``bpe_vocab`` the recognizer runs unbiased.
    """

    def __init__(
        self,
        files: ZipformerFiles,
        *,
        threads: int = 2,
        max_active_paths: int = 4,
        hotwords_score: float = 1.5,
        bpe_vocab: Path | None = None,
    ) -> None:
        so = _so()
        unit: dict[str, str] = (
            {"modeling_unit": "bpe", "bpe_vocab": str(bpe_vocab)} if bpe_vocab is not None else {}
        )
        self._recognizer = so.OnlineRecognizer.from_transducer(
            tokens=str(files.tokens),
            encoder=str(files.encoder),
            decoder=str(files.decoder),
            joiner=str(files.joiner),
            num_threads=threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="modified_beam_search",
            max_active_paths=max_active_paths,
            hotwords_score=hotwords_score,
            **unit,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.0,
            rule2_min_trailing_silence=0.6,
            rule3_min_utterance_length=15.0,
            provider="cpu",
        )

    def stream(self, hotwords: str | None = None) -> SherpaOnlineStream:
        if hotwords:
            return SherpaOnlineStream(self._recognizer, self._recognizer.create_stream(hotwords))
        return SherpaOnlineStream(self._recognizer, self._recognizer.create_stream())


# ---- tts --------------------------------------------------------------------


class SherpaVits:
    """Piper-format VITS voice in ``OfflineTts``, driven by phoneme codepoints (§11.1)."""

    def __init__(self, model: Path, tokens: Path, *, threads: int = 2) -> None:
        so = _so()
        vits = so.OfflineTtsVitsModelConfig(model=str(model), lexicon="", tokens=str(tokens))
        model_config = so.OfflineTtsModelConfig(vits=vits, num_threads=threads, provider="cpu")
        config = so.OfflineTtsConfig(model=model_config, max_num_sentences=1)
        self._tts = so.OfflineTts(config)
        self.sample_rate: int = int(self._tts.sample_rate)
        self.lang: str = str(self._tts.lang)

    def generate(
        self,
        codepoints: list[list[int]],
        *,
        speed: float,
        on_chunk: Callable[[Float32], bool],
    ) -> None:
        """Synthesise; ``on_chunk`` receives each generated piece and returns False to stop."""
        so = _so()
        config = so.GenerationConfig()
        config.sid = 0
        config.speed = speed
        config.phoneme_codepoints = codepoints

        def callback(samples, _progress) -> int:  # pybind callback
            keep_going = on_chunk(np.asarray(samples, dtype=np.float32).copy())
            return 1 if keep_going else 0

        self._tts.generate("", config, callback)


__all__ = [
    "SherpaEmbedder",
    "SherpaOnlineStream",
    "SherpaSilero",
    "SherpaVits",
    "SherpaZipformer",
    "ZipformerFiles",
]
