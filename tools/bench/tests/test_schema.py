"""Result-file schema: stage names, deterministic JSON Schema, round trip, no audio."""

from __future__ import annotations

import json

from beepboop_bench.schema import (
    SCENARIOS,
    STAGE_NAMES,
    BenchResult,
    E2ESection,
    Sections,
    Turn,
    json_schema,
    json_schema_text,
)
from beepboop_bench.testing import result

OPERATIONS_STAGES = (
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


def test_stage_names_are_the_operations_table() -> None:
    assert STAGE_NAMES == OPERATIONS_STAGES
    assert SCENARIOS == ("fast_path", "command_1s", "no_tool", "tool")


def test_schema_is_deterministic_and_versioned() -> None:
    a = json_schema_text()
    b = json_schema_text()
    assert a == b
    schema = json_schema()
    props = schema["properties"]
    assert isinstance(props, dict)
    assert props["schema_version"]["const"] == 1
    assert list(schema.keys()) == sorted(schema.keys())


def test_turn_round_trip_keeps_none_for_unmeasured_stages() -> None:
    turn = Turn(
        turn_id="t0001",
        clip_id="c1",
        scenario="tool",
        started_at_s=0.5,
        stages_ms={"endpoint": 250.0, "llm_ttft": None},
    )
    res = result(sections=Sections(e2e=E2ESection(status="measured", turns=[turn])))
    text = res.to_json()
    back = BenchResult.model_validate_json(text)
    assert back.sections.e2e is not None
    assert back.sections.e2e.turns[0].stages_ms == {"endpoint": 250.0, "llm_ttft": None}
    assert json.loads(text) == json.loads(back.to_json())


def test_no_audio_fields_anywhere() -> None:
    """Hard rule 4: no bytes or PCM-like fields in anything we write to disk."""
    text = json_schema_text().lower()
    for banned in ('"format": "binary"', "pcm", "audio_bytes", "audio_ref", "audio_enc"):
        assert banned not in text, banned


def test_extra_keys_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="extra"):
        Turn(turn_id="t", clip_id="c", scenario="tool", started_at_s=0.0, pcm=b"x")  # type: ignore[call-arg]
