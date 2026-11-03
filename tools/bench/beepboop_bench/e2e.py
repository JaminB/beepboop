"""``bench e2e`` (G6): the full pipeline replayed concurrently, one waterfall per turn.

What runs: for every command clip, a worker thread feeds 10 ms frames into
Silero VAD and the streaming zipformer at real-time pace (``realtime=True``),
finds the last VAD-positive frame (**clock zero**), waits the VOICE_PIPELINE
§7.2 hangover (``endpoint``), flushes STT (``stt_tail``) while the speaker
embedding of the last 2 s runs on another thread (``speaker_final``), then

* ``fast_path`` / ``command_1s``: a grammar lookup stands in for the fast
  path and the reply is a pre-synthesised cached clip (ADR 0015), so
  ``first_speech`` is the moment that clip is ready to play;
* ``no_tool`` / ``tool``: with ``--llm-live`` the Bedrock provider is called
  with the probe's turn-shaped requests (``llm_ttft``, ``llm_first_sentence``,
  the ``ack`` earcon as ``first_audio`` on tool turns, the second call) and
  the first sentence is synthesised (``tts_first_chunk``, ``first_speech``).
  Without ``--llm-live`` those stages are ``None`` and the turn's
  ``first_speech`` is **not reported**; only the local stages are.

Not measured anywhere off the Pi: the output buffer and DAC (50 ms budget
row), the wake earcon by loopback, and the HA state storm (needs HA). Each is
listed in the section notes so a reader never mistakes the numbers for the
product's.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import cycle
from typing import Protocol, cast

from beepboop.log import get_logger
from beepboop.providers.llm.protocol import Event, Request, TextDelta, ToolCallEnd
from beepboop_bench import phonemize
from beepboop_bench.audio import (
    FRAME_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    Float32Array,
    frames,
    last_seconds,
    read_wav,
    silence,
)
from beepboop_bench.corpus import Clip, Corpus
from beepboop_bench.runtime import SpeakerEmbedder, SttRecognizer, Synthesizer, Vad
from beepboop_bench.schema import SCENARIOS, E2ESection, Scenario, ScenarioSummary, ToolSpan, Turn
from beepboop_bench.stages import DECODE_EVERY_FRAMES, FLUSH_SILENCE_S, ModelSet
from beepboop_bench.stats import maybe_summarize

log = get_logger(__name__)

_SENTENCE_END = re.compile(r"[.!?](\s|$)")
CACHED_DONE_TEXT = "Done."
CANNED_REPLY = "The kitchen lights are off now."


@dataclass(frozen=True)
class E2EOptions:
    concurrency: int = 2
    realtime: bool = True
    hangover_ms: int = 250
    threads: int = 2
    llm_live: bool = False
    ha_running: bool | None = None
    offline: bool | None = None
    max_turns: int | None = None
    duration_s: float | None = None


class _StreamProvider(Protocol):
    def stream(self, req: Request) -> AsyncIterator[Event]: ...


LlmFactory = Callable[[], _StreamProvider]


@dataclass
class _Worker:
    """Per-thread model instances (sherpa objects are not shared across threads)."""

    vad: Vad
    stt: SttRecognizer
    speaker: SpeakerEmbedder | None
    tts: Synthesizer | None
    cached_done: Float32Array | None
    llm: _StreamProvider | None


class _Pool:
    def __init__(self, models: ModelSet, opts: E2EOptions, llm_factory: LlmFactory | None) -> None:
        self._models = models
        self._opts = opts
        self._local = threading.local()
        self._llm_factory = llm_factory

    def get(self) -> _Worker:
        worker = cast("_Worker | None", getattr(self._local, "worker", None))
        if worker is None:
            worker = self._build()
            self._local.worker = worker
        return worker

    def _build(self) -> _Worker:
        m, t = self._models, self._opts.threads
        if m.vad is None or m.stt is None:
            msg = "e2e needs the vad and stt models"
            raise FileNotFoundError(msg)
        tts = None
        cached = None
        if m.tts_voices and phonemize.available():
            model, tokens = next(iter(m.tts_voices.values()))
            tts = Synthesizer(model, tokens, threads=t)
            cached = tts.synth(CACHED_DONE_TEXT)
        speaker = SpeakerEmbedder(m.speaker, t) if m.speaker is not None else None
        llm = self._llm_factory() if self._llm_factory is not None else None
        return _Worker(Vad(m.vad), SttRecognizer(m.stt, threads=t), speaker, tts, cached, llm)


# ---- one turn ---------------------------------------------------------------------


def _pace(start: float, frame_index: int, realtime: bool) -> None:
    if not realtime:
        return
    deadline = start + (frame_index + 1) * FRAME_MS / 1000.0
    delay = deadline - time.perf_counter()
    if delay > 0:
        time.sleep(delay)


def run_turn(
    worker: _Worker, clip: Clip, samples: Float32Array, turn_id: str, opts: E2EOptions, t0: float
) -> Turn:
    scenario: Scenario = clip.scenario or "no_tool"
    turn = Turn(
        turn_id=turn_id,
        clip_id=clip.id,
        scenario=scenario,
        speaker=clip.speaker,
        started_at_s=time.perf_counter() - t0,
        expected_transcript=clip.transcript,
    )
    stages: dict[str, float | None] = dict.fromkeys(
        (
            "wake",
            "speech",
            "endpoint",
            "stt_tail",
            "speaker_final",
            "fastpath",
            "ctx",
            "llm_ttft",
            "llm_first_sentence",
            "tts_first_chunk",
            "first_audio",
            "first_speech",
        )
    )
    marks: dict[str, float] = {}
    worker.vad.reset()
    stream = worker.stt.stream()
    hangover_s = opts.hangover_ms / 1000.0
    start = time.perf_counter()
    t_eos: float | None = None
    audio_last_positive: float | None = None
    audio_pos = 0.0
    endpoint_at: float | None = None
    all_frames: Iterator[Float32Array] = iter(
        [*frames(samples), *frames(silence(hangover_s + 0.5))]
    )
    for i, frame in enumerate(all_frames):
        _pace(start, i, opts.realtime)
        stream.feed(frame)
        if (i + 1) % DECODE_EVERY_FRAMES == 0:
            stream.decode()
        for on in worker.vad.push(frame):
            if on:
                audio_last_positive = audio_pos + FRAME_SAMPLES / SAMPLE_RATE
                t_eos = time.perf_counter()
        audio_pos += FRAME_SAMPLES / SAMPLE_RATE
        if audio_last_positive is not None and audio_pos - audio_last_positive >= hangover_s:
            endpoint_at = time.perf_counter()
            break
    if t_eos is None or endpoint_at is None:
        return turn.model_copy(
            update={"ok": False, "error": "VAD saw no speech in the clip", "stages_ms": stages}
        )
    stages["speech"] = (t_eos - start) * 1000.0
    stages["endpoint"] = (endpoint_at - t_eos) * 1000.0

    speaker_done: list[float] = []

    def embed() -> None:
        try:
            if worker.speaker is not None:
                worker.speaker.embed(last_seconds(samples, 2.0))
        finally:
            speaker_done.append(time.perf_counter())

    spk_thread = threading.Thread(target=embed, daemon=True) if worker.speaker is not None else None
    if spk_thread is not None:
        spk_thread.start()
    stream.feed(silence(FLUSH_SILENCE_S))
    stream.finish()
    stream.decode()
    text = stream.text()
    t_final = time.perf_counter()
    stages["stt_tail"] = (t_final - t_eos) * 1000.0
    if spk_thread is not None:
        spk_thread.join()
        stages["speaker_final"] = (speaker_done[0] - t_eos) * 1000.0
    turn = turn.model_copy(update={"transcript": text})

    # Fast path: the grammar does not exist yet (v0.1); a dict lookup stands in for
    # "match + ACL resolve" and its cost is what is measured, nothing more.
    t_fp = time.perf_counter()
    matched = scenario in ("fast_path", "command_1s")
    stages["fastpath"] = (time.perf_counter() - t_fp) * 1000.0

    if matched:
        if worker.cached_done is None:
            turn = turn.model_copy(
                update={"ok": False, "error": "no cached reply clip (tts/phonemizer missing)"}
            )
        else:
            t_ready = time.perf_counter()
            # Cached clip: no synthesis, so tts_first_chunk is not a stage here.
            stages["first_audio"] = stages["first_speech"] = (t_ready - t_eos) * 1000.0
    elif worker.llm is not None:
        _llm_turn(worker, scenario, turn_id, t_eos, t_final, stages, marks, turn)
    else:
        if worker.tts is not None:
            m: list[float] = []
            t_tts = time.perf_counter()
            worker.tts.synth(CANNED_REPLY, on_chunk=lambda _n, t, mm=m: mm.append(t))
            if m:
                stages["tts_first_chunk"] = (m[0] - t_tts) * 1000.0
        turn = turn.model_copy(update={"error": "llm not live: first_speech not reported"})
    return turn.model_copy(update={"stages_ms": stages, "marks_ms": marks, "tools": turn.tools})


def _llm_turn(
    worker: _Worker,
    scenario: Scenario,
    turn_id: str,
    t_eos: float,
    t_final: float,
    stages: dict[str, float | None],
    marks: dict[str, float],
    turn: Turn,
) -> None:
    """Bedrock-backed no-tool / tool turn using the probe's turn-shaped requests."""
    from beepboop.providers.llm.bedrock import probe

    provider = worker.llm
    if provider is None:
        return
    now = datetime.now(UTC)
    first = probe.first_request(now, 120, turn_id)
    if scenario == "no_tool":
        first = first.model_copy(update={"tools": []})
    stages["ctx"] = 0.0  # prompt assembly is the probe's static prefix here

    async def run(req: Request) -> tuple[list[Event], float | None, float | None, str]:
        events: list[Event] = []
        t_sent = time.perf_counter()
        ttft: float | None = None
        first_sentence: float | None = None
        text = ""
        async for ev in provider.stream(req):
            events.append(ev)
            if isinstance(ev, TextDelta | ToolCallEnd) and ttft is None:
                ttft = time.perf_counter() - t_sent
            if isinstance(ev, TextDelta):
                text += ev.text
                if first_sentence is None and _SENTENCE_END.search(text):
                    first_sentence = time.perf_counter() - t_sent
        return events, ttft, first_sentence, text

    t_req = time.perf_counter()
    events, ttft, first_sentence, text = asyncio.run(run(first))
    tool_end = next((e for e in events if isinstance(e, ToolCallEnd)), None)
    if ttft is not None:
        stages["llm_ttft"] = ttft * 1000.0
    if scenario == "tool" and tool_end is not None:
        t_ack = time.perf_counter()
        stages["first_audio"] = (t_ack - t_eos) * 1000.0  # the cached `ack` clip is ready now
        turn.tools.append(
            ToolSpan(
                name="home.turn_off",
                ms=0.0,
                risk="act",
                decision="allow",
                factor=None,
            )
        )
        parts, tool_id = probe.assistant_parts(events, 0)
        second = probe.second_request(first, parts, tool_id)
        events, _, first_sentence, text = asyncio.run(run(second))
        marks["llm_second_sent"] = (time.perf_counter() - t_eos) * 1000.0
    if first_sentence is not None:
        stages["llm_first_sentence"] = first_sentence * 1000.0
    sentence = _SENTENCE_END.split(text)[0].strip() if text else ""
    if worker.tts is not None and sentence:
        m: list[float] = []
        t_tts = time.perf_counter()
        worker.tts.synth(sentence + ".", on_chunk=lambda _n, t, mm=m: mm.append(t))
        if m:
            stages["tts_first_chunk"] = (m[0] - t_tts) * 1000.0
            stages["first_speech"] = (m[0] - t_eos) * 1000.0
            if stages["first_audio"] is None:
                stages["first_audio"] = stages["first_speech"]
    marks["llm_request_sent"] = (t_req - t_eos) * 1000.0
    marks["stt_final"] = (t_final - t_eos) * 1000.0


