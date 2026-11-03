"""Server and client over real sockets: loopback TCP everywhere, unix where supported."""

from __future__ import annotations

import asyncio
import socket
import stat
import sys
from pathlib import Path

import pytest

from beepboop_audio.events import (
    AudioEventServer,
    AudioHelloEvent,
    AudioSide,
    Backoff,
    CancelData,
    CancelEvent,
    CoreEventClient,
    CoreHelloEvent,
    CoreSide,
    SocketAddress,
    SttPartialData,
    SttPartialEvent,
    TcpAddress,
    UnixAddress,
    audio_channel,
    audio_handshake,
    unix_sockets_supported,
)
from beepboop_audio.events.connection import ByteDuplex
from beepboop_audio.events.examples import EXAMPLE_TURN_ID, example_audio_hello
from beepboop_audio.events.fake import fake_duplex_pair

unix_only = pytest.mark.skipif(not unix_sockets_supported(), reason="no AF_UNIX asyncio server")


async def _exchange(address: SocketAddress) -> list[str]:
    """Audio sends three partials, core answers each with a cancel; return what core saw."""
    seen: list[str] = []
    audio_got: list[str] = []
    done = asyncio.Event()

    async def audio_handler(channel: AudioSide, _hello: CoreHelloEvent) -> None:
        for i in range(3):
            await channel.send(
                SttPartialEvent(
                    turn_id=EXAMPLE_TURN_ID, data=SttPartialData(text=f"p{i}", stable=False)
                )
            )
            reply = await channel.receive()
            audio_got.append(reply.type)

    async def core_handler(channel: CoreSide, hello: AudioHelloEvent) -> None:
        assert hello.data.device.id == "living-room"
        async for event in channel:
            if isinstance(event, SttPartialEvent):
                seen.append(event.data.text)
                await channel.send(
                    CancelEvent(turn_id=EXAMPLE_TURN_ID, data=CancelData(reason="user"))
                )
        done.set()

    async with AudioEventServer(address, example_audio_hello, audio_handler) as server:
        client = CoreEventClient(server.address, core_handler)
        assert await asyncio.wait_for(client.run_once(), 5)
        await asyncio.wait_for(done.wait(), 5)
    assert audio_got == ["cancel"] * 3
    return seen


async def test_tcp_loopback_end_to_end() -> None:
    assert await _exchange(TcpAddress("127.0.0.1", 0)) == ["p0", "p1", "p2"]


@unix_only
async def test_unix_socket_end_to_end(tmp_path: Path) -> None:
    sock = tmp_path / "run" / "audio.sock"
    assert await _exchange(UnixAddress(sock)) == ["p0", "p1", "p2"]
    assert not sock.exists()  # removed on close


async def test_unix_server_replaces_stale_socket_and_sets_mode(tmp_path: Path) -> None:
    if sys.platform == "win32":  # also tells pyright that AF_UNIX exists below
        pytest.skip("no AF_UNIX on Windows")
    sock = tmp_path / "audio.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(sock))
    stale.close()

    async def handler(_c: AudioSide, _h: CoreHelloEvent) -> None:
        return None

    async with AudioEventServer(UnixAddress(sock), example_audio_hello, handler):
        assert stat.S_IMODE(sock.stat().st_mode) == 0o660


@unix_only
async def test_unix_server_never_deletes_a_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "audio.sock"
    path.write_text("not a socket")

    async def handler(_c: AudioSide, _h: CoreHelloEvent) -> None:
        return None

    with pytest.raises(OSError, match=r"in use|exists"):
        async with AudioEventServer(UnixAddress(path), example_audio_hello, handler):
            pass
    assert path.read_text() == "not a socket"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only message")
async def test_unix_on_windows_explains_tcp_fallback() -> None:
    async def handler(_c: AudioSide, _h: CoreHelloEvent) -> None:
        return None

    with pytest.raises(RuntimeError, match=r"tcp://127\.0\.0\.1"):
        await AudioEventServer(
            UnixAddress(Path("/run/x.sock")), example_audio_hello, handler
        ).start()


# ----------------------------------------------------------------- backoff --


def test_backoff_doubles_to_five_seconds_and_resets() -> None:
    backoff = Backoff()
    assert [backoff.next() for _ in range(7)] == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]
    backoff.reset()
    assert backoff.next() == 0.5


class _StopError(Exception):
    pass


async def test_client_retries_with_backoff_when_audio_is_down() -> None:
    delays: list[float] = []

    async def refuse() -> ByteDuplex:
        raise ConnectionRefusedError("audio not listening")

    async def record(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 6:
            raise _StopError

    async def never(_c: CoreSide, _h: AudioHelloEvent) -> None:
        raise AssertionError("must not connect")

    client = CoreEventClient(TcpAddress("127.0.0.1", 9), never, connect=refuse, sleep=record)
    with pytest.raises(_StopError):
        await client.run()
    assert delays == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


async def test_client_resets_backoff_after_a_successful_handshake() -> None:
    delays: list[float] = []
    attempts = 0
    sessions = 0
    background: list[asyncio.Task[None]] = []

    async def connect() -> ByteDuplex:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError
        # A fake audio end that completes the handshake and then hangs up.
        audio_end, core_end = fake_duplex_pair()

        async def audio_side() -> None:
            channel = audio_channel(audio_end)
            await audio_handshake(channel, example_audio_hello())
            await channel.close()

        background.append(asyncio.create_task(audio_side()))
        return core_end

    async def handler(channel: CoreSide, _h: AudioHelloEvent) -> None:
        nonlocal sessions
        sessions += 1
        async for _ in channel:
            pass

    async def record(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 4:
            raise _StopError

    client = CoreEventClient(TcpAddress("127.0.0.1", 9), handler, connect=connect, sleep=record)
    with pytest.raises(_StopError):
        await client.run()
    # fail, fail, connect (reset) + disconnect, connect (reset) + disconnect
    assert delays == [0.5, 1.0, 0.5, 0.5]
    assert sessions == 2
    assert client.connections == 2
    await asyncio.gather(*background)


async def test_client_handler_bugs_propagate() -> None:
    audio_end, core_end = fake_duplex_pair()
    await audio_channel(audio_end).send(example_audio_hello())

    async def connect() -> ByteDuplex:
        return core_end

    async def buggy(_c: CoreSide, _h: AudioHelloEvent) -> None:
        raise KeyError("bug")

    client = CoreEventClient(TcpAddress("127.0.0.1", 9), buggy, connect=connect)
    with pytest.raises(KeyError):
        await client.run_once()
