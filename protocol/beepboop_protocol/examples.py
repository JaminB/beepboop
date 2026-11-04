"""One valid example of every event, built from the ARCHITECTURE §4.3 values.

For tests, the bench and docs: ``example_audio_hello()`` is what the bench
hands to :func:`~.fake.open_fake_session`. These are illustrative values,
not measurements.
"""

from __future__ import annotations

from beepboop_protocol.audio_events import (
    AudioEvent,
    AudioHelloData,
    AudioHelloEvent,
    DeviceInfo,
    EnrollClipData,
    EnrollClipEvent,
    EnrollQuality,
    ErrorData,
    ErrorEvent,
    HealthData,
    HealthEvent,
    MicStateData,
    MicStateEvent,
    PinResultData,
    PinResultEvent,
    SpeakDoneData,
    SpeakDoneEvent,
    SpeakerCandidate,
    SpeakerFinalData,
    SpeakerFinalEvent,
    SpeakerInfo,
    SpeakerProvisionalData,
    SpeakerProvisionalEvent,
    SttFinalData,
    SttFinalEvent,
    SttInfo,
    SttPartialData,
    SttPartialEvent,
    TtsInfo,
    WakeData,
    WakeEvent,
    WakeInfo,
)
from beepboop_protocol.core_events import (
    Calibration,
    CancelData,
    CancelEvent,
    CapturePinData,
    CapturePinEvent,
    CoreEvent,
    CoreHelloData,
    CoreHelloEvent,
    EarconData,
    EarconEvent,
    EnrollStartData,
    EnrollStartEvent,
    EnrollStopData,
    EnrollStopEvent,
    MuteData,
    MuteEvent,
    ProfilesSyncData,
    ProfilesSyncEvent,
    ProfileVoiceprint,
    SpeakData,
    SpeakEvent,
    TierThresholds,
    VoiceSpec,
)
from beepboop_protocol.envelope import DEFAULT_EMBEDDING_DIM, PROTOCOL_MAJOR

#: A fixed, valid ULID for examples.
EXAMPLE_TURN_ID = "01JBX6Q8Y4H3K9ZR2M7V5T8WDA"
#: A syntactically valid argon2id PHC string (not the hash of any real PIN).
EXAMPLE_PIN_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"

_VOICE = VoiceSpec(voice_id="en_US-lessac-low", rate=1.0)
#: Example profile ids (ULIDs; ARCHITECTURE §4.3 abbreviates them to "alex" and "sam").
EXAMPLE_PROFILE_ALEX = "01JBW2K3M4N5P6Q7R8S9T0VWXY"
EXAMPLE_PROFILE_SAM = "01JBW2K3M4N5P6Q7R8S9T0VWXZ"

_EMBEDDING = [0.01 * (i % 7) for i in range(DEFAULT_EMBEDDING_DIM)]


def example_audio_hello() -> AudioHelloEvent:
    return AudioHelloEvent(
        data=AudioHelloData(
            protocol=PROTOCOL_MAJOR,
            device=DeviceInfo(id="living-room", name="Living room"),
            wake=WakeInfo(provider="microwakeword", models=["hey_beepboop"]),
            stt=SttInfo(provider="sherpa-zipformer", locale="en-US"),
            tts=TtsInfo(provider="sherpa-piper", voices=["en_US-lessac-low"]),
            speaker=SpeakerInfo(model_id="campp-en", model_sha="0" * 64, dim=DEFAULT_EMBEDDING_DIM),
            aec_verified=False,
        )
    )


def example_core_hello() -> CoreHelloEvent:
    return CoreHelloEvent(data=CoreHelloData(protocol=PROTOCOL_MAJOR))


