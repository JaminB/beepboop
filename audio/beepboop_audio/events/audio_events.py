"""Events sent by the audio service to core (ARCHITECTURE §4.2, "Audio → core").

When each one fires is VOICE_PIPELINE §5. Field names, types and enumerations
here are exactly the catalog's; anything stricter than the catalog is an
invariant the spec states in prose, noted on the validator.
"""

# Each event narrows the base ``type: str`` to a Literal discriminator. Models are
# frozen, so the narrowing is sound even though pyright sees a mutable override.
# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field, model_validator

from beepboop_audio.events.envelope import (
    EMBEDDING_DIM,
    Embedding,
    EventBase,
    Locale,
    Millis,
    MonotonicNs,
    Name,
    ProfileId,
    TurnScope,
    WireModel,
)

# --------------------------------------------------------------------- hello --


class DeviceInfo(WireModel):
    id: Name
    name: str = Field(max_length=128)


class WakeInfo(WireModel):
    provider: Name
    models: list[Name]


class SttInfo(WireModel):
    provider: Name
    locale: Locale


class TtsInfo(WireModel):
    provider: Name
    voices: list[Name]


class SpeakerInfo(WireModel):
    model_id: Name
    model_sha: Name
    dim: int

    @model_validator(mode="after")
    def _fixed_dim(self) -> SpeakerInfo:
        # Embeddings are 192 floats in protocol 1; another size needs a protocol bump.
        if self.dim != EMBEDDING_DIM:
            raise ValueError(f"speaker dim must be {EMBEDDING_DIM} in protocol 1")
        return self


class AudioHelloData(WireModel):
    protocol: int = Field(ge=1)
    role: Literal["audio"] = "audio"
    device: DeviceInfo
    wake: WakeInfo
    stt: SttInfo
    tts: TtsInfo
    speaker: SpeakerInfo
    aec_verified: bool


class AudioHelloEvent(EventBase):
    """First event audio sends on a new connection."""

    type: Literal["hello"] = "hello"
    data: AudioHelloData


# ---------------------------------------------------------------------- wake --


class WakeData(WireModel):
    device: Name
    model: Name
    score: float = Field(ge=0.0, le=1.0)
    trigger: Literal["wake", "reply", "button"]
    preroll_ms: Millis
    t_wake: MonotonicNs


class WakeEvent(EventBase):
    """Opens a turn; audio has already played the ``wake`` earcon."""

    CANONICAL: ClassVar[bool] = True
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["wake"] = "wake"
    data: WakeData


# ------------------------------------------------------------------- speaker --


class SpeakerProvisionalData(WireModel):
    profile_id: ProfileId
    score: float
    margin: float
    stage: Literal["provisional"] = "provisional"
    audio_ms: Millis


class SpeakerProvisionalEvent(EventBase):
    """Provisional identity from the pre-roll; core only warms caches with it."""

    CANONICAL: ClassVar[bool] = True
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["speaker.provisional"] = "speaker.provisional"
    data: SpeakerProvisionalData


class SpeakerCandidate(WireModel):
    profile_id: ProfileId
    score: float


