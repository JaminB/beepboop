"""Property-based round-trip: for every event, ``decode(encode(e)) == e``.

Hypothesis generates each event's fields within the catalog's constraints
(including awkward text: quotes, newlines, non-BMP characters) and checks the
event survives the JSON-lines codec unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from beepboop_protocol import (
    AUDIO_EVENT_CLASSES,
    CORE_EVENT_CLASSES,
    MAX_EMBEDDING_DIM,
    PROTOCOL_MAJOR,
    AudioHelloData,
    AudioHelloEvent,
    Calibration,
    CancelData,
    CancelEvent,
    CapturePinData,
    CapturePinEvent,
    CoreHelloData,
    CoreHelloEvent,
    DeviceInfo,
    EarconData,
    EarconEvent,
    EnrollClipData,
    EnrollClipEvent,
    EnrollQuality,
    EnrollStartData,
    EnrollStartEvent,
    EnrollStopData,
    EnrollStopEvent,
    ErrorData,
    ErrorEvent,
    EventBase,
    HealthData,
    HealthEvent,
    MicStateData,
    MicStateEvent,
    MuteData,
    MuteEvent,
    PinResultData,
    PinResultEvent,
    ProfilesSyncData,
    ProfilesSyncEvent,
    ProfileVoiceprint,
    SpeakData,
    SpeakDoneData,
    SpeakDoneEvent,
    SpeakerCandidate,
    SpeakerFinalData,
    SpeakerFinalEvent,
    SpeakerInfo,
    SpeakerProvisionalData,
    SpeakerProvisionalEvent,
    SpeakEvent,
    SttFinalData,
    SttFinalEvent,
    SttInfo,
    SttPartialData,
    SttPartialEvent,
    TierThresholds,
    TtsInfo,
    VoiceSpec,
    WakeData,
    WakeEvent,
    WakeInfo,
    decode_audio_event,
    decode_core_event,
    encode,
    new_ulid,
)
from beepboop_protocol.examples import EXAMPLE_PIN_HASH

# ------------------------------------------------------------- primitives --

names = st.text(min_size=1, max_size=20)
texts = st.text(max_size=80)
finite = st.floats(allow_nan=False, allow_infinity=False, width=64)
unit = st.floats(min_value=0.0, max_value=1.0)
millis = st.integers(min_value=0, max_value=10**9)
mono = st.integers(min_value=0, max_value=2**63 - 1)
locales = st.sampled_from(["en-US", "en-GB", "de-DE", "fr"])
turn_ids = st.integers(min_value=0, max_value=2**48 - 1).map(lambda ms: new_ulid(now_ms=ms))
timestamps = st.integers(min_value=0, max_value=4_000_000_000_000).map(lambda ms: ms / 1000)
seqs = st.integers(min_value=0, max_value=2**53)
profile_ids = st.one_of(st.just("guest"), turn_ids)
# One fixed length, so every centroid in a drawn profiles.sync agrees; the
# per-connection length check is exercised in test_channel.py.
embeddings = st.lists(finite, min_size=8, max_size=8)
voices = st.builds(VoiceSpec, voice_id=names, rate=st.floats(min_value=0.1, max_value=4.0))


def event(cls: type[EventBase], data: st.SearchStrategy[Any], turn: str) -> st.SearchStrategy[Any]:
    """Envelope strategy; ``turn`` is the event's TURN_SCOPE."""
    turn_strategy: st.SearchStrategy[str | None]
    if turn == "required":
        turn_strategy = turn_ids
    elif turn == "optional":
        turn_strategy = st.none() | turn_ids
    else:
        turn_strategy = st.none()
    return st.builds(cls, seq=seqs, ts=timestamps, turn_id=turn_strategy, data=data)


# ------------------------------------------------------- audio -> core data --

audio_hello = st.builds(
    AudioHelloData,
    protocol=st.just(PROTOCOL_MAJOR),
    device=st.builds(DeviceInfo, id=names, name=texts.filter(lambda t: len(t) <= 128)),
    wake=st.builds(WakeInfo, provider=names, models=st.lists(names, max_size=3)),
    stt=st.builds(SttInfo, provider=names, locale=locales),
    tts=st.builds(TtsInfo, provider=names, voices=st.lists(names, max_size=3)),
    speaker=st.builds(
        SpeakerInfo, model_id=names, model_sha=names, dim=st.integers(1, MAX_EMBEDDING_DIM)
    ),
    aec_verified=st.booleans(),
)
wake = st.builds(
    WakeData,
    device=names,
    model=names,
    score=unit,
    trigger=st.sampled_from(["wake", "reply", "button"]),
    preroll_ms=millis,
    t_wake=mono,
)
provisional = st.builds(
    SpeakerProvisionalData, profile_id=profile_ids, score=finite, margin=finite, audio_ms=millis
)


