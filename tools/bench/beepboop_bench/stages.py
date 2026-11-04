"""The per-stage benchmarks: speaker, stt, tts, wake, endpoint.

Each ``bench_*`` function loads real models through :mod:`beepboop_bench.runtime`
with explicit thread caps, replays corpus clips, and returns the matching
section of the result schema. They report numbers only; the gate table in
:mod:`beepboop_bench.gates` judges them.

Timing uses ``time.perf_counter`` around the inference call that the product
would make at that point of a turn; nothing is simulated. When a stage cannot
run (model missing, no phonemizer, no clips of the needed kind) the section
says so in ``status``/``reason`` instead of carrying a number.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from beepboop.log import get_logger
from beepboop_bench import phonemize
from beepboop_bench.audio import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    Float32Array,
    duration_s,
    frames,
    last_seconds,
    read_wav,
    silence,
    to_pcm16,
)
from beepboop_bench.corpus import Clip, Corpus
from beepboop_bench.models import Manifest, is_installed
from beepboop_bench.runtime import (
    SpeakerEmbedder,
    SttFiles,
    SttRecognizer,
    Synthesizer,
    Vad,
    WakeDetector,
    cosine,
)
from beepboop_bench.schema import (
    EndpointSection,
    HangoverResult,
    SpeakerSection,
    SpeakerVariant,
    SttSection,
    SttThreads,
    TtsSection,
    TtsVoice,
    WakePerSpeaker,
    WakeSection,
)
from beepboop_bench.stats import maybe_summarize
from beepboop_bench.wer import wer_pct

log = get_logger(__name__)

#: VOICE_PIPELINE §10.1: the stream is flushed with 0.3 s of synthetic silence at t_eos.
FLUSH_SILENCE_S = 0.3
#: VOICE_PIPELINE §10.1: decoding runs every 100 ms of accumulated audio.
DECODE_EVERY_FRAMES = 10
#: VOICE_PIPELINE §17.1 wake refractory.
WAKE_REFRACTORY_S = 2.0
#: VOICE_PIPELINE §7.2 hangover sweep (bench endpoint).
HANGOVERS_MS: tuple[int, ...] = (200, 250, 300, 400)

#: 20 sentences of 6-20 words for ``bench tts`` (VOICE_PIPELINE §16).
TTS_SENTENCES: tuple[str, ...] = (
    "The kitchen lights are off now.",
    "I set a timer for five minutes.",
    "It is twenty one degrees outside with light rain expected this evening.",
    "The front door has been unlocked since a quarter past three.",
    "Milk, eggs and bread are on the shopping list.",
    "Your next meeting starts in fifteen minutes in the blue room.",
    "The living room is at twenty degrees and the heating is on.",
    "I could not reach the weather service, so here is yesterday's forecast.",
    "There are three timers running: pasta, laundry and the oven.",
    "The garage door is still open; would you like me to close it?",
    "Good morning. You have two reminders and one unread message today.",
    "Playing the kitchen playlist in the kitchen at thirty percent volume.",
    "The bedroom thermostat is set to eighteen degrees for the night.",
    "Sorry, I did not catch that. Could you say it again?",
    "The back door locked itself at eleven because nobody was home.",
    "Tomorrow looks sunny with a high of twenty six degrees in the afternoon.",
    "I added paper towels to the household list for you.",
    "The dishwasher finished its cycle about ten minutes ago.",
    "The office lamp is on, and the desk fan is running at low speed.",
    "Your laundry timer has twelve minutes left, and the oven timer has four.",
)


@dataclass(frozen=True)
class ModelSet:
    """Resolved model paths; ``None`` where the manifest entry is not installed."""

    speaker: Path | None
    stt: SttFiles | None
    stt_fp32: SttFiles | None
    vad: Path | None
    tts_voices: dict[str, tuple[Path, Path]]
    wake_json: Path | None
    ids: dict[str, str]


def resolve_models(manifest: Manifest, root: Path) -> ModelSet:
    speaker = stt = stt_fp32 = vad = wake = None
    voices: dict[str, tuple[Path, Path]] = {}
    ids: dict[str, str] = {}
    for entry in manifest.models:
        if not is_installed(entry, root):
            continue
        if entry.kind == "stt" and "tokens" not in entry.files:
            continue  # a companion file (the zipformer's bpe.model), not a recognizer
        ids[entry.kind if entry.kind != "tts" else f"tts:{entry.id}"] = entry.id
        if entry.kind == "speaker":
            speaker = entry.install_path(root)
        elif entry.kind == "stt":
            bpe = entry.file(root, "bpe_vocab") if "bpe_vocab" in entry.files else None
            stt = SttFiles(
                entry.file(root, "tokens"),
                entry.file(root, "encoder_int8"),
                entry.file(root, "decoder_int8"),
                entry.file(root, "joiner_int8"),
                bpe,
            )
            if "encoder_fp32" in entry.files:
                stt_fp32 = SttFiles(
                    entry.file(root, "tokens"),
                    entry.file(root, "encoder_fp32"),
                    entry.file(root, "decoder_fp32"),
                    entry.file(root, "joiner_fp32"),
                    bpe,
                )
        elif entry.kind == "vad":
            vad = entry.install_path(root)
        elif entry.kind == "tts":
            voices[entry.id.removeprefix("vits-piper-")] = (
                entry.file(root, "model"),
                entry.file(root, "tokens"),
            )
        elif entry.kind == "wake":
            wake = entry.file(root, "config")
    return ModelSet(speaker, stt, stt_fp32, vad, voices, wake, ids)


def _load_clip(corpus: Corpus, clip: Clip) -> Float32Array:
    return read_wav(corpus.clip_path(clip), channel=corpus.manifest.capture_channel)


# ---- speaker (G1) ------------------------------------------------------------


class _SttLoad:
    """Keeps a zipformer stream busy on another thread (G1: "concurrent with STT")."""

    def __init__(self, files: SttFiles, clips: Sequence[Float32Array], threads: int = 2) -> None:
        self._recognizer = SttRecognizer(files, threads=threads)
        self._clips = clips
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stt-load", daemon=True)

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set() and self._clips:
            stream = self._recognizer.stream()
            for frame in frames(self._clips[i % len(self._clips)]):
                stream.feed(frame)
                stream.decode()
                if self._stop.is_set():
                    break
            i += 1

    def __enter__(self) -> _SttLoad:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


def eer_pct(same: Sequence[float], different: Sequence[float]) -> tuple[float, float] | None:
    """Equal error rate (%) and its threshold from cosine scores; ``None`` without both sets."""
    if not same or not different:
        return None
    candidates = sorted(set(same) | set(different))
    best: tuple[float, float, float] | None = None
    for t in candidates:
        frr = sum(1 for s in same if s < t) / len(same)
        far = sum(1 for d in different if d >= t) / len(different)
        gap = abs(far - frr)
        if best is None or gap < best[0]:
            best = (gap, (far + frr) / 2, t)
    if best is None:  # unreachable: candidates is non-empty
        return None
    return 100.0 * best[1], best[2]


def bench_speaker(
    corpus: Corpus,
    models: ModelSet,
    *,
    threads: int = 2,
    concurrent_stt: bool = True,
    max_clips: int | None = None,
) -> SpeakerSection:
    if models.speaker is None:
        return SpeakerSection(
            status="skipped", reason="speaker model not installed", threads=threads
        )
    clips = list(corpus.manifest.of_kind("command", "enroll", "negative"))
    if max_clips:
        clips = clips[:max_clips]
    if not clips:
        return SpeakerSection(
            status="skipped", reason="corpus has no command/enroll/negative clips"
        )
    audio = [_load_clip(corpus, c) for c in clips]
    embedder = SpeakerEmbedder(models.speaker, threads)
    notes: list[str] = []

    load = None
    if concurrent_stt and models.stt is not None:
        load = _SttLoad(models.stt, audio[: min(8, len(audio))], threads=2)
    elif concurrent_stt:
        notes.append("STT model missing: embed timings were NOT taken under STT load")
        concurrent_stt = False

    t1: list[float] = []
    t2: list[float] = []
    embeddings: list[tuple[Clip, Float32Array]] = []
    with load if load is not None else _Nothing():
        for clip, samples in zip(clips, audio, strict=True):
            for seconds, sink in ((1.0, t1), (2.0, t2)):
                window = last_seconds(samples, seconds)
                start = time.perf_counter()
                vec = embedder.embed(window)
                sink.append((time.perf_counter() - start) * 1000.0)
                if seconds == 2.0:
                    embeddings.append((clip, vec))

    same: list[float] = []
    different: list[float] = []
    for i, (ci, vi) in enumerate(embeddings):
        for cj, vj in embeddings[i + 1 :]:
            score = cosine(vi, vj)
            if ci.speaker is not None and ci.speaker == cj.speaker and ci.kind != "negative":
                same.append(score)
            else:
                different.append(score)
    eer = eer_pct(same, different)
    speakers = {c.speaker for c in clips if c.speaker is not None and c.kind != "negative"}
    if len(speakers) < 2:
        notes.append("fewer than 2 household speakers: EER is not meaningful")
    variant = SpeakerVariant(
        model_id=models.ids.get("speaker", "unknown"),
        precision="fp32",
        embedding_dim=embedder.dim,
        clip_1s_embed_ms=maybe_summarize(t1),
        clip_2s_embed_ms=maybe_summarize(t2),
        household_eer_pct=eer[0] if eer else None,
        eer_threshold=eer[1] if eer else None,
        n_speakers=len(speakers),
        n_clips=len(clips),
    )
    notes.append(
        "int8 CAM++ not available: sherpa-onnx publishes no int8 export (G1 fallback experiment)"
    )
    return SpeakerSection(
        status="measured",
        threads=threads,
        concurrent_stt=concurrent_stt,
        fp32=variant,
        int8=None,
        notes=notes,
    )


class _Nothing:
    def __enter__(self) -> _Nothing:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# ---- stt (G2) ----------------------------------------------------------------


def _speech_end(clip: Clip, samples: Float32Array, vad: Vad | None) -> float:
    """Clock zero for a clip: the label, else the last VAD-positive window, else the end."""
    if clip.speech_end_s is not None:
        return clip.speech_end_s
    if vad is not None:
        vad.reset()
        flags = vad.push(samples)
        for i in range(len(flags) - 1, -1, -1):
            if flags[i]:
                return (i + 1) * Vad.WINDOW / SAMPLE_RATE
    return duration_s(samples)


def _run_stt_clip(
    recognizer: SttRecognizer, samples: Float32Array, t_eos: float
) -> tuple[str, float, float]:
    """Stream a clip; returns ``(text, rtf, final_tail_ms)``.

    Audio up to ``t_eos`` is fed in 10 ms frames with a decode every 100 ms (the
    product's cadence); the final tail is flush (0.3 s silence) -> final result.
    """
    stream = recognizer.stream()
    end = min(len(samples), int(t_eos * SAMPLE_RATE))
    busy = 0.0
    for i, frame in enumerate(frames(samples[:end])):
        start = time.perf_counter()
        stream.feed(frame)
        if (i + 1) % DECODE_EVERY_FRAMES == 0:
            stream.decode()
        busy += time.perf_counter() - start
    start = time.perf_counter()
    stream.feed(silence(FLUSH_SILENCE_S))
    stream.finish()
    stream.decode()
    text = stream.text()
    tail = time.perf_counter() - start
    busy += tail
    audio_s = max(end / SAMPLE_RATE, FRAME_SAMPLES / SAMPLE_RATE)
    return text, busy / audio_s, tail * 1000.0


def bench_stt(
    corpus: Corpus,
    models: ModelSet,
    *,
    threads: Sequence[int] = (1, 2),
    max_clips: int | None = None,
    precision: str = "int8",
) -> SttSection:
    files = models.stt if precision == "int8" else models.stt_fp32
    if files is None:
        return SttSection(status="skipped", reason=f"stt model ({precision}) not installed")
    clips = corpus.manifest.commands()
    if max_clips:
        clips = clips[:max_clips]
    if not clips:
        return SttSection(status="skipped", reason="corpus has no command clips")
    audio = [_load_clip(corpus, c) for c in clips]
    vad = Vad(models.vad) if models.vad is not None else None
    ends = [_speech_end(c, a, vad) for c, a in zip(clips, audio, strict=True)]
    section = SttSection(
        status="measured",
        model_id=models.ids.get("stt", "unknown"),
        precision="int8" if precision == "int8" else "fp32",
    )
    notes: list[str] = []
    if files.bpe_vocab is None:
        notes.append(
            "hotword WER not measured: this model export has no bpe.model for hotword encoding"
        )
    for n in threads:
        recognizer = SttRecognizer(files, threads=n)
        rtfs: list[float] = []
        tails: list[float] = []
        pairs: list[tuple[str, str]] = []
        words = 0
        for clip, samples, t_eos in zip(clips, audio, ends, strict=True):
            text, rtf, tail = _run_stt_clip(recognizer, samples, t_eos)
            rtfs.append(rtf)
            tails.append(tail)
            pairs.append((clip.transcript or "", text))
            words += len((clip.transcript or "").split())
        result = SttThreads(
            threads=n,
            rtf=maybe_summarize(rtfs),
            final_tail_ms=maybe_summarize(tails),
            wer_pct=wer_pct(pairs),
            wer_hotwords_pct=None,
            n_clips=len(clips),
            n_words=words,
        )
        if n == 1:
            section = section.model_copy(update={"threads_1": result})
        elif n == 2:
            section = section.model_copy(update={"threads_2": result})
        else:
            notes.append(f"{n} threads measured but the schema only keeps 1 and 2")
    return section.model_copy(update={"notes": notes})


# ---- tts (G6 stage check) ----------------------------------------------------


def bench_tts(
    models: ModelSet,
    *,
    threads: int = 2,
    sentences: Sequence[str] = TTS_SENTENCES,
    default_voice: str = "en_US-lessac-low",
) -> TtsSection:
    if not models.tts_voices:
        return TtsSection(status="skipped", reason="no tts voice installed", threads=threads)
    if not phonemize.available():
        return TtsSection(status="skipped", reason=phonemize.MISSING_REASON, threads=threads)
    section = TtsSection(status="measured", threads=threads, default_voice=default_voice)
    for voice, (model, tokens) in models.tts_voices.items():
        synth = Synthesizer(model, tokens, threads=threads)
        synth.synth("Warm up.")  # first call pays one-off allocation; the product warms up too
        first: list[float] = []
        rtf: list[float] = []
        for sentence in sentences:
            marks: list[float] = []
            start = time.perf_counter()
            audio = synth.synth(sentence, on_chunk=lambda _n, t, m=marks: m.append(t))
            total = time.perf_counter() - start
            if marks:
                first.append((marks[0] - start) * 1000.0)
            seconds = len(audio) / synth.sample_rate
            if seconds > 0:
                rtf.append(total / seconds)
        section.voices[voice] = TtsVoice(
            voice=voice,
            sample_rate=synth.sample_rate,
            first_chunk_ms=maybe_summarize(first),
            rtf=maybe_summarize(rtf),
            n_sentences=len(sentences),
        )
    if default_voice not in section.voices:
        section.notes.append(
            f"default voice {default_voice} not installed; measured {sorted(section.voices)}"
        )
    return section


# ---- wake (G5) ---------------------------------------------------------------


def _wake_hits(detector: WakeDetector, samples: Float32Array) -> list[float]:
    """Times (s) at which the detector fired, honouring the refractory period."""
    detector.reset()
    hits: list[float] = []
    step_s = 0.010
    index = 0
    last_hit = -WAKE_REFRACTORY_S
    for frame in frames(samples, FRAME_SAMPLES * 10):
        for prob in detector.process(to_pcm16(frame)):
            t = index * step_s
            index += 1
            if prob > detector.probability_cutoff and t - last_hit >= WAKE_REFRACTORY_S:
                hits.append(t)
                last_hit = t
    return hits


def _distance_bucket(distance: float | None) -> str | None:
    if distance is None:
        return None
    return "1m" if distance < 2.0 else "3m"


def bench_wake(corpus: Corpus, models: ModelSet) -> WakeSection:
    detector = WakeDetector(models.wake_json)
    section = WakeSection(
        status="measured",
        model_id=detector.model_id,
        probability_cutoff=detector.probability_cutoff,
        sliding_window_size=detector.sliding_window_size,
        stock_model=detector.stock,
    )
    if not detector.stock:
        section.notes.append(
            "stock hey_beepboop model not installed: measured with the pymicro-wakeword builtin "
            f"'{detector.wake_word}'; never a G5 result"
        )
    cpu_busy = 0.0
    audio_total = 0.0

    soak = corpus.manifest.of_kind("soak", "negative")
    false_accepts = 0
    soak_seconds = 0.0
    for clip in soak:
        samples = _load_clip(corpus, clip)
        start = time.process_time()
        hits = _wake_hits(detector, samples)
        cpu_busy += time.process_time() - start
        false_accepts += len(hits)
        soak_seconds += duration_s(samples)
    audio_total += soak_seconds
    section.soak_hours = soak_seconds / 3600.0
    if soak:
        section.false_accepts = false_accepts
        section.false_accepts_per_24h = (
            false_accepts * 86400.0 / soak_seconds if soak_seconds else None
        )
    else:
        section.notes.append("no soak clips: false accepts not measured")

    positives = corpus.manifest.of_kind("wake")
    per_bucket: dict[str, list[bool]] = {"1m": [], "3m": []}
    per_speaker: dict[str, list[bool]] = {}
    latencies: list[float] = []
    for clip in positives:
        samples = _load_clip(corpus, clip)
        start = time.process_time()
        hits = _wake_hits(detector, samples)
        cpu_busy += time.process_time() - start
        audio_total += duration_s(samples)
        hit = bool(hits)
        bucket = _distance_bucket(clip.distance_m)
        if bucket is not None and clip.ambient == "quiet":
            per_bucket[bucket].append(hit)
        if clip.speaker is not None:
            per_speaker.setdefault(clip.speaker, []).append(hit)
        if hit and clip.phrase_end_s is not None:
            latencies.append((hits[0] - clip.phrase_end_s) * 1000.0)
    if not positives:
        section.notes.append("no wake clips: false rejects not measured")
    section.n_positive_1m = len(per_bucket["1m"])
    section.n_positive_3m = len(per_bucket["3m"])
    if per_bucket["1m"]:
        section.false_reject_pct_1m = 100.0 * per_bucket["1m"].count(False) / len(per_bucket["1m"])
    if per_bucket["3m"]:
        section.false_reject_pct_3m = 100.0 * per_bucket["3m"].count(False) / len(per_bucket["3m"])
    section.hit_latency_ms = maybe_summarize(latencies)
    for speaker, flags in sorted(per_speaker.items()):
        section.per_speaker[speaker] = WakePerSpeaker(
            n=len(flags),
            false_rejects=flags.count(False),
            false_reject_pct=100.0 * flags.count(False) / len(flags),
        )
    if audio_total > 0:
        section.cpu_pct_of_core = 100.0 * cpu_busy / audio_total
    if not soak and not positives:
        return WakeSection(
            status="skipped", reason="corpus has no wake or soak clips", model_id=detector.model_id
        )
    return section


# ---- endpoint (G6 chooses the hangover) ----------------------------------------


def vad_positive_windows(vad: Vad, samples: Float32Array) -> list[tuple[float, float]]:
    """``(start_s, end_s)`` of every VAD-positive 32 ms window."""
    vad.reset()
    flags = vad.push(np.concatenate([samples, silence(1.0)]))
    step = Vad.WINDOW / SAMPLE_RATE
    return [(i * step, (i + 1) * step) for i, on in enumerate(flags) if on]


def decide_end(
    windows: Sequence[tuple[float, float]], hangover_s: float, labelled_end_s: float
) -> tuple[float, bool]:
    """Where a fixed hangover would end the utterance; ``True`` if that truncates it."""
    if not windows:
        return hangover_s, True
    prev_end = windows[0][1]
    for start, end in windows[1:]:
        if start - prev_end > hangover_s and prev_end < labelled_end_s:
            return prev_end + hangover_s, True
        prev_end = end
    decided = windows[-1][1] + hangover_s
    return decided, decided < labelled_end_s


def bench_endpoint(
    corpus: Corpus, models: ModelSet, *, hangovers_ms: Sequence[int] = HANGOVERS_MS
) -> EndpointSection:
    if models.vad is None:
        return EndpointSection(status="skipped", reason="vad model not installed")
    clips = [c for c in corpus.manifest.commands() if c.speech_end_s is not None]
    if not clips:
        return EndpointSection(status="skipped", reason="no command clips with speech_end_s labels")
    vad = Vad(models.vad)
    windows = [vad_positive_windows(vad, _load_clip(corpus, c)) for c in clips]
    section = EndpointSection(status="measured", n_labelled=len(clips))
    best: tuple[float, int] | None = None
    for h in hangovers_ms:
        truncated = 0
        overruns: list[float] = []
        for clip, w in zip(clips, windows, strict=True):
            labelled = clip.speech_end_s if clip.speech_end_s is not None else 0.0
            decided, cut = decide_end(w, h / 1000.0, labelled)
            truncated += int(cut)
            overruns.append((decided - labelled) * 1000.0)
        pct = 100.0 * truncated / len(clips)
        section.hangovers[str(h)] = HangoverResult(
            hangover_ms=h, n=len(clips), truncation_pct=pct, overrun_ms=maybe_summarize(overruns)
        )
        # VOICE_PIPELINE §16: the smallest hangover with < 2 % truncation.
        if pct < 2.0 and (best is None or h < best[1]):
            best = (pct, h)
    section.chosen_hangover_ms = best[1] if best else None
    if best is None:
        section.notes.append("no hangover in the sweep reached < 2 % truncation")
    return section
