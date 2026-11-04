"""JSON-lines framing: encode/decode rules of ARCHITECTURE §4.1 and the line framer."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

from beepboop_protocol import (
    EventValidationError,
    FrameError,
    LineFramer,
    SpeakerFinalEvent,
    SttPartialData,
    SttPartialEvent,
    WakeEvent,
    codec,
    decode_audio_event,
    decode_core_event,
    encode,
)
from beepboop_protocol.examples import (
    EXAMPLE_PIN_HASH,
    EXAMPLE_PROFILE_ALEX,
    EXAMPLE_PROFILE_SAM,
    EXAMPLE_TURN_ID,
    example_audio_events,
    example_core_events,
)

ARCHITECTURE_MD = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"


def test_encode_is_one_terminated_line() -> None:
    text = 'line one\nline "two"\r\n' + chr(0x2028) + " caf\xe9 \U0001f600"
    line = encode(
        SttPartialEvent(turn_id=EXAMPLE_TURN_ID, data=SttPartialData(text=text, stable=False))
    )
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    decoded = decode_audio_event(line)
    assert isinstance(decoded, SttPartialEvent)
    assert decoded.data.text == text


def test_absent_fields_are_omitted_not_null() -> None:
    health = json.loads(encode(example_audio_events()[10]))
    assert "turn_id" not in health
    speak = json.loads(encode(example_core_events()[1]))
    assert "text" not in speak["data"]
    assert set(speak) == {"type", "seq", "ts", "turn_id", "data"}


def test_spec_example_decodes() -> None:
    """Every line of the ARCHITECTURE §4.3 example is a valid event."""
    block = ARCHITECTURE_MD.read_text(encoding="utf-8").split("```jsonl", 1)[1].split("```", 1)[0]
    lines = [ln for ln in block.strip().splitlines() if ln.strip()]
    assert len(lines) == 8
    for raw in lines:
        # The doc abbreviates ULIDs: "01JBX…" is a turn_id, "alex"/"sam" are profile ids.
        arrow, payload = raw[0], raw[2:].replace("01JBX…", EXAMPLE_TURN_ID)
        payload = payload.replace('"alex"', f'"{EXAMPLE_PROFILE_ALEX}"')
        payload = payload.replace('"sam"', f'"{EXAMPLE_PROFILE_SAM}"')
        decode = decode_audio_event if arrow == "←" else decode_core_event
        event = decode(payload)
        assert event is not None, payload
        assert json.loads(encode(event))["data"] == json.loads(payload)["data"]


def test_unknown_type_is_ignored_and_logged_at_debug() -> None:
    with capture_logs() as logs:
        assert decode_audio_event(b'{"type":"satellite.hello","seq":1,"ts":1,"data":{}}') is None
    assert logs == [
        {"event": "event.unknown_type", "type": "satellite.hello", "log_level": "debug"}
    ]


def test_other_directions_events_are_unknown() -> None:
    speak = encode(example_core_events()[1])
    wake = encode(example_audio_events()[1])
    assert decode_audio_event(speak) is None
    assert decode_core_event(wake) is None


def test_unknown_fields_are_ignored() -> None:
    original = example_audio_events()[3]
    raw = json.loads(encode(original))
    raw["future_envelope_field"] = 1
    raw["data"]["future_field"] = {"x": [1, 2]}
    event = decode_audio_event(json.dumps(raw))
    assert isinstance(event, SpeakerFinalEvent)
    assert event == original


@pytest.mark.parametrize(
    "line",
    [b"not json", b"[1, 2]", b'"wake"', b'{"seq": 1}', b'{"type": 5}', b"\xff\xfe"],
)
def test_malformed_lines_raise_frame_error(line: bytes) -> None:
    with pytest.raises(FrameError):
        decode_audio_event(line)


def test_invalid_known_event_raises_without_echoing_values() -> None:
    raw = json.loads(encode(example_core_events()[3]))  # capture_pin
    raw["data"]["min_len"] = "four"
    with pytest.raises(EventValidationError) as excinfo:
        decode_core_event(json.dumps(raw))
    assert excinfo.value.event_type == "capture_pin"
    assert "data.min_len" in str(excinfo.value)
    assert EXAMPLE_PIN_HASH not in str(excinfo.value)
    assert "four" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("seq", "5"), ("ts", "now"), ("seq", 1.5), ("seq", -1)],
)
def test_envelope_is_strict(field: str, value: object) -> None:
    raw = json.loads(encode(example_audio_events()[1]))
    raw[field] = value
    with pytest.raises(EventValidationError):
        decode_audio_event(json.dumps(raw))


def test_nan_and_strings_for_numbers_are_rejected() -> None:
    line = encode(example_audio_events()[1]).decode()
    with pytest.raises(EventValidationError):
        decode_audio_event(line.replace('"score":0.97', '"score":NaN'))
    with pytest.raises(EventValidationError):
        decode_audio_event(line.replace('"score":0.97', '"score":"0.97"'))
    with pytest.raises(EventValidationError):
        decode_audio_event(line.replace('"preroll_ms":1040', '"preroll_ms":true'))


def test_encode_refuses_oversized_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codec, "MAX_LINE_BYTES", 64)
    with pytest.raises(FrameError, match="limit"):
        encode(example_core_events()[6])


def test_decode_roundtrips_every_example() -> None:
    for event in example_audio_events():
        assert decode_audio_event(encode(event)) == event
    for event in example_core_events():
        assert decode_core_event(encode(event)) == event


def test_wake_type_discriminates() -> None:
    assert isinstance(decode_audio_event(encode(example_audio_events()[1])), WakeEvent)


# ------------------------------------------------------------- line framer --


def test_framer_splits_and_joins_chunks() -> None:
    framer = LineFramer()
    assert framer.feed(b'{"a":1}\n{"b"') == [b'{"a":1}']
    assert framer.feed(b":2}\n\n  \n{") == [b'{"b":2}']
    assert framer.feed(b"}\r\n") == [b"{}"]


def test_framer_drops_oversized_line_and_resyncs() -> None:
    framer = LineFramer(max_line_bytes=8)
    with capture_logs() as logs:
        out = framer.feed(b"ok\n" + b"x" * 5)
        out += framer.feed(b"y" * 10)
        out += framer.feed(b"z\nfine\n")
    assert out == [b"ok", b"fine"]
    assert framer.oversized == 1
    assert [entry["event"] for entry in logs] == ["event.line_too_long"]


def test_framer_accepts_line_of_exactly_max() -> None:
    framer = LineFramer(max_line_bytes=4)
    assert framer.feed(b"abcd\nabcde\nab\n") == [b"abcd", b"ab"]
    assert framer.oversized == 1


def test_framer_drops_unterminated_tail_at_eof() -> None:
    framer = LineFramer()
    assert framer.feed(b"partial") == []
    with capture_logs() as logs:
        framer.eof()
    assert logs[0]["event"] == "event.unterminated_line"


@given(
    lines=st.lists(st.binary(min_size=1, max_size=40).map(lambda b: b.replace(b"\n", b"_"))),
    cuts=st.lists(st.integers(min_value=0, max_value=2000), max_size=10),
)
def test_framer_is_chunking_invariant(lines: list[bytes], cuts: list[int]) -> None:
    lines = [ln for ln in lines if ln.strip() and ln.rstrip(b"\r") == ln]
    stream = b"".join(ln + b"\n" for ln in lines)
    points = sorted({c % (len(stream) + 1) for c in cuts} | {0, len(stream)})
    framer = LineFramer()
    out: list[bytes] = []
    for start, end in itertools.pairwise(points):
        out += framer.feed(stream[start:end])
    assert out == lines