@st.composite
def speaker_final(draw: st.DrawFn) -> SpeakerFinalData:
    ids = draw(st.lists(profile_ids, max_size=3, unique=True))
    scores = sorted(draw(st.lists(finite, min_size=len(ids), max_size=len(ids))), reverse=True)
    return SpeakerFinalData(
        profile_id=draw(profile_ids),
        score=draw(finite),
        margin=draw(finite),
        anchor=draw(st.booleans()),
        audio_ms=draw(millis),
        candidates=[
            SpeakerCandidate(profile_id=p, score=s) for p, s in zip(ids, scores, strict=True)
        ],
    )


@st.composite
def stt_final(draw: st.DrawFn) -> SttFinalData:
    empty = draw(st.booleans())
    t_speech_start, t_eos, t_final = sorted(draw(st.lists(mono, min_size=3, max_size=3)))
    return SttFinalData(
        text="" if empty else draw(texts),
        locale=draw(locales),
        speech_ms=draw(millis),
        endpoint=draw(st.sampled_from(["silence", "max_len", "cancel"])),
        empty=empty,
        provider=draw(names),
        t_speech_start=t_speech_start,
        t_eos=t_eos,
        t_final=t_final,
    )


@st.composite
def enroll_clip(draw: st.DrawFn) -> EnrollClipData:
    accepted = draw(st.booleans())
    return EnrollClipData(
        session_id=draw(names),
        index=draw(st.integers(min_value=0, max_value=100)),
        accepted=accepted,
        quality=EnrollQuality(
            snr_db=draw(finite),
            clipping=draw(unit),
            speech_ratio=draw(unit),
            duration_ms=draw(millis),
        ),
        embedding=draw(embeddings) if accepted else None,
    )


AUDIO_STRATEGIES: dict[type[EventBase], st.SearchStrategy[Any]] = {
    AudioHelloEvent: event(AudioHelloEvent, audio_hello, "forbidden"),
    WakeEvent: event(WakeEvent, wake, "required"),
    SpeakerProvisionalEvent: event(SpeakerProvisionalEvent, provisional, "required"),
    SpeakerFinalEvent: event(SpeakerFinalEvent, speaker_final(), "required"),
    SttPartialEvent: event(
        SttPartialEvent, st.builds(SttPartialData, text=texts, stable=st.booleans()), "required"
    ),
    SttFinalEvent: event(SttFinalEvent, stt_final(), "required"),
    PinResultEvent: event(
        PinResultEvent,
        st.builds(
            PinResultData,
            approval_id=names,
            outcome=st.sampled_from(["ok", "mismatch", "timeout", "cancelled"]),
        ),
        "required",
    ),
    SpeakDoneEvent: event(
        SpeakDoneEvent,
        st.builds(
            SpeakDoneData,
            utterance_id=names,
            reason=st.sampled_from(["finished", "cancelled", "error"]),
            audio_ms=millis,
            t_first_audio=mono,
            t_last_audio=mono,
        ),
        "optional",
    ),
    EnrollClipEvent: event(EnrollClipEvent, enroll_clip(), "forbidden"),
    MicStateEvent: event(
        MicStateEvent,
        st.builds(
            MicStateData, muted=st.booleans(), source=st.sampled_from(["hardware", "software"])
        ),
        "forbidden",
    ),
    HealthEvent: event(
        HealthEvent,
        st.builds(
            HealthData,
            cpu_temp_c=finite,
            throttled=st.booleans(),
            stt_rtf=st.floats(min_value=0.0, max_value=100.0),
            playback_queue_ms=millis,
            models_loaded=st.booleans(),
            aec_verified=st.booleans(),
        ),
        "forbidden",
    ),
    ErrorEvent: event(
        ErrorEvent,
        st.builds(ErrorData, code=names, message=texts, fatal=st.booleans()),
        "optional",
    ),
}

# ------------------------------------------------------- core -> audio data --


