"""In-process fake transport: two connected channels backed by asyncio queues.

For tests and the bench. Every event still goes through the real codec
(``encode`` → bytes → :class:`~.codec.LineFramer` → ``decode``), so a model
that cannot round-trip fails here exactly as it would on the socket::

    audio, core = fake_pair()
    await core.send(CancelEvent(turn_id=tid, data=CancelData(reason="user")))
    event = await audio.receive()

``open_fake_session`` also runs both ``hello`` handshakes. The queues are
unbounded (no backpressure), which is fine for tests and the bench.
"""

from __future__ import annotations

import asyncio

from beepboop_protocol.audio_events import AudioHelloEvent
from beepboop_protocol.connection import (
    AudioSide,
    ConnectionClosedError,
    CoreSide,
    audio_channel,
    audio_handshake,
    core_channel,
    core_handshake,
)
from beepboop_protocol.core_events import CoreHelloEvent

_EOF = b""


class QueueDuplex:
    """One end of an in-memory byte pipe. Closing either end ends both streams."""

    def __init__(self, inbox: asyncio.Queue[bytes], outbox: asyncio.Queue[bytes]) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._closed = False
        self.peer: QueueDuplex | None = None

    async def read(self) -> bytes:
        if self._closed:
            return _EOF
        return await self._inbox.get()

    async def write(self, data: bytes) -> None:
        if self._closed or (self.peer is not None and self.peer._closed):
            raise ConnectionClosedError("fake pipe is closed")
        self._outbox.put_nowait(data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._inbox.put_nowait(_EOF)  # wake our own pending read
        self._outbox.put_nowait(_EOF)  # the peer sees end of stream


def fake_duplex_pair() -> tuple[QueueDuplex, QueueDuplex]:
    """Two connected byte duplexes (``a`` writes what ``b`` reads and vice versa)."""
    a_to_b: asyncio.Queue[bytes] = asyncio.Queue()
    b_to_a: asyncio.Queue[bytes] = asyncio.Queue()
    a, b = QueueDuplex(b_to_a, a_to_b), QueueDuplex(a_to_b, b_to_a)
    a.peer, b.peer = b, a
    return a, b


def fake_pair() -> tuple[AudioSide, CoreSide]:
    """An (audio end, core end) pair of channels, connected in-process, no handshake."""
    audio_end, core_end = fake_duplex_pair()
    return audio_channel(audio_end), core_channel(core_end)


async def open_fake_session(
    audio_hello: AudioHelloEvent, core_hello: CoreHelloEvent | None = None
) -> tuple[AudioSide, CoreSide]:
    """A connected pair with both ``hello`` handshakes already done."""
    audio, core = fake_pair()
    await asyncio.gather(audio_handshake(audio, audio_hello), core_handshake(core, core_hello))
    return audio, core


__all__ = ["QueueDuplex", "fake_duplex_pair", "fake_pair", "open_fake_session"]
