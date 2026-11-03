"""Cross-package contract: core talks to audio through ``beepboop_audio.events``.

Core has no audio consumer yet in M0; this pins that the public import path core
will use works from core's side and that a full ``hello`` handshake plus one event
in each direction crosses the in-process fake (real encoder/decoder underneath).
"""

from __future__ import annotations

from beepboop_audio.events import open_fake_session
from beepboop_audio.events.examples import (
    example_audio_events,
    example_audio_hello,
    example_core_events,
    example_core_hello,
)


async def test_core_and_audio_exchange_events_over_the_fake() -> None:
    audio, core = await open_fake_session(example_audio_hello(), example_core_hello())
    try:
        sent_up = await audio.send(example_audio_events()[0])
        received_up = await core.receive()
        assert received_up.type == sent_up.type
        assert received_up.data == sent_up.data

        sent_down = await core.send(example_core_events()[0])
        received_down = await audio.receive()
        assert received_down.type == sent_down.type
        assert received_down.data == sent_down.data
    finally:
        await audio.close()
        await core.close()