@st.composite
def speak(draw: st.DrawFn) -> SpeakData:
    text = draw(st.none() | texts.filter(lambda t: t.strip() != ""))
    clip = draw(st.from_regex(r"[a-z0-9_.]{1,64}", fullmatch=True) if text is None else st.none())
    return SpeakData(
        utterance_id=draw(names),
        seq=draw(st.integers(min_value=0, max_value=1000)),
        last=draw(st.booleans()),
        text=text,
        clip=clip,
        voice=draw(voices),
        locale=draw(locales),
        listen_after=draw(st.booleans()),
    )


@st.composite
def capture_pin(draw: st.DrawFn) -> CapturePinData:
    lo, hi = sorted(draw(st.lists(st.integers(min_value=4, max_value=8), min_size=2, max_size=2)))
    return CapturePinData(
        approval_id=draw(names),
        pin_hash=EXAMPLE_PIN_HASH,
        min_len=lo,
        max_len=hi,
        timeout_ms=draw(st.integers(min_value=1, max_value=60_000)),
        prompt=draw(st.none() | texts.filter(bool)),
    )


@st.composite
def mute(draw: st.DrawFn) -> MuteData:
    on = draw(st.booleans())
    until = draw(st.none() | st.floats(min_value=0.0, max_value=4e9)) if on else None
    return MuteData(on=on, until=until)


thresholds = st.builds(TierThresholds, act=finite, personal=finite)
voiceprint = st.builds(
    ProfileVoiceprint,
    profile_id=profile_ids,
    centroids=st.lists(embeddings, max_size=2),
    calib=st.builds(Calibration, tau=thresholds, margin=thresholds),
    hotwords=st.lists(texts, max_size=3),
    voice=voices,
    locale=locales,
)
profiles_sync = st.builds(
    ProfilesSyncData,
    profiles=st.lists(voiceprint, max_size=3, unique_by=lambda p: p.profile_id),
    cohort_sha=names,
    model_id=names,
)
non_guest = profile_ids.filter(lambda p: p != "guest")

CORE_STRATEGIES: dict[type[EventBase], st.SearchStrategy[Any]] = {
    CoreHelloEvent: event(
        CoreHelloEvent, st.builds(CoreHelloData, protocol=st.just(PROTOCOL_MAJOR)), "forbidden"
    ),
    SpeakEvent: event(SpeakEvent, speak(), "required"),
    CancelEvent: event(
        CancelEvent,
        st.builds(CancelData, reason=st.sampled_from(["barge_in", "superseded", "error", "user"])),
        "required",
    ),
    CapturePinEvent: event(CapturePinEvent, capture_pin(), "required"),
    EarconEvent: event(
        EarconEvent,
        st.builds(EarconData, name=st.sampled_from(["wake", "ack", "done", "too_long"])),
        "optional",
    ),
    MuteEvent: event(MuteEvent, mute(), "forbidden"),
    ProfilesSyncEvent: event(ProfilesSyncEvent, profiles_sync, "forbidden"),
    EnrollStartEvent: event(
        EnrollStartEvent,
        st.builds(
            EnrollStartData,
            session_id=names,
            profile_id=non_guest,
            mode=st.sampled_from(["wake", "free"]),
            prompts=st.lists(texts, max_size=3),
        ),
        "forbidden",
    ),
    EnrollStopEvent: event(
        EnrollStopEvent, st.builds(EnrollStopData, session_id=names), "forbidden"
    ),
}


def test_every_event_has_a_strategy() -> None:
    assert set(AUDIO_STRATEGIES) == set(AUDIO_EVENT_CLASSES)
    assert set(CORE_STRATEGIES) == set(CORE_EVENT_CLASSES)


@pytest.mark.parametrize("cls", AUDIO_EVENT_CLASSES, ids=lambda c: c.__name__)
@settings(max_examples=40, deadline=None)
@given(data=st.data())
def test_audio_events_roundtrip(cls: type[EventBase], data: st.DataObject) -> None:
    evt = data.draw(AUDIO_STRATEGIES[cls])
    line = encode(evt)
    assert line.endswith(b"}\n")
    assert line.count(b"\n") == 1
    assert decode_audio_event(line) == evt


@pytest.mark.parametrize("cls", CORE_EVENT_CLASSES, ids=lambda c: c.__name__)
@settings(max_examples=40, deadline=None)
@given(data=st.data())
def test_core_events_roundtrip(cls: type[EventBase], data: st.DataObject) -> None:
    evt = data.draw(CORE_STRATEGIES[cls])
    line = encode(evt)
    assert line.endswith(b"}\n")
    assert line.count(b"\n") == 1
    assert decode_core_event(line) == evt
