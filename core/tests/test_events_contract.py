"""Cross-package contract: core talks to audio through ``beepboop_protocol`` (ADR 0019).

Core has no audio consumer yet in M0; this pins that the public import path core
will use works from core's side and that a full ``hello`` handshake plus one event
in each direction crosses the in-process fake (real encoder/decoder underneath).
"""

from __future__ import annotations

from beepboop_protocol import open_fake_session
from beepboop_protocol.examples import (
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


def test_core_log_is_the_shared_protocol_log() -> None:
    """``beepboop.log`` re-exports the one redaction processor both services use (ADR 0019)."""
    import beepboop.log
    import beepboop_protocol.log

    assert beepboop.log.redact_secrets is beepboop_protocol.log.redact_secrets
    assert beepboop.log.configure_logging is beepboop_protocol.log.configure_logging
