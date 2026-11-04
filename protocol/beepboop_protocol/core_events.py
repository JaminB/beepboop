"""Events sent by core to the audio service (ARCHITECTURE §4.2, "Core → audio").

Provider configuration never travels as an event (CLAUDE.md hard rule 4); it
reaches audio through the read-only ``/config/beepboop.yaml``.
"""

# Each event narrows the base ``type: str`` to a Literal discriminator. Models are
# frozen, so the narrowing is sound even though pyright sees a mutable override.
# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field, model_validator

from beepboop_protocol.envelope import (
    GUEST_PROFILE_ID,
    Embedding,
    EventBase,
    Locale,
    Name,
    ProfileId,
    TurnScope,
    WireModel,
)

#: The single earcon name list (VOICE_PIPELINE §13.1 owns it).
EarconName = Literal[
    "wake",
    "ack",
    "done",
    "prompt",
    "cancel",
    "error",
    "offline",
    "pin",
    "mute",
    "unmute",
    "alarm",
    "ready",
    "too_long",
]

#: Spoken-PIN bounds (IDENTITY_AND_TRUST §8.4 is the single source).
PIN_MIN_LEN = 4
PIN_MAX_LEN = 8
PIN_TIMEOUT_MS = 10_000

# --------------------------------------------------------------------- hello --


class CoreHelloData(WireModel):
    protocol: int = Field(ge=1)
    role: Literal["core"] = "core"


class CoreHelloEvent(EventBase):
    """First event core sends on a new connection."""

    type: Literal["hello"] = "hello"
    data: CoreHelloData


# --------------------------------------------------------------------- speak --


class VoiceSpec(WireModel):
    voice_id: Name
    rate: float = Field(gt=0.0, le=4.0)


class SpeakData(WireModel):
    """One sentence (or cached clip) of the reply.

    ``clip`` is a key from the namespace ARCHITECTURE §4.2 defines (system-phrase
    keys, ack keys, earcon names). At least one of ``clip`` and ``text`` is
    present; ``text`` is what audio synthesizes when it has no clip for the key.
    The key set is not enumerated here: it is owned by VOICE_PIPELINE and the
    persona module may extend it, so only the key syntax is validated.
    """

    utterance_id: Name
    seq: int = Field(ge=0)
    last: bool
    text: str | None = Field(default=None, min_length=1)
    clip: str | None = Field(default=None, pattern=r"^[a-z0-9_.]{1,64}$")
    voice: VoiceSpec
    locale: Locale
    listen_after: bool

    @model_validator(mode="after")
    def _something_to_say(self) -> SpeakData:
        if self.text is None and self.clip is None:
            raise ValueError("speak needs text, clip or both")
        if self.text is not None and not self.text.strip():
            raise ValueError("speak text must not be blank")
        return self


class SpeakEvent(EventBase):
    CANONICAL: ClassVar[bool] = True
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["speak"] = "speak"
    data: SpeakData


# -------------------------------------------------------------------- cancel --


class CancelData(WireModel):
    reason: Literal["barge_in", "superseded", "error", "user"]


class CancelEvent(EventBase):
    """Stop STT/TTS for this turn and flush the playback queue."""

    CANONICAL: ClassVar[bool] = True
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["cancel"] = "cancel"
    data: CancelData


# --------------------------------------------------------------- capture_pin --


class CapturePinData(WireModel):
    """Ask audio to capture and verify a spoken PIN against ``pin_hash``.

    ``pin_hash`` is an argon2id PHC string. It is hidden from ``repr`` and
    never logged; audio answers with ``pin.result{outcome}`` only. The
    catalog's ``turn_id`` travels in the envelope (required here).
    """

    approval_id: Name
    pin_hash: str = Field(repr=False, pattern=r"^\$argon2id\$", max_length=512)
    min_len: int = Field(default=PIN_MIN_LEN, ge=PIN_MIN_LEN, le=PIN_MAX_LEN)
    max_len: int = Field(default=PIN_MAX_LEN, ge=PIN_MIN_LEN, le=PIN_MAX_LEN)
    timeout_ms: int = Field(default=PIN_TIMEOUT_MS, gt=0, le=60_000)
    prompt: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _length_range(self) -> CapturePinData:
        if self.min_len > self.max_len:
            raise ValueError("min_len must not exceed max_len")
        return self


class CapturePinEvent(EventBase):
    CANONICAL: ClassVar[bool] = True
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["capture_pin"] = "capture_pin"
    data: CapturePinData


