"""``SttProvider`` for the streaming zipformer transducer (VOICE_PIPELINE §10).

``modified_beam_search`` (required for hotwords), ``max_active_paths: 4``, 2
intra-op threads, int8 weights. Audio arrives in 10 ms frames and is decoded
every 100 ms of accumulated audio on the provider's own worker thread, never
on the event loop. When the input iterator ends (the pipeline's end of
speech), the stream is flushed with 0.3 s of synthetic silence and exactly one
``Final`` is emitted. The zipformer's own endpoint rules are the backstop
(§7.2): when they fire, input stops early and the ``Final`` follows.

Hotwords (§10.2): the per-stream list is passed upper-cased to
``create_stream`` and segmented by sherpa-onnx with the vocabulary rendered
from the pinned ``bpe.model``. Without that model installed the stream runs
unbiased (logged once). ``mode="pin"`` biases the digit words and the caller
must not emit anything decoded in that mode (§12).
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import structlog

from beepboop_audio._sherpa import SherpaOnlineStream, SherpaZipformer, ZipformerFiles
from beepboop_audio.frames import SAMPLE_RATE, to_float32
from beepboop_audio.models import STT_ZIPFORMER, STT_ZIPFORMER_BPE, ModelMissingError, ModelStore
from beepboop_audio.stt.sherpa_zipformer.bpe import (
    BpeEncoder,
    hotwords_argument,
    read_pieces,
    vocab_text,
)
from beepboop_audio.types import PCM16, Final, Float32, Partial, SttContext

log = structlog.get_logger(__name__)

DECODE_EVERY_SAMPLES = SAMPLE_RATE // 10  # 100 ms
FLUSH_SILENCE_SAMPLES = int(0.3 * SAMPLE_RATE)
#: Words biased in ``mode="pin"`` (VOICE_PIPELINE §12: "zero"…"nine", "oh").
PIN_HOTWORDS: tuple[str, ...] = (
    "zero",
    "oh",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)
PIN_HOTWORDS_SCORE = 2.5  # target


def words_from_tokens(
    tokens: list[str], times_s: list[float], t0_ns: int, t_end_ns: int
) -> tuple[tuple[str, int, int], ...]:
    """Group BPE pieces into words with capture-clock stamps.

    A piece that starts with ``▁`` (or with a space: sherpa-onnx reports pieces with
    the boundary already turned into a space) starts a new word.
    """
    words: list[tuple[str, int, int]] = []
    current = ""
    start_ns = t0_ns
    for i, piece in enumerate(tokens):
        t_ns = t0_ns + int(times_s[i] * 1e9) if i < len(times_s) else t_end_ns
        boundary = piece.startswith(("▁", " "))
        if boundary and current:
            words.append((current.lower(), start_ns, max(start_ns, t_ns)))
            current = ""
        if not current:
            start_ns = t_ns
        current += piece.lstrip("▁ ")
    if current:
        words.append((current.lower(), start_ns, max(start_ns, t_end_ns)))
    return tuple(words)


class ZipformerProvider:
    id = "sherpa-zipformer"
    license = "Apache-2.0"
    streaming = True
    locales: tuple[str, ...] = ("en-US", "en-GB", "en")

    def __init__(
        self,
        store: ModelStore,
        *,
        threads: int = 2,
        max_active_paths: int = 4,
        hotwords_score: float = 1.5,
        int8: bool = True,
    ) -> None:
        entry = store.get(STT_ZIPFORMER)
        suffix = "int8" if int8 else "fp32"
        files = ZipformerFiles(
            tokens=entry.file("tokens"),
            encoder=entry.file(f"encoder_{suffix}"),
            decoder=entry.file(f"decoder_{suffix}"),
            joiner=entry.file(f"joiner_{suffix}"),
        )
        self._encoder: BpeEncoder | None = None
        vocab_dir: tempfile.TemporaryDirectory[str] | None = None
        vocab: Path | None = None
        try:
            bpe_model = store.get(STT_ZIPFORMER_BPE).file("model")
        except ModelMissingError:
            log.warning("stt.hotwords_disabled", reason="bpe.model not installed")
        else:
            pieces = read_pieces(bpe_model.read_bytes())
            self._encoder = BpeEncoder(pieces)
            # sherpa-onnx reads bpe.vocab once, at construction; the text is derived from
            # the model file (not audio) and the temporary file is removed right after.
            vocab_dir = tempfile.TemporaryDirectory(prefix="bb-bpe-")
            vocab = Path(vocab_dir.name) / "bpe.vocab"
            vocab.write_text(vocab_text(pieces), encoding="utf-8")
        try:
            self._model = SherpaZipformer(
                files,
                threads=threads,
                max_active_paths=max_active_paths,
                hotwords_score=hotwords_score,
                bpe_vocab=vocab,
            )
        finally:
            if vocab_dir is not None:
                vocab_dir.cleanup()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bb-stt")
        self.rtf = 0.0

    @property
    def hotwords_enabled(self) -> bool:
        return self._encoder is not None

    def _hotwords(self, ctx: SttContext) -> str | None:
        if self._encoder is None:
            return None
        phrases = PIN_HOTWORDS if ctx.mode == "pin" else ctx.hotwords
        if not phrases:
            return None
        return hotwords_argument(self._encoder, phrases) or None

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def stream(
        self, audio: AsyncIterator[PCM16], ctx: SttContext
    ) -> AsyncIterator[Partial | Final]:
        loop = asyncio.get_running_loop()
        hotwords = self._hotwords(ctx)
        stream: SherpaOnlineStream = await loop.run_in_executor(
            self._executor, self._model.stream, hotwords
        )
        t0_ns = time.monotonic_ns()
        pending: list[Float32] = []
        pending_n = 0
        fed = 0
        busy_ns = 0
        last_text = ""
        endpoint = False

        def feed_and_decode(chunk: Float32) -> tuple[str, bool, int]:
            start = time.perf_counter_ns()
            stream.feed(chunk)
            stream.decode()
            return stream.text(), stream.is_endpoint(), time.perf_counter_ns() - start

        async for frame in audio:
            pending.append(to_float32(frame))
            pending_n += len(frame)
            if pending_n < DECODE_EVERY_SAMPLES:
                continue
            chunk = np.concatenate(pending)
            pending, pending_n = [], 0
            fed += len(chunk)
            text, endpoint, spent = await loop.run_in_executor(
                self._executor, feed_and_decode, chunk
            )
            busy_ns += spent
            if ctx.mode == "command" and text and text != last_text:
                last_text = text
                yield Partial(text=text.lower(), stable=False, t_ns=time.monotonic_ns())
            if endpoint:
                break
        t_eos_ns = time.monotonic_ns()
        tail = np.concatenate([*pending, np.zeros(FLUSH_SILENCE_SAMPLES, dtype=np.float32)])
        fed += sum(len(p) for p in pending)

        def flush() -> tuple[str, list[str], list[float], int]:
            start = time.perf_counter_ns()
            stream.feed(tail)
            stream.finish()
            stream.decode()
            tokens, times = stream.tokens_and_times()
            return stream.text(), tokens, times, time.perf_counter_ns() - start

        text, tokens, times, spent = await loop.run_in_executor(self._executor, flush)
        busy_ns += spent
        t_final_ns = time.monotonic_ns()
        if fed:
            self.rtf = busy_ns / (fed / SAMPLE_RATE * 1e9)
        text = text.strip().lower()
        yield Final(
            text=text,
            confidence=1.0 if text else 0.0,
            words=words_from_tokens(tokens, times, t0_ns, t_eos_ns) if text else (),
            t_eos_ns=t_eos_ns,
            t_final_ns=t_final_ns,
        )


__all__ = ["PIN_HOTWORDS", "ZipformerProvider", "words_from_tokens"]
