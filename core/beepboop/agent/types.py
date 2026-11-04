"""Agent-loop value types and seams (docs/ARCHITECTURE.md §5, OPERATIONS §11 stage names).

The seams other builders implement:

* :class:`Speaker`: speech and earcons for one turn (audio bridge: ``speak``/``earcon``
  events; ``beepboop chat``: stdout);
* :class:`VoiceControl`: ``cancel`` and ``capture_pin`` (audio bridge);
* :class:`ConversationStore`: the 90 s window (:class:`beepboop.agent.records.DbConversation`);
* :class:`TurnLog`: ``turns`` rows, "that wasn't me", "forget that"
  (:class:`beepboop.agent.records.DbTurnLog`);
* :class:`ProfileDirectory`: names and kinds for phrases and the prompt;
* :class:`PinStore`: a profile's PIN hash and lockout state (audio bridge / secrets).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from beepboop.policy.pin import PinOutcome, PinState
from beepboop.policy.types import Principal, ProfileKind

TurnPath = Literal["fast", "llm", "offline", "error"]
CancelReason = Literal["barge_in", "superseded", "error", "user"]

#: The fixed stage names (OPERATIONS §11 owns this table).
STAGES: tuple[str, ...] = (
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

MAX_TOOL_CALLS_PER_TURN = 8
CONVERSATION_WINDOW_S = 90
REPLY_WINDOW_S = 8
#: ARCHITECTURE §5 step 4 / IDENTITY_AND_TRUST §3.1: wait this long after ``stt.final``.
SPEAKER_FINAL_WAIT_S = 0.3
#: VOICE_PIPELINE §8.8: "that wasn't me" applies to the previous turn within 5 minutes.
NOT_ME_WINDOW_S = 300


@dataclass(frozen=True, slots=True)
class Utterance:
    """One ``stt.final`` (or one chat line) bound to its Principal."""

    text: str
    principal: Principal
    locale: str = "en-US"
    device: str = "main"
    stt_provider: str = "sherpa-zipformer"
    tts_provider: str = "sherpa-piper"
    id_stage: Literal["provisional", "final"] | None = None
    #: stages measured before the loop (endpoint, stt_tail, speaker_final), in ms.
    stages_ms: dict[str, float] = field(default_factory=dict[str, float])


@dataclass(frozen=True, slots=True)
class ToolCallSummary:
    tool: str
    decision: str
    reason: str | None = None
    risk: str = "read"
    duration_ms: int | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class Speech:
    """One ``speak`` event's worth: a system-phrase ``clip`` key, free ``text``, or both."""

    text: str | None = None
    clip: str | None = None
    listen_after: bool = False


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """A ``confirm`` that paused the turn (ARCHITECTURE §5 confirm path)."""

    approval_id: str
    tool: str
    args: dict[str, object]
    summary: str
    factors: tuple[str, ...]
    exp: datetime


@dataclass(slots=True)
class TurnResult:
    turn_id: str
    path: TurnPath
    speech: list[Speech] = field(default_factory=list[Speech])
    tool_calls: list[ToolCallSummary] = field(default_factory=list[ToolCallSummary])
    stages_ms: dict[str, object] = field(default_factory=dict[str, object])
    tainted: bool = False
    pending_approval_id: str | None = None
    pending: PendingApproval | None = None
    error: str | None = None
    llm_provider: str | None = None
    rung: str | None = None
    tools_offered: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    cost_micro_usd: int = 0
    prefix_hash: str | None = None
    stop_reason: str | None = None
    cancelled: bool = False

    @property
    def reply_text(self) -> str:
        return " ".join(s.text for s in self.speech if s.text)


class Speaker(Protocol):
    """What the loop needs from the audio side (or the chat UI): speech and earcons."""

    async def speak(self, turn_id: str, speech: Speech, *, last: bool) -> None: ...
    async def earcon(self, turn_id: str, name: str) -> None: ...


class VoiceControl(Protocol):
    """Turn control toward the audio service (ARCHITECTURE §4.2 ``cancel``, ``capture_pin``)."""

    async def cancel(self, turn_id: str, reason: CancelReason) -> None: ...
    async def capture_pin(
        self, turn_id: str, approval_id: str, pin_hash: str, prompt: str | None
    ) -> None: ...


class ConversationStore(Protocol):
    """The 90 s window (DATA_AND_PRIVACY §6.1 ``conversation``), keyed (profile, device)."""

    async def load(self, profile_id: str, device: str) -> tuple[list[dict[str, object]], bool]: ...
    async def save(
        self, profile_id: str, device: str, messages: list[dict[str, object]], tainted: bool
    ) -> None: ...
    async def taint(self, profile_id: str, device: str) -> None: ...
    async def clear(self, profile_id: str, device: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TurnStart:
    """What is known when a turn begins; the ``turns`` row is inserted from it so the
    registry's ``tool_calls`` rows (FK ``turn_id``) can be written during the turn."""

    turn_id: str
    principal: Principal
    locale: str
    device: str
    stt_provider: str
    tts_provider: str
    id_stage: Literal["provisional", "final"] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PreviousTurn:
    turn_id: str
    profile_id: str
    created_at: datetime


class TurnLog(Protocol):
    """``turns`` rows and the two core actions that touch them (DATA_AND_PRIVACY §6.1, §9.3)."""

    async def start(self, turn: TurnStart) -> None: ...
    async def finish(self, turn: TurnStart, transcript: str, result: TurnResult) -> None: ...
    async def previous(self, device: str, before_turn_id: str) -> PreviousTurn | None: ...
    async def not_me(self, turn_id: str, *, corrector: Principal, disputed: bool) -> None: ...
    async def forget(self, turn_id: str) -> bool: ...
    async def flag(self, turn_id: str) -> None:
        """IDENTITY_AND_TRUST §11 ``flagged`` for events the registry does not see (PIN lockout)."""
        ...


class ProfileDirectory(Protocol):
    """Names and kinds for phrases and the volatile prompt block."""

    def kind(self, profile_id: str) -> ProfileKind: ...
    def name(self, profile_id: str) -> str: ...
    def admin_name(self) -> str: ...


class PinStore(Protocol):
    """A profile's spoken-PIN hash (unwrapped only to send in ``capture_pin``) and its
    lockout state (IDENTITY_AND_TRUST §8.4; columns ``profiles.pin_*``)."""

    async def load(self, profile_id: str) -> tuple[str | None, PinState]: ...
    async def save(self, profile_id: str, state: PinState) -> None: ...


__all__ = [
    "CONVERSATION_WINDOW_S",
    "MAX_TOOL_CALLS_PER_TURN",
    "NOT_ME_WINDOW_S",
    "REPLY_WINDOW_S",
    "SPEAKER_FINAL_WAIT_S",
    "STAGES",
    "CancelReason",
    "ConversationStore",
    "PendingApproval",
    "PinOutcome",
    "PinStore",
    "PreviousTurn",
    "ProfileDirectory",
    "Speaker",
    "Speech",
    "ToolCallSummary",
    "TurnLog",
    "TurnPath",
    "TurnResult",
    "TurnStart",
    "Utterance",
    "VoiceControl",
]