# -------------------------------------------------------------- earcon/mute --


class EarconData(WireModel):
    name: EarconName


class EarconEvent(EventBase):
    """Non-speech feedback; may belong to a turn (the ``ack`` of a tool call)."""

    TURN_SCOPE: ClassVar[TurnScope] = "optional"
    type: Literal["earcon"] = "earcon"
    data: EarconData


class MuteData(WireModel):
    on: bool
    until: float | None = Field(default=None, ge=0.0, description="Unix seconds")

    @model_validator(mode="after")
    def _until_only_when_on(self) -> MuteData:
        if not self.on and self.until is not None:
            raise ValueError("until is only meaningful when muting")
        return self


class MuteEvent(EventBase):
    type: Literal["mute"] = "mute"
    data: MuteData


# ------------------------------------------------------------- profiles.sync --


class TierThresholds(WireModel):
    act: float
    personal: float


class Calibration(WireModel):
    tau: TierThresholds
    margin: TierThresholds


class ProfileVoiceprint(WireModel):
    profile_id: ProfileId
    centroids: list[Embedding]
    calib: Calibration
    hotwords: list[str]
    voice: VoiceSpec
    locale: Locale


class ProfilesSyncData(WireModel):
    """Full snapshot of voiceprints; audio keeps it in memory only."""

    profiles: list[ProfileVoiceprint]
    cohort_sha: Name
    model_id: Name

    @model_validator(mode="after")
    def _unique_profiles(self) -> ProfilesSyncData:
        ids = [p.profile_id for p in self.profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("profiles.sync must not repeat a profile_id")
        if len({len(c) for p in self.profiles for c in p.centroids}) > 1:
            raise ValueError("all centroids in one profiles.sync must have the same length")
        return self


class ProfilesSyncEvent(EventBase):
    type: Literal["profiles.sync"] = "profiles.sync"
    data: ProfilesSyncData

    def embedding_lengths(self) -> tuple[int, ...]:
        return tuple(len(c) for p in self.data.profiles for c in p.centroids)


# ---------------------------------------------------------------- enrollment --


class EnrollStartData(WireModel):
    session_id: Name
    profile_id: ProfileId
    mode: Literal["wake", "free"]
    prompts: list[str]

    @model_validator(mode="after")
    def _not_guest(self) -> EnrollStartData:
        # Guest is "unrecognized"; it has no voiceprint to enroll.
        if self.profile_id == GUEST_PROFILE_ID:
            raise ValueError("the guest profile cannot be enrolled")
        return self


class EnrollStartEvent(EventBase):
    type: Literal["enroll.start"] = "enroll.start"
    data: EnrollStartData


class EnrollStopData(WireModel):
    session_id: Name


class EnrollStopEvent(EventBase):
    type: Literal["enroll.stop"] = "enroll.stop"
    data: EnrollStopData


#: Every event core may send, discriminated on ``type``.
type CoreEvent = Annotated[
    CoreHelloEvent
    | SpeakEvent
    | CancelEvent
    | CapturePinEvent
    | EarconEvent
    | MuteEvent
    | ProfilesSyncEvent
    | EnrollStartEvent
    | EnrollStopEvent,
    Field(discriminator="type"),
]

CORE_EVENT_CLASSES: tuple[type[EventBase], ...] = (
    CoreHelloEvent,
    SpeakEvent,
    CancelEvent,
    CapturePinEvent,
    EarconEvent,
    MuteEvent,
    ProfilesSyncEvent,
    EnrollStartEvent,
    EnrollStopEvent,
)

__all__ = [
    "CORE_EVENT_CLASSES",
    "PIN_MAX_LEN",
    "PIN_MIN_LEN",
    "PIN_TIMEOUT_MS",
    "Calibration",
    "CancelData",
    "CancelEvent",
    "CapturePinData",
    "CapturePinEvent",
    "CoreEvent",
    "CoreHelloData",
    "CoreHelloEvent",
    "EarconData",
    "EarconEvent",
    "EarconName",
    "EnrollStartData",
    "EnrollStartEvent",
    "EnrollStopData",
    "EnrollStopEvent",
    "MuteData",
    "MuteEvent",
    "ProfileVoiceprint",
    "ProfilesSyncData",
    "ProfilesSyncEvent",
    "SpeakData",
    "SpeakEvent",
    "TierThresholds",
    "VoiceSpec",
]
