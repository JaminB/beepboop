"""Catalog completeness and the invariants ARCHITECTURE §4 states in prose."""

from __future__ import annotations

import typing
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from beepboop_audio.events import (
    AUDIO_EVENT_CLASSES,
    CORE_EVENT_CLASSES,
    EMBEDDING_DIM,
    CapturePinData,
    EnrollClipData,
    EnrollQuality,
    EnrollStartData,
    EventBase,
    HealthEvent,
    MuteData,
    PinResultData,
    ProfilesSyncData,
    SpeakData,
    SpeakerCandidate,
    SpeakerFinalData,
    SpeakerInfo,
    SpeakerProvisionalData,
    SttFinalData,
    VoiceSpec,
    WakeData,
    WakeEvent,
    new_ulid,
    protocol_schema,
)
from beepboop_audio.events.examples import (
    EXAMPLE_PIN_HASH,
    example_audio_events,
    example_core_events,
)

# The catalog exactly as ARCHITECTURE §4.2 lists it.
SPEC_AUDIO_TYPES = {
    "hello", "wake", "speaker.provisional", "speaker.final", "stt.partial", "stt.final",
    "pin.result", "speak.done", "enroll.clip", "mic.state", "health", "error",
}  # fmt: skip
SPEC_CORE_TYPES = {
    "hello", "speak", "cancel", "capture_pin", "earcon", "mute", "profiles.sync",
    "enroll.start", "enroll.stop",
}  # fmt: skip
SPEC_CANONICAL = {
    "wake", "speaker.provisional", "speaker.final", "stt.partial", "stt.final",
    "speak", "cancel", "capture_pin",
}  # fmt: skip

ALL_CLASSES = (*AUDIO_EVENT_CLASSES, *CORE_EVENT_CLASSES)


def _type_of(cls: type[EventBase]) -> str:
    default = cls.model_fields["type"].default
    assert isinstance(default, str)
    return default


def test_catalog_matches_spec() -> None:
    assert {_type_of(c) for c in AUDIO_EVENT_CLASSES} == SPEC_AUDIO_TYPES
    assert {_type_of(c) for c in CORE_EVENT_CLASSES} == SPEC_CORE_TYPES
    assert len(AUDIO_EVENT_CLASSES) == len(SPEC_AUDIO_TYPES)
    assert len(CORE_EVENT_CLASSES) == len(SPEC_CORE_TYPES)


def test_exactly_eight_canonical_events() -> None:
    assert {_type_of(c) for c in ALL_CLASSES if c.CANONICAL} == SPEC_CANONICAL


def test_canonical_events_are_turn_scoped() -> None:
    for cls in ALL_CLASSES:
        if cls.CANONICAL:
            assert cls.TURN_SCOPE == "required", cls.__name__


def test_examples_cover_every_class() -> None:
    assert [type(e) for e in example_audio_events()] == list(AUDIO_EVENT_CLASSES)
    assert [type(e) for e in example_core_events()] == list(CORE_EVENT_CLASSES)


# ------------------------------------------------------------ no audio bytes --

_BYTES_TYPES = (bytes, bytearray, memoryview)
_AUDIO_FIELD_NAMES = {"audio", "pcm", "wav", "samples", "waveform", "audio_data", "audio_b64"}


def _models_reachable(root: type[BaseModel]) -> set[type[BaseModel]]:
    seen: set[type[BaseModel]] = set()
    stack = [root]
    while stack:
        model = stack.pop()
        if model in seen:
            continue
        seen.add(model)
        for info in model.model_fields.values():
            stack.extend(_nested_models(info.annotation))
    return seen


def _flatten(annotation: Any) -> list[Any]:
    out = [annotation]
    for arg in typing.get_args(annotation):
        out.extend(_flatten(arg))
    return out


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    return [a for a in _flatten(annotation) if isinstance(a, type) and issubclass(a, BaseModel)]


def test_no_model_has_a_bytes_or_audio_field() -> None:
    """CLAUDE.md hard rule 4: no PCM ever crosses the socket."""
    models = set[type[BaseModel]]().union(*(_models_reachable(c) for c in ALL_CLASSES))
    assert len(models) > len(ALL_CLASSES)  # we really walked into the data models
    for model in models:
        for name, info in model.model_fields.items():
            leaves = _flatten(info.annotation)
            assert not any(isinstance(a, type) and issubclass(a, _BYTES_TYPES) for a in leaves), (
                f"{model.__name__}.{name} is a bytes field"
            )
            assert name not in _AUDIO_FIELD_NAMES, f"{model.__name__}.{name} looks like audio"


