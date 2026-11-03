"""Core's side of the socket: connect, handshake, reconnect with backoff.

ARCHITECTURE §4.1: core connects and reconnects with backoff 0.5 s → 5 s. The
delay doubles after each failed attempt (0.5, 1, 2, 4, 5, 5, ...) and resets
once a handshake succeeds. Only transport and handshake failures trigger a
reconnect; any other exception from the handler propagates, because a bug
should crash loudly rather than spin in a reconnect loop.

Usage::

    async def handle(channel: CoreSide, audio_hello: AudioHelloEvent) -> None:
        async for event in channel:
            ...

    await CoreEventClient(address_from_env(), handle).run()   # runs until cancelled
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from beepboop_audio.events.address import SocketAddress, UnixAddress
from beepboop_audio.events.audio_events import AudioHelloEvent
from beepboop_audio.events.codec import ProtocolError
from beepboop_audio.events.connection import (
    ByteDuplex,
    CoreSide,
    StreamDuplex,
    core_channel,
    core_handshake,
    core_hello,
)
from beepboop_audio.events.core_events import CoreHelloEvent

log = structlog.get_logger(__name__)

CoreHandler = Callable[[CoreSide, AudioHelloEvent], Awaitable[None]]
Connector = Callable[[], Awaitable[ByteDuplex]]


@dataclass
class Backoff:
    """Exponential reconnect delay, ``initial`` doubling up to ``maximum``."""

    initial: float = 0.5
    maximum: float = 5.0
    factor: float = 2.0
    _next: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.reset()

    def next(self) -> float:
        delay = self._next
        self._next = min(self._next * self.factor, self.maximum)
        return delay

    def reset(self) -> None:
        self._next = self.initial


async def open_duplex(address: SocketAddress) -> StreamDuplex:
    """Connect to ``address`` (unix socket, or loopback TCP in dev)."""
    if isinstance(address, UnixAddress):
        if sys.platform == "win32":
            raise OSError("unix sockets are not available on Windows; use tcp://127.0.0.1:<port>")
        reader, writer = await asyncio.open_unix_connection(address.path)
    else:
        reader, writer = await asyncio.open_connection(address.host, address.port)
    return StreamDuplex(reader, writer)


class CoreEventClient:
    """Keeps one connection to the audio service alive and runs ``handler`` on it."""

    def __init__(
        self,
        address: SocketAddress,
        handler: CoreHandler,
        *,
        hello: CoreHelloEvent | None = None,
        backoff: Backoff | None = None,
        connect: Connector | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.address = address
        self._handler = handler
        self._hello = hello or core_hello()
        self.backoff = backoff or Backoff()
        self._connect: Connector = connect or (lambda: open_duplex(address))
        self._sleep = sleep
        self.connections = 0

    async def run(self) -> None:
        """Connect, handshake, run the handler; repeat forever. Cancel to stop."""
        while True:
            await self.run_once()
            delay = self.backoff.next()
            log.info("audio_socket.reconnecting", delay_s=delay)
            await self._sleep(delay)

    async def run_once(self) -> bool:
        """One connection attempt; True if the handshake succeeded."""
        try:
            duplex = await self._connect()
        except OSError as exc:
            log.warning(
                "audio_socket.connect_failed",
                address=str(self.address),
                error=f"{exc.__class__.__name__}: {exc}",
            )
            return False
        channel = core_channel(duplex)
        connected = False
        try:
            audio_hello = await core_handshake(channel, self._hello)
            connected = True
            self.backoff.reset()
            self.connections += 1
            log.info("audio_socket.connected", device=audio_hello.data.device.id)
            await self._handler(channel, audio_hello)
        except ProtocolError as exc:
            log.warning("audio_socket.disconnected", reason=str(exc))
        finally:
            await channel.close()
        return connected


__all__ = ["Backoff", "CoreEventClient", "CoreHandler", "open_duplex"]