class SpeakerFinalData(WireModel):
    profile_id: ProfileId
    score: float
    margin: float
    stage: Literal["final"] = "final"
    anchor: bool
    audio_ms: Millis
    candidates: list[SpeakerCandidate] = Field(max_length=3)

    @model_validator(mode="after")
    def _top_three(self) -> SpeakerFinalData:
        # "candidates: [{profile_id, score}] (top 3)": best first, one row per profile.
        scores = [c.score for c in self.candidates]
        if scores != sorted(scores, reverse=True):
            raise ValueError("candidates must be sorted by score, best first")
        ids = [c.profile_id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidates must not repeat a profile_id")
        return self


class SpeakerFinalEvent(EventBase):
    """Final identity of the turn; always precedes ``stt.final``."""

    CANONICAL: ClassVar[bool] = True
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["speaker.final"] = "speaker.final"
    data: SpeakerFinalData


# ----------------------------------------------------------------------- stt --


class SttPartialData(WireModel):
    text: str
    stable: bool


class SttPartialEvent(EventBase):
    CANONICAL: ClassVar[bool] = True
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["stt.partial"] = "stt.partial"
    data: SttPartialData


class SttFinalData(WireModel):
    text: str
    locale: Locale
    speech_ms: Millis
    endpoint: Literal["silence", "max_len", "cancel"]
    empty: bool
    provider: Name
    t_speech_start: MonotonicNs
    t_eos: MonotonicNs
    t_final: MonotonicNs

    @model_validator(mode="after")
    def _consistent(self) -> SttFinalData:
        # "empty: true means no speech after the wake word", so there is no text.
        if self.empty and self.text.strip():
            raise ValueError("stt.final with empty=true must have blank text")
        if self.t_final < self.t_eos:
            raise ValueError("t_final must not precede t_eos")
        if not self.empty and self.t_eos < self.t_speech_start:
            raise ValueError("t_eos must not precede t_speech_start")
        return self


class SttFinalEvent(EventBase):
    CANONICAL: ClassVar[bool] = True
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["stt.final"] = "stt.final"
    data: SttFinalData


# ----------------------------------------------------------------------- pin --


class PinResultData(WireModel):
    """Outcome of ``capture_pin``. There is deliberately no field for digits.

    The catalog lists ``turn_id`` among the fields; it travels in the envelope
    (required for this event), not inside ``data``.
    """

    approval_id: Name
    outcome: Literal["ok", "mismatch", "timeout", "cancelled"]


class PinResultEvent(EventBase):
    TURN_SCOPE: ClassVar[TurnScope] = "required"
    type: Literal["pin.result"] = "pin.result"
    data: PinResultData


# -------------------------------------------------------------- speak.done --


class SpeakDoneData(WireModel):
    utterance_id: Name
    reason: Literal["finished", "cancelled", "error"]
    audio_ms: Millis
    t_first_audio: MonotonicNs
    t_last_audio: MonotonicNs


class SpeakDoneEvent(EventBase):
    """Playback of one utterance ended. Carries the turn_id when it has one (§4.3)."""

    TURN_SCOPE: ClassVar[TurnScope] = "optional"
    type: Literal["speak.done"] = "speak.done"
    data: SpeakDoneData


# -------------------------------------------------------------- enrollment --


class EnrollQuality(WireModel):
    snr_db: float
    clipping: float = Field(ge=0.0, le=1.0, description="fraction of clipped samples")
    speech_ratio: float = Field(ge=0.0, le=1.0)
    duration_ms: Millis


class EnrollClipData(WireModel):
    session_id: Name
    index: int = Field(ge=0)
    accepted: bool
    quality: EnrollQuality
    embedding: Embedding | None = None

    @model_validator(mode="after")
    def _embedding_iff_accepted(self) -> EnrollClipData:
        # "embedding present only if accepted"; an accepted clip without one is useless.
        if self.accepted != (self.embedding is not None):
            raise ValueError("embedding must be present exactly when the clip is accepted")
        return self


class EnrollClipEvent(EventBase):
    type: Literal["enroll.clip"] = "enroll.clip"
    data: EnrollClipData


# ------------------------------------------------------------- mic / health --


class MicStateData(WireModel):
    muted: bool
    source: Literal["hardware", "software"]


class MicStateEvent(EventBase):
    type: Literal["mic.state"] = "mic.state"
    data: MicStateData


class HealthData(WireModel):
    cpu_temp_c: float
    throttled: bool
    stt_rtf: float = Field(ge=0.0)
    playback_queue_ms: Millis
    models_loaded: bool


class HealthEvent(EventBase):
    type: Literal["health"] = "health"
    data: HealthData


class ErrorData(WireModel):
    code: Name
    message: str = Field(max_length=1024)
    fatal: bool


class ErrorEvent(EventBase):
    TURN_SCOPE: ClassVar[TurnScope] = "optional"
    type: Literal["error"] = "error"
    data: ErrorData


#: Every event audio may send, discriminated on ``type``.
type AudioEvent = Annotated[
    AudioHelloEvent
    | WakeEvent
    | SpeakerProvisionalEvent
    | SpeakerFinalEvent
    | SttPartialEvent
    | SttFinalEvent
    | PinResultEvent
    | SpeakDoneEvent
    | EnrollClipEvent
    | MicStateEvent
    | HealthEvent
    | ErrorEvent,
    Field(discriminator="type"),
]

AUDIO_EVENT_CLASSES: tuple[type[EventBase], ...] = (
    AudioHelloEvent,
    WakeEvent,
    SpeakerProvisionalEvent,
    SpeakerFinalEvent,
    SttPartialEvent,
    SttFinalEvent,
    PinResultEvent,
    SpeakDoneEvent,
    EnrollClipEvent,
    MicStateEvent,
    HealthEvent,
    ErrorEvent,
)

__all__ = [
    "AUDIO_EVENT_CLASSES",
    "AudioEvent",
    "AudioHelloData",
    "AudioHelloEvent",
    "DeviceInfo",
    "EnrollClipData",
    "EnrollClipEvent",
    "EnrollQuality",
    "ErrorData",
    "ErrorEvent",
    "HealthData",
    "HealthEvent",
    "MicStateData",
    "MicStateEvent",
    "PinResultData",
    "PinResultEvent",
    "SpeakDoneData",
    "SpeakDoneEvent",
    "SpeakerCandidate",
    "SpeakerFinalData",
    "SpeakerFinalEvent",
    "SpeakerInfo",
    "SpeakerProvisionalData",
    "SpeakerProvisionalEvent",
    "SttFinalData",
    "SttFinalEvent",
    "SttInfo",
    "SttPartialData",
    "SttPartialEvent",
    "TtsInfo",
    "WakeData",
    "WakeEvent",
    "WakeInfo",
]