def example_audio_events(turn_id: str = EXAMPLE_TURN_ID) -> list[AudioEvent]:
    """One instance of every audio → core event, in catalog order."""
    return [
        example_audio_hello(),
        WakeEvent(
            turn_id=turn_id,
            data=WakeData(
                device="living-room",
                model="hey_beepboop",
                score=0.97,
                trigger="wake",
                preroll_ms=1040,
                t_wake=812331000000,
            ),
        ),
        SpeakerProvisionalEvent(
            turn_id=turn_id,
            data=SpeakerProvisionalData(
                profile_id=EXAMPLE_PROFILE_ALEX, score=0.74, margin=0.19, audio_ms=1040
            ),
        ),
        SpeakerFinalEvent(
            turn_id=turn_id,
            data=SpeakerFinalData(
                profile_id=EXAMPLE_PROFILE_ALEX,
                score=0.83,
                margin=0.27,
                anchor=True,
                audio_ms=2990,
                candidates=[
                    SpeakerCandidate(profile_id=EXAMPLE_PROFILE_ALEX, score=0.83),
                    SpeakerCandidate(profile_id=EXAMPLE_PROFILE_SAM, score=0.56),
                ],
            ),
        ),
        SttPartialEvent(turn_id=turn_id, data=SttPartialData(text="turn off the", stable=True)),
        SttFinalEvent(
            turn_id=turn_id,
            data=SttFinalData(
                text="turn off the kitchen lights",
                locale="en-US",
                speech_ms=1950,
                endpoint="silence",
                empty=False,
                provider="sherpa-zipformer",
                t_speech_start=812332100000,
                t_eos=812333810000,
                t_final=812333890000,
            ),
        ),
        PinResultEvent(turn_id=turn_id, data=PinResultData(approval_id="appr1", outcome="ok")),
        SpeakDoneEvent(
            turn_id=turn_id,
            data=SpeakDoneData(
                utterance_id="u1",
                reason="finished",
                audio_ms=350,
                t_first_audio=812333940000,
                t_last_audio=812334290000,
            ),
        ),
        EnrollClipEvent(
            data=EnrollClipData(
                session_id="enr1",
                index=0,
                accepted=True,
                quality=EnrollQuality(
                    snr_db=21.5, clipping=0.0, speech_ratio=0.8, duration_ms=1500
                ),
                embedding=_EMBEDDING,
            )
        ),
        MicStateEvent(data=MicStateData(muted=True, source="hardware")),
        HealthEvent(
            data=HealthData(
                cpu_temp_c=61.2,
                throttled=False,
                stt_rtf=0.31,
                playback_queue_ms=0,
                models_loaded=True,
                aec_verified=False,
            )
        ),
        ErrorEvent(data=ErrorData(code="alsa_xrun", message="capture overrun", fatal=False)),
    ]


def example_core_events(turn_id: str = EXAMPLE_TURN_ID) -> list[CoreEvent]:
    """One instance of every core → audio event, in catalog order."""
    return [
        example_core_hello(),
        SpeakEvent(
            turn_id=turn_id,
            data=SpeakData(
                utterance_id="u1",
                seq=0,
                last=True,
                clip="done",
                voice=_VOICE,
                locale="en-US",
                listen_after=False,
            ),
        ),
        CancelEvent(turn_id=turn_id, data=CancelData(reason="barge_in")),
        CapturePinEvent(
            turn_id=turn_id,
            data=CapturePinData(approval_id="appr1", pin_hash=EXAMPLE_PIN_HASH),
        ),
        EarconEvent(turn_id=turn_id, data=EarconData(name="ack")),
        MuteEvent(data=MuteData(on=True, until=1793661210.0)),
        ProfilesSyncEvent(
            data=ProfilesSyncData(
                profiles=[
                    ProfileVoiceprint(
                        profile_id=EXAMPLE_PROFILE_ALEX,
                        centroids=[_EMBEDDING, _EMBEDDING],
                        calib=Calibration(
                            tau=TierThresholds(act=0.55, personal=0.7),
                            margin=TierThresholds(act=0.1, personal=0.2),
                        ),
                        hotwords=["Mochi"],
                        voice=_VOICE,
                        locale="en-US",
                    )
                ],
                cohort_sha="c" * 64,
                model_id="campp-en",
            )
        ),
        EnrollStartEvent(
            data=EnrollStartData(
                session_id="enr1",
                profile_id=EXAMPLE_PROFILE_ALEX,
                mode="wake",
                prompts=["Hey beepboop"],
            )
        ),
        EnrollStopEvent(data=EnrollStopData(session_id="enr1")),
    ]


__all__ = [
    "EXAMPLE_PIN_HASH",
    "EXAMPLE_PROFILE_ALEX",
    "EXAMPLE_PROFILE_SAM",
    "EXAMPLE_TURN_ID",
    "example_audio_events",
    "example_audio_hello",
    "example_core_events",
    "example_core_hello",
]