# ---- the run ---------------------------------------------------------------------


def summarize_turns(turns: list[Turn]) -> dict[Scenario, ScenarioSummary]:
    out: dict[Scenario, ScenarioSummary] = {}
    for scenario in SCENARIOS:
        mine = [t for t in turns if t.scenario == scenario and t.ok]
        if not mine:
            continue
        stages: dict[str, list[float]] = {}
        for t in mine:
            for name, value in t.stages_ms.items():
                if value is not None:
                    stages.setdefault(name, []).append(value)
        out[scenario] = ScenarioSummary(
            n=len(mine),
            first_audio_ms=maybe_summarize(stages.get("first_audio", [])),
            first_speech_ms=maybe_summarize(stages.get("first_speech", [])),
            stages={
                k: s for k, v in sorted(stages.items()) if (s := maybe_summarize(v)) is not None
            },
        )
    return out


def bench_e2e(
    corpus: Corpus, models: ModelSet, opts: E2EOptions, *, llm_factory: LlmFactory | None = None
) -> E2ESection:
    if models.vad is None or models.stt is None:
        return E2ESection(status="skipped", reason="vad and stt models are required for e2e")
    clips = corpus.manifest.commands()
    if not clips:
        return E2ESection(status="skipped", reason="corpus has no command clips")
    if opts.llm_live and llm_factory is None:
        return E2ESection(status="skipped", reason="--llm-live but no LLM provider could be built")
    audio = {
        c.id: read_wav(corpus.clip_path(c), channel=corpus.manifest.capture_channel) for c in clips
    }
    pool = _Pool(models, opts, llm_factory if opts.llm_live else None)
    t0 = time.perf_counter()
    planned: list[Clip] = []
    if opts.duration_s is not None:
        est = sum(len(a) for a in audio.values()) / SAMPLE_RATE / len(audio) + 1.0
        n = int(opts.duration_s / est * opts.concurrency) + opts.concurrency
        planned = [c for c, _ in zip(cycle(clips), range(n), strict=False)]
    else:
        planned = (
            list(clips)
            if opts.max_turns is None
            else [c for c, _ in zip(cycle(clips), range(opts.max_turns), strict=False)]
        )

    def job(index: int, clip: Clip) -> Turn:
        if opts.duration_s is not None and time.perf_counter() - t0 > opts.duration_s:
            return Turn(
                turn_id=f"t{index:04d}",
                clip_id=clip.id,
                scenario=clip.scenario or "no_tool",
                started_at_s=time.perf_counter() - t0,
                ok=False,
                error="run duration reached",
            )
        try:
            return run_turn(pool.get(), clip, audio[clip.id], f"t{index:04d}", opts, t0)
        except Exception as exc:  # a failed turn is recorded, never hidden
            log.exception("bench.e2e.turn_failed", turn_id=f"t{index:04d}")
            return Turn(
                turn_id=f"t{index:04d}",
                clip_id=clip.id,
                scenario=clip.scenario or "no_tool",
                started_at_s=time.perf_counter() - t0,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    with ThreadPoolExecutor(max_workers=opts.concurrency, thread_name_prefix="e2e") as pool_exec:
        turns = list(pool_exec.map(job, range(len(planned)), planned))
    turns = [t for t in turns if t.error != "run duration reached"]
    summary = summarize_turns(turns)
    notes = [
        "not measured off the Pi: output buffer/DAC, wake earcon loopback, HA state storm",
        "fast path: grammar lookup stub; reply is the pre-synthesised cached 'Done.' clip",
    ]
    if not opts.llm_live:
        notes.append("LLM not live: no_tool/tool first_speech not reported (local stages only)")
    section = E2ESection(
        status="measured",
        concurrency=opts.concurrency,
        realtime=opts.realtime,
        ha_running=opts.ha_running,
        llm_live=opts.llm_live,
        offline=opts.offline,
        turns=turns,
        notes=notes,
    )
    by_name: dict[str, ScenarioSummary] = {str(k): v for k, v in summary.items()}
    return section.model_copy(update=by_name)
