"""The audio-side turn state machine (VOICE_PIPELINE §4, §5, §7-§15).

:class:`Pipeline` consumes capture frames (already through the front end: ring
buffer, VAD flag, wake hit) and core events, drives the providers, and emits
audio events through ``send``. Every timer is driven by frame stamps, so the
machine is deterministic under test. Blocking inference runs on executors,
never on the event loop.

States: ``IDLE`` (the only state with wake detection armed), ``LISTENING``,
``THINKING``, ``SPEAKING``, ``REPLY_WINDOW``, ``PIN_CAPTURE``, ``MUTED``.

Ordering guarantees (§5): ``wake`` first; ``speaker.provisional`` before
``speaker.final``; ``speaker.final`` always, and always before ``stt.final``;
``stt.partial`` at most one per 200 ms; ``pin.result`` never carries digits.

Audio lifetime (ADR 0018): the pre-roll, the command frames and the enrollment
clip live in RAM for one turn (or one clip) and are zeroed when it ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

import numpy as np
import structlog

from beepboop_audio.config import AudioConfig
from beepboop_audio.frames import (
    FRAME_NS,
    PREROLL_MS,
    Frame,
    concat,
    ms_to_samples,
    samples_to_ms,
    wipe,
)
from beepboop_audio.pipeline.earcons import Earcons
from beepboop_audio.pipeline.endpoint import Endpointer, EndpointEvent
from beepboop_audio.pipeline.enroll import EnrollmentSession, gate, measure
from beepboop_audio.pipeline.frontend import FrontEnd
from beepboop_audio.pipeline.leds import LedDriver, LedState
from beepboop_audio.pipeline.mixer import Mixer, SpeechItem
from beepboop_audio.pipeline.pin import PinOutcome, outcome_for_text
from beepboop_audio.speaker.identifier import (
    GUEST,
    MIN_CMD_MS,
    SpeakerIdentifier,
    Thresholds,
    Voiceprint,
)
from beepboop_audio.tts.phrases import SYSTEM_PHRASES, ClipCache, spoken_text
from beepboop_audio.types import (
    PCM16,
    Final,
    Identity,
    IdentityStage,
    Partial,
    SpeakerProvider,
    SttContext,
    SttProvider,
    TtsProvider,
    VoiceRef,
)
from beepboop_protocol import (
    AudioEvent,
    CancelEvent,
    CapturePinEvent,
    CoreEvent,
    CoreHelloEvent,
    EarconEvent,
    EnrollClipData,
    EnrollClipEvent,
    EnrollQuality,
    EnrollStartEvent,
    EnrollStopEvent,
    MicStateData,
    MicStateEvent,
    MuteEvent,
    PinResultData,
    PinResultEvent,
    ProfilesSyncEvent,
    SpeakData,
    SpeakDoneData,
    SpeakDoneEvent,
    SpeakerCandidate,
    SpeakerFinalData,
    SpeakerFinalEvent,
    SpeakerProvisionalData,
    SpeakerProvisionalEvent,
    SpeakEvent,
    SttFinalData,
    SttFinalEvent,
    SttPartialData,
    SttPartialEvent,
    WakeData,
    WakeEvent,
    new_ulid,
)

log = structlog.get_logger(__name__)

type Send = Callable[[AudioEvent], Awaitable[None]]

CMD_EMBED_MS = 2000
PROVISIONAL_WAIT_S = 0.15
PARTIAL_INTERVAL_NS = 200_000_000
THINKING_TIMEOUT_NS = 10_000_000_000
REPLY_SEED_MS = 300
HOTWORD_CAP = 200
HOTWORD_MAX_TOKENS = 4
ENROLL_LISTEN_MS = 60_000
NOISE_MS = 500


class State(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    REPLY_WINDOW = "REPLY_WINDOW"
    PIN_CAPTURE = "PIN_CAPTURE"
    MUTED = "MUTED"


@dataclass
class ProfileInfo:
    hotwords: tuple[str, ...]
    voice_id: str
    rate: float
    locale: str


@dataclass(eq=False)
class Turn:
    id: str
    trigger: Literal["wake", "reply"]
    t_wake: int
    endpointer: Endpointer
    locale: str
    preroll: PCM16
    stt_queue: asyncio.Queue[PCM16 | None] = field(default_factory=asyncio.Queue[PCM16 | None])
    frames: list[Frame] = field(default_factory=list[Frame])
    provisional_task: asyncio.Task[Identity] | None = None
    stt_task: asyncio.Task[Final | None] | None = None
    final_task: asyncio.Task[Identity | None] | None = None
    final_gen: int = -1
    final_locked: bool = False
    gen: int = 0
    last_partial_ns: int = 0
    finishing: bool = False
    final_identity: Identity | None = None
    profile_id: str = GUEST


@dataclass(eq=False)
class PinCapture:
    turn_id: str
    approval_id: str
    pin_hash: str
    min_len: int
    max_len: int
    deadline_ns: int
    listening: bool = False
    endpointer: Endpointer | None = None
    queue: asyncio.Queue[PCM16 | None] = field(default_factory=asyncio.Queue[PCM16 | None])
    task: asyncio.Task[Final | None] | None = None
    finishing: bool = False

    def wipe(self) -> None:
        self.pin_hash = ""


@dataclass(eq=False)
class EnrollCapture:
    session: EnrollmentSession
    endpointer: Endpointer
    frames: list[Frame] = field(default_factory=list[Frame])


@dataclass(eq=False)
class Utterance:
    turn_id: str | None
    utterance_id: str
    listen_after: bool = False
    closed: bool = False
    is_last: bool = False
    pending: int = 0
    samples: int = 0
    t_first: int | None = None
    t_last: int = 0
    reason: Literal["finished", "cancelled", "error"] = "finished"
    reported: bool = False


async def _aclose(stream: object) -> None:
    if isinstance(stream, AsyncGenerator):
        await stream.aclose()


async def _drain(queue: asyncio.Queue[PCM16 | None]) -> AsyncIterator[PCM16]:
    while True:
        item = await queue.get()
        if item is None:
            return
        yield item


def _guest(stage: IdentityStage, anchor: bool, embedder: tuple[str, str]) -> Identity:
    return Identity(GUEST, 0.0, 0.0, stage, anchor, (), embedder)


def cap_hotwords(*lists: tuple[str, ...]) -> tuple[str, ...]:
    """Lower-cased, deduplicated, ≤ 4 tokens per phrase, ≤ 200 entries (VOICE_PIPELINE §10.2)."""
    out: dict[str, None] = {}
    for phrases in lists:
        for phrase in phrases:
            norm = " ".join(phrase.lower().split())
            if norm and len(norm.split()) <= HOTWORD_MAX_TOKENS:
                out.setdefault(norm, None)
            if len(out) >= HOTWORD_CAP:
                return tuple(out)
    return tuple(out)


class Pipeline:
    def __init__(
        self,
        *,
        config: AudioConfig,
        frontend: FrontEnd,
        identifier: SpeakerIdentifier,
        speaker: SpeakerProvider | None,
        stt: SttProvider,
        tts: TtsProvider,
        mixer: Mixer,
        earcons: Earcons,
        clips: ClipCache,
        leds: LedDriver,
        send: Send,
        wake_model: str = "hey_beepboop",
        executor: Executor | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.config = config
        self.frontend = frontend
        self.identifier = identifier
        self.speaker = speaker
        self.stt = stt
        self.tts = tts
        self.mixer = mixer
        self.earcons = earcons
        self.clips = clips
        self.leds = leds
        self._send = send
        self.wake_model = wake_model
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="bb-voice"
        )
        self._clock = clock
        self.state = State.IDLE
        self.turn: Turn | None = None
        self.pin: PinCapture | None = None
        self.enroll: EnrollCapture | None = None
        self.profiles: dict[str, ProfileInfo] = {}
        self.household_hotwords: tuple[str, ...] = ()
        self.hw_muted = False
        self.sw_muted = False
        self._unmute_handle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._speak_queue: asyncio.Queue[tuple[str | None, SpeakData]] = asyncio.Queue()
        self._speak_worker: asyncio.Task[None] | None = None
        self._utterances: dict[str, Utterance] = {}
        self._open_utterance: dict[str | None, str] = {}
        self._seq_next: dict[str | None, int] = {}
        self._seq_hold: dict[str | None, dict[int, SpeakData]] = {}
        self._cancelled_turns: set[str] = set()
        self._t_now = 0
        self._t_thinking = 0
        self._reply: Endpointer | None = None
        self._reply_frames: list[Frame] = []
        self._last_profile: str = GUEST
        self._loop: asyncio.AbstractEventLoop | None = None
        self.frontend.arm(True)
        self.leds.show(LedState.IDLE)

    # ------------------------------------------------------------- utilities --

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = self.loop.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("pipeline.task_failed", task=task.get_name(), error=repr(exc))

    async def send(self, event: AudioEvent) -> None:
        try:
            await self._send(event)
        except Exception as exc:
            log.warning("pipeline.send_failed", type=event.type, error=exc.__class__.__name__)

    async def settle(self, rounds: int = 50) -> None:
        """Wait (bounded) until no background task is pending (tests and shutdown).

        Tasks waiting on playback (a PIN prompt) only finish when the mixer is pulled,
        so each round waits at most 50 ms.
        """
        for _ in range(rounds):
            pending = [t for t in self._tasks if not t.done()]
            if not pending and self._speak_queue.empty():
                await asyncio.sleep(0)
                if not [t for t in self._tasks if not t.done()]:
                    return
            if pending:
                await asyncio.wait(pending, timeout=0.05)
            else:
                await asyncio.sleep(0.001)

    async def close(self) -> None:
        if self._speak_worker is not None:
            self._speak_worker.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._speak_worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._speak_worker

    def _set_state(self, state: State, led: LedState | None = None) -> None:
        if state != self.state:
            log.debug(
                "pipeline.state", state=str(state), turn_id=self.turn.id if self.turn else None
            )
        self.state = state
        if led is not None:
            self.leds.show(led)
        self.frontend.arm(
            state == State.IDLE
            and not self.muted
            and (self.enroll is None or self.enroll.session.mode == "wake")
        )

    @property
    def muted(self) -> bool:
        return self.hw_muted or self.sw_muted

    def _embedder(self) -> tuple[str, str]:
        if self.speaker is None:
            return ("none", "none")
        return (self.speaker.model_id, self.speaker.model_sha)

    async def _embed_identify(
        self, audio: PCM16, *, stage: IdentityStage, anchor: bool
    ) -> Identity | None:
        """Embed and identify off the loop; None when nothing can be said (no prints, too short)."""
        if self.speaker is None or not self.identifier.profiles or not len(audio):
            return None
        speaker = self.speaker
        try:
            emb = await self.loop.run_in_executor(self._executor, speaker.embed, audio)
        except ValueError:
            return None
        return self.identifier.identify(emb, stage=stage, anchor=anchor)

    def _voice_for(self, profile_id: str, data_voice: str | None = None) -> str:
        default = self.config.providers.tts.default_voice
        candidate = data_voice or (
            self.profiles[profile_id].voice_id if profile_id in self.profiles else default
        )
        voices = {v.voice_id for v in self.tts.voices()}
        return candidate if candidate in voices or not voices else default

    # ---------------------------------------------------------------- frames --

    async def on_frame(self, frame: Frame) -> None:
        self._t_now = frame.t_ns
        if self.muted:
            return
        if self.enroll is not None and self.state == State.IDLE:
            await self._enroll_frame(frame)
            return
        match self.state:
            case State.IDLE:
                if frame.wake is not None:
                    await self._start_turn(
                        "wake", frame, score=frame.wake.prob, model=frame.wake.model
                    )
            case State.LISTENING:
                await self._listen_frame(frame)
            case State.THINKING:
                if frame.t_ns - self._t_thinking >= THINKING_TIMEOUT_NS and self.turn is not None:
                    log.info("pipeline.no_speak", turn_id=self.turn.id)
                    self.mixer.play_earcon(self.earcons.get("error"))
                    self.leds.show(LedState.ERROR)
                    self._end_turn()
            case State.REPLY_WINDOW:
                await self._reply_frame(frame)
            case State.PIN_CAPTURE:
                await self._pin_frame(frame)
            case State.SPEAKING | State.MUTED:
                pass

    # ----------------------------------------------------------------- turns --

    async def _start_turn(
        self,
        trigger: Literal["wake", "reply"],
        frame: Frame,
        *,
        score: float,
        model: str,
        seed: list[Frame] | None = None,
    ) -> None:
        locale = self.config.locale
        t_wake = frame.wake.t_wake_ns if frame.wake is not None else frame.t_ns
        preroll = (
            self.frontend.preroll(ms_to_samples(PREROLL_MS))
            if trigger == "wake"
            else np.zeros(0, np.int16)
        )
        turn = Turn(
            id=new_ulid(),
            trigger=trigger,
            t_wake=t_wake,
            endpointer=Endpointer(self.config.audio.endpoint, t_wake, locale=locale),
            locale=locale,
            preroll=preroll,
        )
        self.turn = turn
        self._set_state(State.LISTENING, LedState.LISTENING)
        await self.send(
            WakeEvent(
                turn_id=turn.id,
                data=WakeData(
                    device=self.config.device.id,
                    model=model,
                    score=min(1.0, max(0.0, score)),
                    trigger=trigger,
                    preroll_ms=samples_to_ms(len(preroll)),
                    t_wake=t_wake,
                ),
            )
        )
        self.mixer.play_earcon(self.earcons.get("wake" if trigger == "wake" else "prompt"))
        if trigger == "wake":
            turn.provisional_task = self._spawn(self._provisional(turn), f"provisional-{turn.id}")
        turn.stt_task = self._spawn(self._run_stt(turn), f"stt-{turn.id}")
        for seeded in seed or []:
            await self._listen_frame(seeded)

    async def _provisional(self, turn: Turn) -> Identity:
        ident = await self._embed_identify(turn.preroll, stage="provisional", anchor=True)
        if ident is None:
            ident = _guest("provisional", True, self._embedder())
        turn.profile_id = ident.profile_id
        await self.send(
            SpeakerProvisionalEvent(
                turn_id=turn.id,
                data=SpeakerProvisionalData(
                    profile_id=ident.profile_id,
                    score=ident.score,
                    margin=ident.margin,
                    audio_ms=samples_to_ms(len(turn.preroll)),
                ),
            )
        )
        return ident

    def _hotwords_for(self, profile_id: str) -> tuple[str, ...]:
        personal = self.profiles[profile_id].hotwords if profile_id in self.profiles else ()
        return cap_hotwords(self.household_hotwords, personal)

    async def _run_stt(self, turn: Turn) -> Final | None:
        if turn.provisional_task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(turn.provisional_task), PROVISIONAL_WAIT_S)
        profile = turn.profile_id if turn.trigger == "wake" else self._last_profile
        ctx = SttContext(
            locale=turn.locale,
            hotwords=self._hotwords_for(profile),
            hotwords_score=self.config.providers.stt.hotwords_score,
            mode="command",
        )
        final: Final | None = None
        stream = self.stt.stream(_drain(turn.stt_queue), ctx)
        try:
            async for item in stream:
                if isinstance(item, Partial):
                    turn.endpointer.set_partial(item.text)
                    now = self._clock()
                    if now - turn.last_partial_ns >= PARTIAL_INTERVAL_NS and not turn.finishing:
                        turn.last_partial_ns = now
                        await self.send(
                            SttPartialEvent(
                                turn_id=turn.id,
                                data=SttPartialData(text=item.text, stable=item.stable),
                            )
                        )
                else:
                    final = item
                    break
        finally:
            await _aclose(stream)
        if not turn.finishing and self.turn is turn and final is not None:
            # The STT endpoint backstop fired before the VAD did (§7.2).
            self._request_finish(turn, "silence", t_eos=self._t_now)
        return final

    async def _listen_frame(self, frame: Frame) -> None:
        turn = self.turn
        if turn is None or turn.finishing:
            return
        turn.stt_queue.put_nowait(frame.pcm)
        if len(turn.frames) < ms_to_samples(15_000 + 7_000) // 160:
            turn.frames.append(frame)
        for ev in turn.endpointer.update(frame.speech, frame.t_ns):
            match ev:
                case EndpointEvent.SPEECH_START:
                    pass
                case EndpointEvent.SPECULATE:
                    if not turn.final_locked:
                        self._start_final(turn)
                case EndpointEvent.RESUMED:
                    if not turn.final_locked:
                        turn.gen += 1
                case EndpointEvent.EOS:
                    self._request_finish(
                        turn, "silence", t_eos=turn.endpointer.t_eos_ns or frame.t_ns
                    )
                    return
                case EndpointEvent.MAX_LEN:
                    self.mixer.play_earcon(self.earcons.get("too_long"))
                    self.leds.show(LedState.TOO_LONG)
                    self._request_finish(turn, "max_len", t_eos=frame.t_ns)
                    return
                case EndpointEvent.TIMEOUT:
                    if not turn.finishing:
                        turn.finishing = True
                        self._spawn(self._finish_empty(turn), f"empty-{turn.id}")
                    return
        if (
            turn.endpointer.speech_started
            and not turn.final_locked
            and samples_to_ms(len(self._cmd_audio(turn))) >= CMD_EMBED_MS
        ):
            turn.final_locked = True
            if turn.final_gen != turn.gen or turn.final_task is None:
                self._start_final(turn)

    def _cmd_audio(self, turn: Turn) -> PCM16:
        start = turn.endpointer.t_speech_start_ns
        if start is None:
            return np.zeros(0, dtype=np.int16)
        end = turn.endpointer.last_speech_ns or start
        pcm = concat([f.pcm for f in turn.frames if start <= f.t_ns <= end])
        return pcm[: ms_to_samples(CMD_EMBED_MS)]

    def _start_final(self, turn: Turn) -> None:
        turn.final_gen = turn.gen
        audio = self._cmd_audio(turn)
        stage_anchor = turn.trigger == "wake"
        if samples_to_ms(len(audio)) < MIN_CMD_MS:
            turn.final_task = None
            return
        turn.final_task = self._spawn(
            self._embed_identify(audio, stage="final", anchor=stage_anchor), f"final-{turn.id}"
        )

    async def _final_identity(self, turn: Turn) -> Identity:
        provisional: Identity | None = None
        if turn.provisional_task is not None:
            provisional = await turn.provisional_task
        cmd: Identity | None = None
        if turn.final_task is not None and turn.final_gen == turn.gen:
            cmd = await turn.final_task
        else:
            audio = self._cmd_audio(turn)
            if samples_to_ms(len(audio)) >= MIN_CMD_MS:
                cmd = await self._embed_identify(
                    audio, stage="final", anchor=turn.trigger == "wake"
                )
        embedder = self._embedder()
        if turn.trigger == "reply":
            return cmd if cmd is not None else _guest("final", False, embedder)
        if provisional is None or not self.identifier.profiles:
            return _guest("final", True, embedder)
        if cmd is None:
            return self.identifier.as_final(provisional)
        return self.identifier.fuse(provisional, cmd)

    async def _send_speaker_final(self, turn: Turn) -> Identity:
        ident = await self._final_identity(turn)
        turn.final_identity = ident
        cmd_ms = min(CMD_EMBED_MS, samples_to_ms(len(self._cmd_audio(turn))))
        await self.send(
            SpeakerFinalEvent(
                turn_id=turn.id,
                data=SpeakerFinalData(
                    profile_id=ident.profile_id,
                    score=ident.score,
                    margin=ident.margin,
                    anchor=turn.trigger == "wake",
                    audio_ms=samples_to_ms(len(turn.preroll)) + cmd_ms,
                    candidates=[
                        SpeakerCandidate(profile_id=p, score=s) for p, s in ident.candidates
                    ],
                ),
            )
        )
        if ident.profile_id != GUEST:
            self._last_profile = ident.profile_id
        return ident

    def _request_finish(
        self, turn: Turn, endpoint: Literal["silence", "max_len", "cancel"], *, t_eos: int
    ) -> asyncio.Task[None] | None:
        """End of speech: stop feeding STT now; the finals are sent from a task."""
        if turn.finishing:
            return None
        turn.finishing = True
        if self.turn is turn and endpoint != "cancel":
            self._t_thinking = self._t_now
            self._set_state(State.THINKING, LedState.THINKING)
        turn.stt_queue.put_nowait(None)
        return self._spawn(self._complete(turn, endpoint, t_eos), f"finish-{turn.id}")

    async def _complete(
        self, turn: Turn, endpoint: Literal["silence", "max_len", "cancel"], t_eos: int
    ) -> None:
        await self._send_speaker_final(turn)
        final: Final | None = None
        if turn.stt_task is not None:
            try:
                final = await turn.stt_task
            except Exception as exc:
                log.error("stt.failed", turn_id=turn.id, error=repr(exc))
        text = final.text.strip() if final is not None else ""
        t_speech_start = turn.endpointer.t_speech_start_ns or t_eos
        t_final = max(t_eos, self._clock(), final.t_final_ns if final else 0)
        await self.send(
            SttFinalEvent(
                turn_id=turn.id,
                data=SttFinalData(
                    text=text,
                    locale=turn.locale,
                    speech_ms=turn.endpointer.speech_ms,
                    endpoint=endpoint,
                    empty=not text,
                    provider=self.stt.id,
                    t_speech_start=min(t_speech_start, t_eos),
                    t_eos=t_eos,
                    t_final=t_final,
                ),
            )
        )
        self._wipe_turn(turn)
        if not text and self.turn is turn and endpoint != "cancel":
            self.mixer.play_earcon(self.earcons.get("cancel"))
            self._end_turn()

    async def _finish_empty(self, turn: Turn) -> None:
        """No speech within the speech-start timeout: a false wake (or an empty reply window)."""
        turn.stt_queue.put_nowait(None)
        if turn.stt_task is not None:
            turn.stt_task.cancel()
        await self._send_speaker_final(turn)
        await self.send(
            SttFinalEvent(
                turn_id=turn.id,
                data=SttFinalData(
                    text="",
                    locale=turn.locale,
                    speech_ms=0,
                    endpoint="silence",
                    empty=True,
                    provider=self.stt.id,
                    t_speech_start=turn.t_wake,
                    t_eos=self._t_now,
                    t_final=max(self._t_now, self._clock()),
                ),
            )
        )
        self._wipe_turn(turn)
        self.mixer.play_earcon(self.earcons.get("cancel"))
        self.leds.show(LedState.CANCEL)
        if self.turn is turn:
            self._end_turn()

    def _wipe_turn(self, turn: Turn) -> None:
        wipe(turn.preroll)
        for f in turn.frames:
            wipe(f.pcm)
        turn.frames.clear()

    def _end_turn(self) -> None:
        if self.turn is not None:
            self._wipe_turn(self.turn)
        self.turn = None
        self._reply = None
        self._reply_frames.clear()
        self.frontend.clear_ring()
        self._set_state(State.IDLE, LedState.IDLE)

    # ---------------------------------------------------------- reply window --

    def _open_reply_window(self) -> None:
        self._set_state(State.REPLY_WINDOW, LedState.REPLY_WINDOW)
        self.turn = None
        self.mixer.play_earcon(self.earcons.get("prompt"))
        self._reply = Endpointer(
            self.config.audio.endpoint,
            self._t_now,
            locale=self.config.locale,
            speech_start_timeout_ms=self.config.audio.reply_window_ms,
        )
        self._reply_frames = []

    async def _reply_frame(self, frame: Frame) -> None:
        window = self._reply
        if window is None:
            self._end_turn()
            return
        self._reply_frames.append(frame)
        horizon = (
            frame.t_ns - (self.config.audio.endpoint.min_speech_ms + REPLY_SEED_MS) * 1_000_000
        )
        while self._reply_frames and self._reply_frames[0].t_ns < horizon - FRAME_NS:
            wipe(self._reply_frames.pop(0).pcm)
        for ev in window.update(frame.speech, frame.t_ns):
            if ev == EndpointEvent.TIMEOUT:
                self._end_turn()
                return
            if ev == EndpointEvent.SPEECH_START:
                seed = list(self._reply_frames)
                self._reply = None
                self._reply_frames = []
                await self._start_turn(
                    "reply", seed[0], score=0.0, model=self.wake_model, seed=seed
                )
                return

    # ----------------------------------------------------------- core events --

    async def handle(self, event: CoreEvent) -> None:
        match event:
            case SpeakEvent():
                self._queue_speak(event.turn_id, event.data)
            case CancelEvent():
                await self._cancel(event.turn_id)
            case CapturePinEvent():
                self._spawn(self._capture_pin(event), f"pin-{event.turn_id}")
            case EarconEvent():
                self.mixer.play_earcon(self.earcons.get(event.data.name))
                if event.data.name == "ack":
                    self.leds.show(LedState.TOOL)
            case MuteEvent():
                await self.set_software_mute(event.data.on, event.data.until)
            case ProfilesSyncEvent():
                self.sync_profiles(event)
            case EnrollStartEvent():
                self._start_enroll(event)
            case EnrollStopEvent():
                if (
                    self.enroll is not None
                    and self.enroll.session.session_id == event.data.session_id
                ):
                    self._stop_enroll()
            case CoreHelloEvent():
                pass

    # ------------------------------------------------------------- profiles --

    def sync_profiles(self, event: ProfilesSyncEvent) -> None:
        """Replace voiceprints, vocabulary and voices (RAM only, VOICE_PIPELINE §9.4).

        The ``guest`` row (no centroids) carries the household-wide vocabulary.
        """
        if self.speaker is not None and event.data.model_id != self.speaker.model_id:
            log.warning(
                "profiles.model_mismatch", synced=event.data.model_id, active=self.speaker.model_id
            )
            prints: list[Voiceprint] = []
        else:
            prints = [
                Voiceprint(
                    profile_id=p.profile_id,
                    centroids=np.asarray(p.centroids, dtype=np.float32),
                    thresholds=Thresholds(
                        tau_act=p.calib.tau.act,
                        margin_act=p.calib.margin.act,
                        tau_personal=p.calib.tau.personal,
                        margin_personal=p.calib.margin.personal,
                    ),
                )
                for p in event.data.profiles
                if p.profile_id != GUEST and p.centroids
            ]
        self.identifier.load(prints)
        self.profiles = {
            p.profile_id: ProfileInfo(tuple(p.hotwords), p.voice.voice_id, p.voice.rate, p.locale)
            for p in event.data.profiles
        }
        guest = self.profiles.get(GUEST)
        self.household_hotwords = guest.hotwords if guest is not None else ()
        log.info("profiles.synced", profiles=len(self.profiles), voiceprints=len(prints))

    # -------------------------------------------------------------- speaking --

    def _queue_speak(self, turn_id: str | None, data: SpeakData) -> None:
        if turn_id is not None and turn_id in self._cancelled_turns:
            return
        if self._speak_worker is None or self._speak_worker.done():
            self._speak_worker = self.loop.create_task(self._speak_loop(), name="speak-worker")
        if self.turn is not None and turn_id == self.turn.id and self.state == State.THINKING:
            self._set_state(State.SPEAKING, LedState.SPEAKING)
        expected = self._seq_next.get(turn_id, 0)
        hold = self._seq_hold.setdefault(turn_id, {})
        hold[data.seq] = data
        while expected in hold:
            self._speak_queue.put_nowait((turn_id, hold.pop(expected)))
            expected += 1
        self._seq_next[turn_id] = expected

    async def _speak_loop(self) -> None:
        while True:
            turn_id, data = await self._speak_queue.get()
            if turn_id is not None and turn_id in self._cancelled_turns:
                continue
            try:
                await self._speak_one(turn_id, data)
            except Exception as exc:
                log.error("speak.failed", turn_id=turn_id, error=repr(exc))
                await self._utterance_failed(turn_id, data)

    def _utterance(self, turn_id: str | None, data: SpeakData) -> Utterance:
        open_id = self._open_utterance.get(turn_id)
        if open_id is not None and open_id != data.utterance_id:
            prev = self._utterances.get(open_id)
            if prev is not None:
                prev.closed = True
                self._maybe_done(prev)
        self._open_utterance[turn_id] = data.utterance_id
        utt = self._utterances.get(data.utterance_id)
        if utt is None:
            utt = Utterance(turn_id, data.utterance_id)
            self._utterances[data.utterance_id] = utt
        if data.last:
            utt.closed = True
            utt.is_last = True
            utt.listen_after = data.listen_after
        return utt

    async def _speak_one(self, turn_id: str | None, data: SpeakData) -> None:
        profile = (
            self.turn.final_identity.profile_id if self.turn and self.turn.final_identity else GUEST
        )
        voice = VoiceRef(
            self._voice_for(profile, data.voice.voice_id), data.voice.rate, data.locale
        )
        vkey = ClipCache.voice_key(voice.voice_id, voice.rate)
        if data.clip == "alarm":
            self._play_alarm(turn_id, data)
            return
        utt = self._utterance(turn_id, data)
        utt.pending += 1
        item = self._speech_item(turn_id, utt)
        pcm: PCM16 | None = None
        fixed = spoken_text(data.clip) if data.clip else None
        if data.clip and fixed is not None:
            pcm = self.clips.clip(vkey, data.clip) or await self._render(fixed, voice)
            if pcm is not None:
                self.clips.put_clip(vkey, data.clip, pcm)
        elif data.clip and data.clip in self.earcons and data.text is None:
            pcm = self.earcons.get(data.clip)
        if pcm is None and data.text:
            pcm = self.clips.sentence(vkey, data.text)
            if pcm is None:
                await self._stream_synth(item, data.text, voice, vkey, turn_id)
                return
        if pcm is None:
            # An unknown clip key with no text (ARCHITECTURE §4.2): error earcon, speak.done{error}.
            self.mixer.play_earcon(self.earcons.get("error"))
            utt.reason = "error"
            utt.pending -= 1
            utt.closed = True
            self._maybe_done(utt)
            return
        item.append(pcm.copy())
        item.close()
        self.mixer.enqueue(item)

    async def _render(self, text: str, voice: VoiceRef) -> PCM16 | None:
        chunks = [c async for c in self.tts.synth(text, voice)]
        return concat(chunks) if chunks else None

    async def _stream_synth(
        self,
        item: SpeechItem,
        text: str,
        voice: VoiceRef,
        vkey: tuple[str, float],
        turn_id: str | None,
    ) -> None:
        self.mixer.enqueue(item)
        chunks: list[PCM16] = []
        stream = self.tts.synth(text, voice)
        try:
            async for chunk in stream:
                if turn_id is not None and turn_id in self._cancelled_turns:
                    break
                item.append(chunk)
                chunks.append(chunk)
        finally:
            await _aclose(stream)
            item.close()
        if chunks and not (turn_id is not None and turn_id in self._cancelled_turns):
            self.clips.put_sentence(vkey, text, concat(chunks))

    def _speech_item(self, turn_id: str | None, utt: Utterance) -> SpeechItem:
        def on_start(t_ns: int) -> None:
            self.loop.call_soon_threadsafe(self._item_started, utt, t_ns)

        def on_end(t_ns: int, samples: int, cancelled: bool) -> None:
            self.loop.call_soon_threadsafe(self._item_ended, utt, t_ns, samples, cancelled)

        return SpeechItem(turn_id, utt.utterance_id, on_start=on_start, on_end=on_end)

    def _item_started(self, utt: Utterance, t_ns: int) -> None:
        if utt.t_first is None:
            utt.t_first = t_ns

    def _item_ended(self, utt: Utterance, t_ns: int, samples: int, cancelled: bool) -> None:
        utt.pending -= 1
        utt.samples += samples
        utt.t_last = max(utt.t_last, t_ns)
        if cancelled:
            utt.reason = "cancelled"
            utt.closed = True
        self._maybe_done(utt)

    async def _utterance_failed(self, turn_id: str | None, data: SpeakData) -> None:
        utt = self._utterances.get(data.utterance_id)
        if utt is None:
            return
        utt.reason = "error"
        utt.closed = True
        self._maybe_done(utt)

    def _maybe_done(self, utt: Utterance) -> None:
        if utt.reported or not utt.closed or utt.pending > 0:
            return
        utt.reported = True
        self._utterances.pop(utt.utterance_id, None)
        if self._open_utterance.get(utt.turn_id) == utt.utterance_id:
            self._open_utterance.pop(utt.turn_id, None)
        t_first = utt.t_first if utt.t_first is not None else max(utt.t_last, self._clock())
        t_last = max(utt.t_last, t_first)
        self._spawn(
            self.send(
                SpeakDoneEvent(
                    turn_id=utt.turn_id,
                    data=SpeakDoneData(
                        utterance_id=utt.utterance_id,
                        reason=utt.reason,
                        audio_ms=samples_to_ms(utt.samples),
                        t_first_audio=t_first,
                        t_last_audio=t_last,
                    ),
                )
            ),
            f"speak-done-{utt.utterance_id}",
        )
        if (
            utt.is_last
            and utt.reason == "finished"
            and self.turn is not None
            and utt.turn_id == self.turn.id
            and self.state == State.SPEAKING
        ):
            self._seq_next.pop(utt.turn_id, None)
            self._seq_hold.pop(utt.turn_id, None)
            if utt.listen_after:
                self._open_reply_window()
            else:
                self._end_turn()

    def _play_alarm(self, turn_id: str | None, data: SpeakData) -> None:
        self.mixer.play_alert(self.earcons.get("alarm"), turn_id=turn_id, loop=True)
        self.leds.show(LedState.ALERT)
        now = self._clock()
        self._spawn(
            self.send(
                SpeakDoneEvent(
                    turn_id=turn_id,
                    data=SpeakDoneData(
                        utterance_id=data.utterance_id,
                        reason="finished",
                        audio_ms=0,
                        t_first_audio=now,
                        t_last_audio=now,
                    ),
                )
            ),
            f"alarm-{data.utterance_id}",
        )

    async def play_and_wait(self, text: str, voice: VoiceRef, turn_id: str | None) -> None:
        """Speak one system sentence and wait until it has played (PIN prompt)."""
        done: asyncio.Future[None] = self.loop.create_future()

        def ended(_t: int, _n: int, _cancelled: bool) -> None:
            self.loop.call_soon_threadsafe(lambda: done.done() or done.set_result(None))

        item = SpeechItem(turn_id, f"pin-prompt-{turn_id}", on_end=ended)
        vkey = ClipCache.voice_key(voice.voice_id, voice.rate)
        pcm = self.clips.clip(vkey, "pin.prompt") if text == SYSTEM_PHRASES["pin.prompt"] else None
        if pcm is None:
            pcm = await self._render(text, voice)
        if pcm is None:
            return
        item.append(pcm.copy())
        item.close()
        self.mixer.enqueue(item)
        await done

    # ----------------------------------------------------------------- cancel --

    async def _cancel(self, turn_id: str | None) -> None:
        if turn_id is not None:
            self._cancelled_turns.add(turn_id)
            if len(self._cancelled_turns) > 64:
                self._cancelled_turns.pop()
        flushed = self.mixer.flush_speech(turn_id)
        stopped = self.mixer.stop_alerts(turn_id)
        self._seq_next.pop(turn_id, None)
        self._seq_hold.pop(turn_id, None)
        pin = self.pin
        if pin is not None and pin.turn_id == turn_id:
            await self._pin_done(pin, "cancelled")
        active = self.turn is not None and self.turn.id == turn_id
        if active and self.turn is not None:
            turn = self.turn
            turn.finishing = True
            turn.stt_queue.put_nowait(None)
            if turn.stt_task is not None:
                turn.stt_task.cancel()
        if active or flushed or stopped:
            self.mixer.play_earcon(self.earcons.get("cancel"))
            self.leds.show(LedState.CANCEL)
        if active or (self.state == State.REPLY_WINDOW and turn_id is not None):
            self._end_turn()

    # -------------------------------------------------------------------- PIN --

    async def _capture_pin(self, event: CapturePinEvent) -> None:
        data = event.data
        turn = self.turn
        if (
            turn is None
            or event.turn_id != turn.id
            or self.state not in (State.LISTENING, State.THINKING, State.SPEAKING)
            or self.pin is not None
        ):
            await self.send(
                PinResultEvent(
                    turn_id=event.turn_id,
                    data=PinResultData(approval_id=data.approval_id, outcome="cancelled"),
                )
            )
            return
        pin = PinCapture(
            turn_id=turn.id,
            approval_id=data.approval_id,
            pin_hash=data.pin_hash,
            min_len=data.min_len,
            max_len=data.max_len,
            deadline_ns=self._t_now + data.timeout_ms * 1_000_000,
        )
        self.pin = pin
        self._set_state(State.PIN_CAPTURE, LedState.PIN)
        profile = turn.final_identity.profile_id if turn.final_identity else GUEST
        voice = VoiceRef(self._voice_for(profile), 1.0, turn.locale)
        if profile in self.profiles:
            voice = VoiceRef(voice.voice_id, self.profiles[profile].rate, turn.locale)
        await self.play_and_wait(data.prompt or SYSTEM_PHRASES["pin.prompt"], voice, turn.id)
        if self.pin is not pin:
            return
        self.mixer.play_earcon(self.earcons.get("pin"))
        remaining_ms = max(0, (pin.deadline_ns - self._t_now) // 1_000_000)
        pin.endpointer = Endpointer(
            self.config.audio.endpoint,
            self._t_now,
            locale=turn.locale,
            speech_start_timeout_ms=int(remaining_ms),
        )
        ctx = SttContext(locale=turn.locale, hotwords=(), hotwords_score=0.0, mode="pin")
        pin.task = self._spawn(self._pin_stt(pin, ctx), f"pin-stt-{turn.id}")
        pin.listening = True

    async def _pin_stt(self, pin: PinCapture, ctx: SttContext) -> Final | None:
        stream = self.stt.stream(_drain(pin.queue), ctx)
        final: Final | None = None
        try:
            async for item in stream:
                if isinstance(item, Final):  # partials in pin mode are dropped unseen
                    final = item
                    break
        finally:
            await _aclose(stream)
        return final

    async def _pin_frame(self, frame: Frame) -> None:
        pin = self.pin
        if pin is None or pin.finishing:
            return
        if frame.t_ns >= pin.deadline_ns:
            await self._pin_done(pin, "timeout")
            return
        if not pin.listening or pin.endpointer is None:
            return
        pin.queue.put_nowait(frame.pcm)
        for ev in pin.endpointer.update(frame.speech, frame.t_ns):
            if ev == EndpointEvent.TIMEOUT:
                await self._pin_done(pin, "timeout")
                return
            if ev in (EndpointEvent.EOS, EndpointEvent.MAX_LEN):
                pin.finishing = True
                pin.queue.put_nowait(None)
                self._spawn(self._pin_verify(pin), f"pin-verify-{pin.turn_id}")
                return

    async def _pin_verify(self, pin: PinCapture) -> None:
        final = await pin.task if pin.task is not None else None
        text = final.text if final is not None else ""
        pin_hash = pin.pin_hash
        outcome: PinOutcome = await self.loop.run_in_executor(
            self._executor,
            functools.partial(
                outcome_for_text, text, pin_hash, min_len=pin.min_len, max_len=pin.max_len
            ),
        )
        await self._pin_done(pin, outcome)

    async def _pin_done(self, pin: PinCapture, outcome: PinOutcome) -> None:
        if self.pin is not pin:
            return
        self.pin = None
        pin.finishing = True
        pin.queue.put_nowait(None)
        if pin.task is not None and not pin.task.done() and outcome in ("timeout", "cancelled"):
            pin.task.cancel()
        pin.wipe()
        await self.send(
            PinResultEvent(
                turn_id=pin.turn_id,
                data=PinResultData(approval_id=pin.approval_id, outcome=outcome),
            )
        )
        if (
            self.turn is not None
            and self.turn.id == pin.turn_id
            and self.state == State.PIN_CAPTURE
        ):
            self._t_thinking = self._t_now
            self._set_state(State.THINKING, LedState.THINKING)

    # ------------------------------------------------------------------- mute --

    async def set_hardware_mute(self, on: bool) -> None:
        if on == self.hw_muted:
            return
        self.hw_muted = on
        await self._apply_mute("hardware")

    async def set_software_mute(self, on: bool, until: float | None = None) -> None:
        if self._unmute_handle is not None:
            self._unmute_handle.cancel()
            self._unmute_handle = None
        if on and until is not None:
            delay = max(0.0, until - time.time())
            self._unmute_handle = self.loop.call_later(
                delay, lambda: self._spawn(self.set_software_mute(False), "unmute")
            )
        if on == self.sw_muted:
            return
        self.sw_muted = on
        if not on and self.hw_muted:
            # Software mute cannot override a hardware mute (§13.2): still muted.
            await self.send(MicStateEvent(data=MicStateData(muted=True, source="hardware")))
            return
        await self._apply_mute("software")

    async def _apply_mute(self, source: Literal["hardware", "software"]) -> None:
        muted = self.muted
        self.frontend.set_muted(muted)
        if muted:
            turn = self.turn
            if turn is not None and self.state == State.LISTENING:
                task = self._request_finish(turn, "cancel", t_eos=self._t_now)
                if task is not None:
                    await task
            if self.pin is not None:
                await self._pin_done(self.pin, "cancelled")
            self.turn = None
            self._reply = None
            self._set_state(State.MUTED, LedState.MUTED)
            self.mixer.play_earcon(self.earcons.get("mute"))
        else:
            self._set_state(State.IDLE, LedState.IDLE)
            self.mixer.play_earcon(self.earcons.get("unmute"))
        await self.send(MicStateEvent(data=MicStateData(muted=muted, source=source)))

    # ------------------------------------------------------------- enrollment --

    def _new_enroll_endpointer(self) -> Endpointer:
        return Endpointer(
            self.config.audio.endpoint,
            self._t_now,
            locale=self.config.locale,
            speech_start_timeout_ms=ENROLL_LISTEN_MS,
        )

    def _start_enroll(self, event: EnrollStartEvent) -> None:
        if self.enroll is not None:
            self._stop_enroll()
        session = EnrollmentSession(event.data.session_id, event.data.profile_id, event.data.mode)
        self.enroll = EnrollCapture(session, self._new_enroll_endpointer())
        self._set_state(self.state if self.state != State.IDLE else State.IDLE, LedState.ENROLL)
        log.info("enroll.started", session_id=session.session_id, mode=session.mode)

    def _stop_enroll(self) -> None:
        if self.enroll is None:
            return
        for f in self.enroll.frames:
            wipe(f.pcm)
        self.enroll = None
        self._set_state(self.state, LedState.IDLE)

    async def _enroll_frame(self, frame: Frame) -> None:
        cap = self.enroll
        if cap is None:
            return
        cap.frames.append(frame)
        max_frames = (self.config.audio.endpoint.max_utterance_ms + NOISE_MS + 1000) // 10
        if (not cap.endpointer.speech_started and len(cap.frames) > NOISE_MS // 10 + 100) or len(
            cap.frames
        ) > max_frames:
            wipe(cap.frames.pop(0).pcm)
        for ev in cap.endpointer.update(frame.speech, frame.t_ns):
            if ev == EndpointEvent.TIMEOUT:
                cap.endpointer = self._new_enroll_endpointer()
                return
            if ev in (EndpointEvent.EOS, EndpointEvent.MAX_LEN):
                frames = cap.frames
                endpointer = cap.endpointer
                cap.frames = []
                cap.endpointer = self._new_enroll_endpointer()
                await self._enroll_clip(cap.session, frames, endpointer)
                return

    async def _enroll_clip(
        self, session: EnrollmentSession, frames: list[Frame], endpointer: Endpointer
    ) -> None:
        start = endpointer.t_speech_start_ns or frames[0].t_ns
        end = endpointer.t_eos_ns or frames[-1].t_ns
        noise_frames = [f for f in frames if start - NOISE_MS * 1_000_000 <= f.t_ns < start]
        clip_frames = [f for f in frames if start <= f.t_ns <= end]
        clip = concat([f.pcm for f in clip_frames])
        noise = concat([f.pcm for f in noise_frames])
        speech_frames = sum(1 for f in clip_frames if f.speech)
        wake_fired = any(f.wake is not None for f in clip_frames)
        quality = measure(clip, noise, speech_frames, len(clip_frames))
        reason = gate(quality, mode=session.mode, wake_fired=wake_fired)
        embedding: list[float] | None = None
        if reason is None and self.speaker is not None:
            speaker = self.speaker
            try:
                emb = await self.loop.run_in_executor(self._executor, speaker.embed, clip)
            except ValueError:
                reason = "embed_failed"
            else:
                reason = session.consistency(emb)
                if reason is None:
                    session.accepted.append(emb)
                    embedding = [float(x) for x in emb]
        elif reason is None:
            reason = "embed_failed"
        wipe(clip)
        wipe(noise)
        for f in frames:
            wipe(f.pcm)
        log.info(
            "enroll.clip", session_id=session.session_id, accepted=reason is None, reason=reason
        )
        await self.send(
            EnrollClipEvent(
                data=EnrollClipData(
                    session_id=session.session_id,
                    index=session.next_index(),
                    accepted=embedding is not None,
                    quality=EnrollQuality(
                        snr_db=quality.snr_db,
                        clipping=quality.clipping,
                        speech_ratio=quality.speech_ratio,
                        duration_ms=quality.duration_ms,
                    ),
                    embedding=embedding,
                )
            )
        )


__all__ = ["Pipeline", "ProfileInfo", "Send", "State", "Turn", "cap_hotwords"]
