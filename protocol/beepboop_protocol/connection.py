"""A typed, framed event channel over any byte duplex, plus the ``hello`` handshake.

:class:`EventChannel` is the one object both sides use, whatever carries the
bytes (a unix socket, loopback TCP in dev, or the in-process fake):

* ``send`` stamps the per-sender monotonic ``seq`` (starting at 1) and writes
  one line. Sends are serialized, so concurrent senders keep ``seq`` order.
* ``receive`` returns the next known, valid event. Unknown types are skipped
  (debug log), invalid lines are skipped (warning log, field locations only,
  never values), ``seq`` gaps are logged and never fatal (ARCHITECTURE §4.1).
* Once the handshake has set :attr:`EventChannel.embedding_dim` (from audio's
  ``hello.speaker.dim``), an event whose embeddings have another length is
  dropped on receive and refused on send (ARCHITECTURE §4.4, ADR 0019).
* End of stream raises :class:`ConnectionClosedError`.

Logs name the event ``type``, ``seq`` and ``turn_id`` only, never ``data``:
``data`` holds transcripts and, in ``capture_pin``, a PIN hash.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Protocol

import structlog

from beepboop_protocol.audio_events import AudioEvent, AudioHelloEvent, ErrorData, ErrorEvent
from beepboop_protocol.codec import (
    EventValidationError,
    LineFramer,
    ProtocolError,
    decode_audio_event,
    decode_core_event,
    encode,
)
from beepboop_protocol.core_events import CoreEvent, CoreHelloData, CoreHelloEvent
from beepboop_protocol.envelope import PROTOCOL_MAJOR, EventBase

log = structlog.get_logger(__name__)

#: How long each side waits for the peer's ``hello``.
HELLO_TIMEOUT_S = 5.0


class ConnectionClosedError(ProtocolError):
    """The peer closed the stream (or this side already closed it)."""


class HandshakeError(ProtocolError):
    """The peer did not open with a valid ``hello``."""


class ProtocolMismatchError(HandshakeError):
    """The peer speaks a different protocol major version (§4.1: refuse)."""


class ByteDuplex(Protocol):
    """The minimal byte transport an :class:`EventChannel` needs."""

    async def read(self) -> bytes:
        """Return the next chunk of bytes; ``b""`` means end of stream."""
        ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


class StreamDuplex:
    """:class:`ByteDuplex` over an asyncio ``StreamReader``/``StreamWriter`` pair."""

    CHUNK = 64 * 1024

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read(self) -> bytes:
        return await self._reader.read(self.CHUNK)

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer.is_closing():
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError) as exc:  # peer already gone
            log.debug("event.close_error", error=exc.__class__.__name__)


class EventChannel[InT: EventBase, OutT: EventBase]:
    """Typed event stream: receives ``InT`` events and sends ``OutT`` events."""

    def __init__(
        self,
        duplex: ByteDuplex,
        decoder: Callable[[bytes], InT | None],
        *,
        peer: str = "peer",
        framer: LineFramer | None = None,
    ) -> None:
        self._duplex = duplex
        self._decode = decoder
        self._framer = framer or LineFramer()
        self._pending: list[bytes] = []
        self._send_lock = asyncio.Lock()
        self._seq = 0
        self._last_rx_seq: int | None = None
        self._closed = False
        self._eof = False
        self.peer = peer
        self.seq_gaps = 0
        self.dropped = 0
        #: Embedding length for this connection (audio's ``hello.speaker.dim``);
        #: ``None`` until the handshake sets it, and then nothing is checked.
        self.embedding_dim: int | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def send(self, event: OutT) -> OutT:
        """Stamp the next ``seq`` on ``event``, write it, return the stamped copy."""
        if self._closed:
            raise ConnectionClosedError("channel is closed")
        self._check_embeddings(event)
        async with self._send_lock:
            self._seq += 1
            stamped = event.model_copy(update={"seq": self._seq})
            line = encode(stamped)
            try:
                await self._duplex.write(line)
            except (ConnectionError, OSError) as exc:
                raise ConnectionClosedError(f"write failed: {exc.__class__.__name__}") from exc
        log.debug("event.sent", type=stamped.type, seq=stamped.seq, turn_id=stamped.turn_id)
        return stamped

    async def receive(self) -> InT:
        """Return the next known, valid event; raise :class:`ConnectionClosedError` at EOF."""
        while True:
            if not self._pending:
                await self._fill()
            line = self._pending.pop(0)
            try:
                event = self._decode(line)
            except ProtocolError as exc:
                self.dropped += 1
                log.warning("event.dropped", peer=self.peer, reason=str(exc))
                continue
            if event is None:
                continue
            try:
                self._check_embeddings(event)
            except EventValidationError as exc:
                self.dropped += 1
                log.warning("event.dropped", peer=self.peer, reason=str(exc))
                self._check_seq(event)
                continue
            self._check_seq(event)
            log.debug("event.received", type=event.type, seq=event.seq, turn_id=event.turn_id)
            return event

    async def __aiter__(self) -> AsyncIterator[InT]:
        try:
            while True:
                yield await self.receive()
        except ConnectionClosedError:
            return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._duplex.close()

    async def _fill(self) -> None:
        while not self._pending:
            if self._closed or self._eof:
                raise ConnectionClosedError("connection closed")
            try:
                data = await self._duplex.read()
            except (ConnectionError, OSError) as exc:
                self._eof = True
                raise ConnectionClosedError(f"read failed: {exc.__class__.__name__}") from exc
            if not data:
                self._eof = True
                self._framer.eof()
                raise ConnectionClosedError("peer closed the connection")
            self._pending.extend(self._framer.feed(data))

    def _check_embeddings(self, event: EventBase) -> None:
        dim = self.embedding_dim
        if dim is None:
            return
        if any(length != dim for length in event.embedding_lengths()):
            raise EventValidationError(event.type, [f"embedding length != speaker dim {dim}"])

    def _check_seq(self, event: EventBase) -> None:
        last = self._last_rx_seq
        if last is not None and event.seq != last + 1:
            self.seq_gaps += 1
            log.warning("event.seq_gap", peer=self.peer, expected=last + 1, got=event.seq)
        self._last_rx_seq = event.seq


#: The audio service's end: receives core events, sends audio events.
type AudioSide = EventChannel[CoreEvent, AudioEvent]
#: Core's end: receives audio events, sends core events.
type CoreSide = EventChannel[AudioEvent, CoreEvent]


def audio_channel(duplex: ByteDuplex, peer: str = "core") -> AudioSide:
    return EventChannel(duplex, decode_core_event, peer=peer)


def core_channel(duplex: ByteDuplex, peer: str = "audio") -> CoreSide:
    return EventChannel(duplex, decode_audio_event, peer=peer)


def core_hello() -> CoreHelloEvent:
    return CoreHelloEvent(data=CoreHelloData(protocol=PROTOCOL_MAJOR))


async def _first_event[E: EventBase, O: EventBase](channel: EventChannel[E, O], wait_s: float) -> E:
    try:
        async with asyncio.timeout(wait_s):
            return await channel.receive()
    except TimeoutError:
        raise HandshakeError(f"no hello from {channel.peer} within {wait_s:g} s") from None


async def audio_handshake(
    channel: AudioSide, hello: AudioHelloEvent, *, hello_timeout_s: float = HELLO_TIMEOUT_S
) -> CoreHelloEvent:
    """Audio side: send ``hello``, require core's ``hello`` with the same protocol major.

    On a mismatch audio tells core why (a fatal ``error``) before raising; the
    caller closes the channel.
    """
    await channel.send(hello)
    first = await _first_event(channel, hello_timeout_s)
    if not isinstance(first, CoreHelloEvent):
        raise HandshakeError(f"expected hello from core, got {first.type}")
    if first.data.protocol != PROTOCOL_MAJOR:
        await channel.send(
            ErrorEvent(
                data=ErrorData(
                    code="protocol_mismatch",
                    message=f"audio speaks protocol {PROTOCOL_MAJOR}, "
                    f"core sent {first.data.protocol}",
                    fatal=True,
                )
            )
        )
        raise ProtocolMismatchError(f"core protocol {first.data.protocol} != {PROTOCOL_MAJOR}")
    channel.embedding_dim = hello.data.speaker.dim
    return first


async def core_handshake(
    channel: CoreSide,
    hello: CoreHelloEvent | None = None,
    *,
    hello_timeout_s: float = HELLO_TIMEOUT_S,
) -> AudioHelloEvent:
    """Core side: send ``hello``, require audio's ``hello`` with the same protocol major."""
    await channel.send(hello or core_hello())
    first = await _first_event(channel, hello_timeout_s)
    if not isinstance(first, AudioHelloEvent):
        raise HandshakeError(f"expected hello from audio, got {first.type}")
    if first.data.protocol != PROTOCOL_MAJOR:
        raise ProtocolMismatchError(f"audio protocol {first.data.protocol} != {PROTOCOL_MAJOR}")
    channel.embedding_dim = first.data.speaker.dim
    return first


__all__ = [
    "HELLO_TIMEOUT_S",
    "AudioSide",
    "ByteDuplex",
    "ConnectionClosedError",
    "CoreSide",
    "EventChannel",
    "HandshakeError",
    "ProtocolMismatchError",
    "StreamDuplex",
    "audio_channel",
    "audio_handshake",
    "core_channel",
    "core_handshake",
    "core_hello",
]
