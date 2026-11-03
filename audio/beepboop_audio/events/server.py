"""The audio side of the socket: audio **listens**, core connects (ARCHITECTURE §4.1).

Each accepted connection gets its own :class:`~.connection.EventChannel`; the
server sends audio's ``hello``, requires core's, then hands the channel to the
``handler`` coroutine. When the handler returns or the peer disconnects the
channel is closed. Transport and handshake failures end that one connection
and are logged; exceptions raised by the handler itself propagate into the
connection task and are logged with a traceback, never silently dropped.

Usage::

    async def handle(channel: AudioSide, peer_hello: CoreHelloEvent) -> None:
        async for event in channel:
            ...

    async with AudioEventServer(address_from_env(), make_hello, handle) as server:
        await server.serve_forever()
"""

from __future__ import annotations

import asyncio
import contextlib
import stat
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Self

import structlog

from beepboop_audio.events.address import SocketAddress, TcpAddress, UnixAddress
from beepboop_audio.events.audio_events import AudioHelloEvent
from beepboop_audio.events.codec import ProtocolError
from beepboop_audio.events.connection import (
    AudioSide,
    StreamDuplex,
    audio_channel,
    audio_handshake,
)
from beepboop_audio.events.core_events import CoreHelloEvent

log = structlog.get_logger(__name__)

AudioHandler = Callable[[AudioSide, CoreHelloEvent], Awaitable[None]]

#: Socket file mode: owner and group (core and audio share a group in compose).
DEFAULT_SOCKET_MODE = 0o660


_NO_UNIX = (
    "unix sockets are not available on this platform; for development set "
    "BB_AUDIO_SOCKET=tcp://127.0.0.1:<port> (see audio/beepboop_audio/events/README.md)"
)


def unix_sockets_supported() -> bool:
    """True when this platform's asyncio can serve a unix socket (not Windows)."""
    return sys.platform != "win32"


def _prepare_socket_path(path: Path) -> None:
    """Create the parent directory and remove a stale socket file (never a regular file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()


class AudioEventServer:
    """Listen on ``address`` and run ``handler`` for each core connection."""

    def __init__(
        self,
        address: SocketAddress,
        hello: Callable[[], AudioHelloEvent],
        handler: AudioHandler,
        *,
        socket_mode: int = DEFAULT_SOCKET_MODE,
    ) -> None:
        self.address: SocketAddress = address
        self._hello = hello
        self._handler = handler
        self._socket_mode = socket_mode
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if isinstance(self.address, UnixAddress):
            self._server = await self._start_unix(self.address.path)
        else:
            self._server = await asyncio.start_server(
                self._on_connect, self.address.host, self.address.port
            )
            # Port 0 means "any free port": report the real one.
            port: int = self._server.sockets[0].getsockname()[1]
            self.address = TcpAddress(self.address.host, port)
        log.info("audio_socket.listening", address=str(self.address))

    async def _start_unix(self, path: Path) -> asyncio.Server:
        if sys.platform == "win32":
            raise RuntimeError(_NO_UNIX)
        _prepare_socket_path(path)
        server = await asyncio.start_unix_server(self._on_connect, path)
        path.chmod(self._socket_mode)  # noqa: ASYNC240 - one metadata call at startup
        return server

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        if self._server is None:  # pragma: no cover - start() always sets it
            raise RuntimeError("server failed to start")
        await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if isinstance(self.address, UnixAddress):
            self.address.path.unlink(missing_ok=True)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        channel = audio_channel(StreamDuplex(reader, writer))
        try:
            peer_hello = await audio_handshake(channel, self._hello())
            log.info("audio_socket.core_connected")
            await self._handler(channel, peer_hello)
        except ProtocolError as exc:
            log.warning("audio_socket.connection_ended", reason=str(exc))
        finally:
            await channel.close()
            if task is not None:
                self._tasks.discard(task)


__all__ = ["DEFAULT_SOCKET_MODE", "AudioEventServer", "AudioHandler", "unix_sockets_supported"]
