"""Voice turns: audio events → Principal → ``TurnLoop`` (ARCHITECTURE §5, §11; IDENTITY §3.1).

:class:`VoiceSession` is the per-connection turn state machine core runs for one audio
service. The audio bridge feeds it every event it receives (``on_event``) and gives it a
:class:`~beepboop.agent.types.Speaker` and :class:`~beepboop.agent.types.VoiceControl`
that send ``speak`` / ``earcon`` / ``cancel`` / ``capture_pin`` on the socket. It owns:

* **wake**: supersede any in-flight turn on that device (``cancel{superseded}``) and
  pre-warm the LLM (fire-and-forget). A muted microphone starts no turn.
* **provisional ID**: kept for personalization only; it never authorizes anything.
* **final ID**: the Principal is built once from ``speaker.final``. If it has not arrived
  ``SPEAKER_FINAL_WAIT_S`` (300 ms) after ``stt.final``, the turn runs as ``guest`` and
  this is logged. Guest is ``profile_id == "guest"``.
* **stt.final**: ``empty`` (false wake) ends the turn silently; otherwise one task runs
  ``TurnLoop.handle`` and, if the turn paused for approval, ``follow_approval`` (the turn
  stays open until the approval is decided or expires).
* **pin.result**: routed to the waiting approval; digits never arrive here.
* cancellation is cooperative and keyed by ``turn_id``: superseding cancels the task,
  which closes the LLM stream (an in-flight HA call is left to finish).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from beepboop.agent.turn import TurnLoop
from beepboop.agent.types import (
    SPEAKER_FINAL_WAIT_S,
    CancelReason,
    TurnResult,
    Utterance,
    VoiceControl,
)
from beepboop.log import get_logger
from beepboop.policy.types import ALL_SCOPES, GUEST_PROFILE_ID, Principal
from beepboop_protocol import (
    AudioEvent,
    MicStateEvent,
    PinResultEvent,
    SpeakerFinalData,
    SpeakerFinalEvent,
    SpeakerProvisionalData,
    SpeakerProvisionalEvent,
    SttFinalData,
    SttFinalEvent,
    WakeEvent,
)

log = get_logger(__name__)


@dataclass
class _VoiceTurn:
    turn_id: str
    device: str
    trigger: str
    t_wake_mono: float
    provisional: SpeakerProvisionalData | None = None
    final: SpeakerFinalData | None = None
    final_seen: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[TurnResult | None] | None = None


def _ns_to_ms(ns: int) -> float:
    return round(ns / 1_000_000, 1)


@dataclass
class VoiceSession:
    """Turn state for one audio connection. Feed it every received event."""

    loop: TurnLoop
    control: VoiceControl
    tts_provider: str = "sherpa-piper"
    speaker_final_wait_s: float = SPEAKER_FINAL_WAIT_S
    monotonic: Callable[[], float] = time.monotonic
    muted: bool = False
    _turns: dict[str, _VoiceTurn] = field(default_factory=dict[str, _VoiceTurn])
    _active: dict[str, str] = field(default_factory=dict[str, str])
    _background: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    results: list[TurnResult] = field(default_factory=list[TurnResult])

    def __post_init__(self) -> None:
        if self.loop.control is None:
            self.loop.control = self.control

    @property
    def busy(self) -> bool:
        """A turn is open on some device (``announce(priority="low")`` waits it out)."""
        return bool(self._active)

    async def on_event(self, event: AudioEvent) -> None:
        if isinstance(event, WakeEvent):
            await self._on_wake(event)
        elif isinstance(event, SpeakerProvisionalEvent):
            turn = self._turns.get(event.turn_id or "")
            if turn is not None:
                turn.provisional = event.data
        elif isinstance(event, SpeakerFinalEvent):
            turn = self._turns.get(event.turn_id or "")
            if turn is not None:
                turn.final = event.data
                turn.final_seen.set()
        elif isinstance(event, SttFinalEvent):
            self._on_stt_final(event)
        elif isinstance(event, PinResultEvent):
            if not self.loop.deliver_pin_result(event.data.approval_id, event.data.outcome):
                log.info("pin.result_unrouted", turn_id=event.turn_id)
        elif isinstance(event, MicStateEvent):
            self.muted = event.data.muted
            if self.muted:
                for turn_id in list(self._active.values()):
                    await self._cancel(turn_id, "user")

    async def _on_wake(self, event: WakeEvent) -> None:
        turn_id = event.turn_id or ""
        device = event.data.device
        if self.muted:
            log.info("turn.refused_muted", turn_id=turn_id)
            return
        previous = self._active.get(device)
        if previous is not None and previous != turn_id:
            await self._cancel(previous, "superseded")
        self._turns[turn_id] = _VoiceTurn(turn_id, device, event.data.trigger, self.monotonic())
        self._active[device] = turn_id
        task = asyncio.create_task(self.loop.warm())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _on_stt_final(self, event: SttFinalEvent) -> None:
        turn = self._turns.get(event.turn_id or "")
        if turn is None:
            log.info("stt.final_unknown_turn", turn_id=event.turn_id)
            return
        if event.data.empty or not event.data.text.strip():
            log.info("turn.false_wake", turn_id=turn.turn_id)
            self._drop(turn)
            return
        turn.task = asyncio.create_task(self._run(turn, event.data, self.monotonic()))

    async def _run(self, turn: _VoiceTurn, stt: SttFinalData, t_stt: float) -> TurnResult | None:
        blog = log.bind(turn_id=turn.turn_id)
        try:
            if turn.final is None:
                remaining = self.speaker_final_wait_s - (self.monotonic() - t_stt)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(turn.final_seen.wait(), max(0.0, remaining))
            principal, id_stage = self._principal(turn)
            if turn.final is None:
                blog.warning("speaker.final_late", wait_ms=round(self.speaker_final_wait_s * 1000))
            stages: dict[str, float] = {
                "speech": _ns_to_ms(stt.t_eos - stt.t_speech_start),
                "stt_tail": _ns_to_ms(stt.t_final - stt.t_eos),
                "speaker_final": round((self.monotonic() - t_stt) * 1000, 1),
            }
            utterance = Utterance(
                stt.text,
                principal,
                locale=stt.locale,
                device=turn.device,
                stt_provider=stt.provider,
                tts_provider=self.tts_provider,
                id_stage=id_stage,
                stages_ms=stages,
            )
            result = await self.loop.handle(utterance)
            self.results.append(result)
            if result.pending is not None:
                await self.loop.follow_approval(result, utterance)
            return result
        except asyncio.CancelledError:
            blog.info("turn.cancelled")
            raise
        finally:
            self._drop(turn)

    def _principal(self, turn: _VoiceTurn) -> tuple[Principal, Literal["final"] | None]:
        final = turn.final
        if final is None:
            return (
                Principal(
                    GUEST_PROFILE_ID,
                    "voice",
                    0.0,
                    0.0,
                    turn.device,
                    ALL_SCOPES,
                    False,
                    turn.turn_id,
                ),
                None,
            )
        return (
            Principal(
                final.profile_id,
                "voice",
                final.score,
                final.margin,
                turn.device,
                ALL_SCOPES,
                False,  # the registry/loop snapshot conversation taint per call (§3.2)
                turn.turn_id,
            ),
            "final",
        )

    async def _cancel(self, turn_id: str, reason: CancelReason) -> None:
        turn = self._turns.get(turn_id)
        with contextlib.suppress(Exception):
            await self.control.cancel(turn_id, reason)
        if turn is not None and turn.task is not None and not turn.task.done():
            turn.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await turn.task
        if turn is not None:
            self._drop(turn)

    def _drop(self, turn: _VoiceTurn) -> None:
        self._turns.pop(turn.turn_id, None)
        if self._active.get(turn.device) == turn.turn_id:
            del self._active[turn.device]

    async def close(self) -> None:
        """Audio disconnected: cancel every open turn (ARCHITECTURE §10)."""
        for turn in list(self._turns.values()):
            if turn.task is not None and not turn.task.done():
                turn.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await turn.task
        self._turns.clear()
        self._active.clear()
        for task in list(self._background):
            task.cancel()

    async def drain(self) -> None:
        """Wait for every running turn (tests, shutdown)."""
        tasks = [t.task for t in self._turns.values() if t.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["VoiceSession"]