def test_json_schema_has_no_binary_content() -> None:
    text = str(protocol_schema())
    for marker in ("binary", "base64", "contentEncoding"):
        assert marker not in text


def test_control_plane_events_carry_no_transcript_field() -> None:
    """Only canonical events carry turn content (ARCHITECTURE §4.2)."""
    for cls in ALL_CLASSES:
        if cls.CANONICAL:
            continue
        for model in _models_reachable(cls):
            assert "text" not in model.model_fields, f"{model.__name__} has a text field"
            assert "transcript" not in model.model_fields


# ----------------------------------------------------------------- identity --


@pytest.mark.parametrize("bad", ["GUEST", "Guest", "gUeSt"])
def test_guest_must_be_lowercase_literal(bad: str) -> None:
    with pytest.raises(ValidationError, match="guest"):
        SpeakerProvisionalData(profile_id=bad, score=0.1, margin=0.0, audio_ms=1000)
    with pytest.raises(ValidationError, match="guest"):
        SpeakerCandidate(profile_id=bad, score=0.1)
    ok = SpeakerProvisionalData(profile_id="guest", score=0.1, margin=0.0, audio_ms=1000)
    assert ok.profile_id == "guest"


def test_profile_id_is_required_on_speaker_events() -> None:
    with pytest.raises(ValidationError):
        SpeakerProvisionalData.model_validate_json('{"score":0.1,"margin":0,"audio_ms":5}')
    with pytest.raises(ValidationError):
        SpeakerProvisionalData(profile_id="", score=0.1, margin=0.0, audio_ms=1000)


def _final(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile_id": "alex", "score": 0.8, "margin": 0.2, "anchor": True, "audio_ms": 2000,
        "candidates": [{"profile_id": "alex", "score": 0.8}, {"profile_id": "sam", "score": 0.5}],
    }  # fmt: skip
    return base | over


def test_speaker_final_candidates_top_three_sorted_unique() -> None:
    SpeakerFinalData.model_validate(_final(), strict=False)
    too_many = [{"profile_id": f"p{i}", "score": 0.9 - i / 10} for i in range(4)]
    unsorted = [{"profile_id": "a", "score": 0.1}, {"profile_id": "b", "score": 0.9}]
    repeated = [{"profile_id": "a", "score": 0.9}, {"profile_id": "a", "score": 0.1}]
    for candidates in (too_many, unsorted, repeated):
        with pytest.raises(ValidationError):
            SpeakerFinalData.model_validate(_final(candidates=candidates), strict=False)


def test_speaker_final_stage_is_fixed() -> None:
    with pytest.raises(ValidationError):
        SpeakerFinalData.model_validate(_final(stage="provisional"), strict=False)


# --------------------------------------------------------------- turn scope --


def _wake() -> WakeData:
    return WakeData(
        device="d", model="hey_beepboop", score=0.9, trigger="wake", preroll_ms=0, t_wake=1
    )


def test_turn_events_require_turn_id() -> None:
    with pytest.raises(ValidationError, match="turn_id"):
        WakeEvent(data=_wake())


def test_control_plane_events_refuse_turn_id() -> None:
    health = example_audio_events()[10]
    assert isinstance(health, HealthEvent)
    with pytest.raises(ValidationError, match="control-plane"):
        HealthEvent(turn_id=new_ulid(), data=health.data)


@pytest.mark.parametrize("bad", ["01JBX…", "not-a-ulid", "81JBX6Q8Y4H3K9ZR2M7V5T8WDA", ""])
def test_turn_id_must_be_a_ulid(bad: str) -> None:
    with pytest.raises(ValidationError):
        WakeEvent(turn_id=bad, data=_wake())


def test_new_ulid_shape_and_order() -> None:
    a, b = new_ulid(now_ms=1), new_ulid(now_ms=2)
    assert len(a) == 26
    assert a < b
    WakeEvent(turn_id=new_ulid(), data=_wake())
    with pytest.raises(ValueError, match="range"):
        new_ulid(now_ms=2**48)


def test_ts_is_rounded_to_milliseconds() -> None:
    event = WakeEvent(turn_id=new_ulid(), ts=1793657610.123456, data=_wake())
    assert event.ts == 1793657610.123


# ---------------------------------------------------------------------- PIN --


def _pin(**over: Any) -> CapturePinData:
    fields: dict[str, Any] = {"approval_id": "a1", "pin_hash": EXAMPLE_PIN_HASH} | over
    return CapturePinData(**fields)


def test_capture_pin_defaults_match_identity_and_trust() -> None:
    pin = _pin()
    assert (pin.min_len, pin.max_len, pin.timeout_ms) == (4, 8, 10_000)


