"""EventChannel behaviour and the hello handshake, over the in-process fake."""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import capture_logs

from beepboop_audio.events import (
    CancelData,
    CancelEvent,
    ConnectionClosedError,
    CoreHelloData,
    CoreHelloEvent,
    ErrorEvent,
    HandshakeError,
    ProtocolMismatchError,
    SttPartialData,
    SttPartialEvent,
    audio_channel,
    audio_handshake,
    core_channel,
    core_handshake,
    encode,
    fake_pair,
    open_fake_session,
)
from beepboop_audio.events.examples import (
    EXAMPLE_TURN_ID,
    example_audio_events,
    example_audio_hello,
    example_core_events,
)
from beepboop_audio.events.fake import fake_duplex_pair


def _partial(text: str) -> SttPartialEvent:
    return SttPartialEvent(turn_id=EXAMPLE_TURN_ID, data=SttPartialData(text=text, stable=False))


async def test_every_example_crosses_the_fake_in_both_directions() -> None:
    audio, core = fake_pair()
    for event in example_audio_events():
        sent = await audio.send(event)
        assert await core.receive() == sent
    for event in example_core_events():
        sent = await core.send(event)
        assert await audio.receive() == sent


async def test_send_stamps_monotonic_seq_from_one() -> None:
    audio, core = fake_pair()
    stamped = [await audio.send(_partial(str(i))) for i in range(3)]
    assert [e.seq for e in stamped] == [1, 2, 3]
    assert [(await core.receive()).seq for _ in range(3)] == [1, 2, 3]
    assert core.seq_gaps == 0


async def test_concurrent_sends_keep_seq_order() -> None:
    audio, core = fake_pair()
    await asyncio.gather(*(audio.send(_partial(str(i))) for i in range(50)))
    seqs = [(await core.receive()).seq for _ in range(50)]
    assert seqs == list(range(1, 51))


async def test_seq_gap_is_logged_not_fatal() -> None:
    a, b = fake_duplex_pair()
    receiver = core_channel(b)
    for seq in (1, 2, 5):
        await a.write(encode(_partial("x").model_copy(update={"seq": seq})))
    with capture_logs() as logs:
        got = [(await receiver.receive()).seq for _ in range(3)]
    assert got == [1, 2, 5]
    assert receiver.seq_gaps == 1
    assert {"event": "event.seq_gap", "peer": "audio", "expected": 3, "got": 5} | {
        "log_level": "warning"
    } in logs


async def test_unknown_and_invalid_lines_are_skipped() -> None:
    a, b = fake_duplex_pair()
    receiver = core_channel(b)
    await a.write(b'{"type":"from.the.future","seq":1,"ts":1,"data":{}}\n')
    await a.write(b"garbage\n")
    await a.write(b'{"type":"wake","seq":2,"ts":1,"data":{}}\n')
    await a.write(encode(_partial("ok").model_copy(update={"seq": 3})))
    with capture_logs() as logs:
        event = await receiver.receive()
    assert isinstance(event, SttPartialEvent)
    assert event.data.text == "ok"
    assert receiver.dropped == 2
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
        "event.dropped",
        "event.dropped",
    ]


async def test_close_ends_both_sides() -> None:
    audio, core = fake_pair()
    await audio.send(_partial("last words"))
    await audio.close()
    assert isinstance(await core.receive(), SttPartialEvent)  # queued data still delivered
    with pytest.raises(ConnectionClosedError):
        await core.receive()
    with pytest.raises(ConnectionClosedError):
        await audio.send(_partial("too late"))
    with pytest.raises(ConnectionClosedError):
        await core.send(CancelEvent(turn_id=EXAMPLE_TURN_ID, data=CancelData(reason="user")))


async def test_close_wakes_a_pending_receive() -> None:
    audio, _core = fake_pair()
    pending = asyncio.create_task(audio.receive())
    await asyncio.sleep(0)
    await audio.close()
    with pytest.raises(ConnectionClosedError):
        await pending


async def test_async_iteration_stops_at_close() -> None:
    audio, core = fake_pair()
    for i in range(3):
        await audio.send(_partial(str(i)))
    await audio.close()
    texts = [e.data.text async for e in core if isinstance(e, SttPartialEvent)]
    assert texts == ["0", "1", "2"]


# ---------------------------------------------------------------- handshake --


async def test_open_fake_session_handshakes() -> None:
    audio, core = await open_fake_session(example_audio_hello())
    await core.send(CancelEvent(turn_id=EXAMPLE_TURN_ID, data=CancelData(reason="user")))
    received = await audio.receive()
    assert isinstance(received, CancelEvent)
    assert received.seq == 2  # hello was seq 1


async def test_protocol_mismatch_is_refused_with_fatal_error() -> None:
    audio, core = fake_pair()
    future = CoreHelloEvent(data=CoreHelloData(protocol=2))
    core_task = asyncio.create_task(core_handshake(core, future))
    with pytest.raises(ProtocolMismatchError):
        await audio_handshake(audio, example_audio_hello())
    await core_task  # core saw a protocol-1 audio hello, which it accepts
    error = await core.receive()
    assert isinstance(error, ErrorEvent)
    assert error.data.code == "protocol_mismatch"
    assert error.data.fatal


async def test_core_refuses_newer_audio() -> None:
    audio, core = fake_pair()
    hello = example_audio_hello()
    newer = hello.model_copy(update={"data": hello.data.model_copy(update={"protocol": 2})})
    await audio.send(newer)
    with pytest.raises(ProtocolMismatchError):
        await core_handshake(core)


async def test_first_event_must_be_hello() -> None:
    a, b = fake_duplex_pair()
    audio = audio_channel(a)
    await b.write(encode(CancelEvent(turn_id=EXAMPLE_TURN_ID, data=CancelData(reason="user"))))
    with pytest.raises(HandshakeError, match="expected hello"):
        await audio_handshake(audio, example_audio_hello())


async def test_handshake_times_out() -> None:
    audio, _core = fake_pair()
    with pytest.raises(HandshakeError, match="no hello"):
        await audio_handshake(audio, example_audio_hello(), hello_timeout_s=0.05)