@pytest.mark.parametrize(
    "over",
    [
        {"pin_hash": "1234"},
        {"pin_hash": "$argon2i$v=19$m=1,t=1,p=1$x$y"},
        {"min_len": 3},
        {"max_len": 9},
        {"min_len": 6, "max_len": 5},
        {"timeout_ms": 0},
    ],
)
def test_capture_pin_rejects_bad_fields(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _pin(**over)


def test_capture_pin_hash_not_in_repr() -> None:
    assert EXAMPLE_PIN_HASH not in repr(_pin())
    assert EXAMPLE_PIN_HASH not in str(_pin())


def test_pin_result_cannot_carry_digits() -> None:
    assert set(PinResultData.model_fields) == {"approval_id", "outcome"}
    with pytest.raises(ValidationError):
        PinResultData.model_validate({"approval_id": "a", "outcome": "1234"})


# ------------------------------------------------------------------ others --


def _quality() -> EnrollQuality:
    return EnrollQuality(snr_db=20.0, clipping=0.0, speech_ratio=0.9, duration_ms=1200)


def test_enroll_clip_embedding_iff_accepted() -> None:
    emb = [0.0] * EMBEDDING_DIM
    EnrollClipData(session_id="s", index=0, accepted=True, quality=_quality(), embedding=emb)
    EnrollClipData(session_id="s", index=1, accepted=False, quality=_quality())
    with pytest.raises(ValidationError):
        EnrollClipData(session_id="s", index=0, accepted=True, quality=_quality())
    with pytest.raises(ValidationError):
        EnrollClipData(session_id="s", index=0, accepted=False, quality=_quality(), embedding=emb)
    with pytest.raises(ValidationError):
        EnrollClipData(
            session_id="s", index=0, accepted=True, quality=_quality(), embedding=emb[:-1]
        )


def test_speaker_dim_is_fixed_in_protocol_1() -> None:
    with pytest.raises(ValidationError):
        SpeakerInfo(model_id="m", model_sha="s", dim=256)


def test_profiles_sync_centroid_length_and_unique_profiles() -> None:
    sync = example_core_events()[6].model_dump()["data"]
    bad_dim = {**sync, "profiles": [{**sync["profiles"][0], "centroids": [[0.0] * 10]}]}
    with pytest.raises(ValidationError):
        ProfilesSyncData.model_validate(bad_dim, strict=False)
    dup = {**sync, "profiles": sync["profiles"] * 2}
    with pytest.raises(ValidationError, match="repeat"):
        ProfilesSyncData.model_validate(dup, strict=False)


def _stt(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "text": "", "locale": "en-US", "speech_ms": 0, "endpoint": "silence", "empty": True,
        "provider": "p", "t_speech_start": 0, "t_eos": 10, "t_final": 20,
    }  # fmt: skip
    return base | over


def test_stt_final_consistency() -> None:
    SttFinalData.model_validate(_stt())
    with pytest.raises(ValidationError, match="blank"):
        SttFinalData.model_validate(_stt(text="hello"))
    with pytest.raises(ValidationError):
        SttFinalData.model_validate(_stt(t_final=5))
    with pytest.raises(ValidationError):
        SttFinalData.model_validate(_stt(empty=False, text="hi", t_speech_start=50))


def _speak(**over: Any) -> SpeakData:
    fields: dict[str, Any] = {
        "utterance_id": "u", "seq": 0, "last": True, "voice": VoiceSpec(voice_id="v", rate=1.0),
        "locale": "en-US", "listen_after": False,
    } | over  # fmt: skip
    return SpeakData(**fields)


def test_speak_needs_text_or_clip() -> None:
    assert _speak(text="Hi.").text == "Hi."
    assert _speak(clip="approval.prompt").clip == "approval.prompt"
    assert _speak(text="Done.", clip="done").clip == "done"
    for over in ({}, {"text": "   "}, {"clip": "Not A Key"}):
        with pytest.raises(ValidationError):
            _speak(**over)


def test_mute_until_only_when_muting() -> None:
    MuteData(on=True, until=1.0)
    MuteData(on=False)
    with pytest.raises(ValidationError):
        MuteData(on=False, until=1.0)


def test_guest_cannot_be_enrolled() -> None:
    with pytest.raises(ValidationError, match="guest"):
        EnrollStartData(session_id="s", profile_id="guest", mode="wake", prompts=[])


def test_models_are_frozen() -> None:
    event = example_audio_events()[1]
    with pytest.raises(ValidationError):
        event.seq = 5  # frozen model
