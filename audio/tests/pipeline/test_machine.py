"""The turn state machine end to end with fake providers (VOICE_PIPELINE §4, §5)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from argon2 import PasswordHasher

from beepboop_audio.harness import ALEX, ALEX_HZ, SAM, SAM_HZ, PipelineHarness
from beepboop_audio.pipeline.leds import LedState
from beepboop_audio.pipeline.machine import State, cap_hotwords
from beepboop_audio.testing import silence, voice
from beepboop_protocol import (
    CancelData,
    CancelEvent,
    CapturePinData,
    CapturePinEvent,
    EarconData,
    EarconEvent,
    EnrollClipEvent,
    EnrollStartData,
    EnrollStartEvent,
    EnrollStopData,
    EnrollStopEvent,
    MicStateEvent,
    MuteData,
    MuteEvent,
    PinResultEvent,
    SpeakDoneEvent,
    SpeakerFinalEvent,
    SpeakerProvisionalEvent,
    SttFinalEvent,
    WakeEvent,
    new_ulid,
)


@pytest.fixture
async def h() -> AsyncIterator[PipelineHarness]:
    harness = PipelineHarness("turn off the kitchen lights")
    yield harness
    await harness.pipeline.close()
    harness.close()


async def command(h: PipelineHarness, hz: float = ALEX_HZ, ms: int = 1500) -> None:
    await h.feed(voice(hz, ms))
    await h.feed(silence(600))
    await h.settle()


async def test_full_turn_event_order(h: PipelineHarness) -> None:
    await h.wake()
    assert h.pipeline.state == State.LISTENING
    await command(h)
    types = h.types()
    assert types[0] == "wake"
    assert "speaker.provisional" in types
    assert types.index("speaker.provisional") < types.index("speaker.final")
    assert types.index("speaker.final") < types.index("stt.final")
    final = h.of("stt.final")[-1]
    assert isinstance(final, SttFinalEvent)
    assert final.data.text == "turn off the kitchen lights"
    assert final.data.endpoint == "silence"
    assert not final.data.empty
    assert final.data.t_speech_start <= final.data.t_eos <= final.data.t_final
    assert final.data.provider == h.stt.id
    # every turn event carries the turn id minted at wake
    turn_ids = {e.turn_id for e in h.sent}
    assert len(turn_ids) == 1
    assert None not in turn_ids
    assert h.pipeline.state == State.THINKING
    assert LedState.LISTENING in h.leds.states
    assert LedState.THINKING in h.leds.states


async def test_wake_event_fields(h: PipelineHarness) -> None:
    await h.wake()
    wake = h.of("wake")[0]
    assert isinstance(wake, WakeEvent)
    assert wake.data.trigger == "wake"
    assert wake.data.model == "hey_beepboop"
    assert wake.data.preroll_ms == 1200
    assert wake.data.device == "beepboop"


async def test_unenrolled_household_is_guest(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    prov = h.of("speaker.provisional")[0]
    final = h.of("speaker.final")[0]
    assert isinstance(prov, SpeakerProvisionalEvent)
    assert isinstance(final, SpeakerFinalEvent)
    assert prov.data.profile_id == "guest"
    assert final.data.profile_id == "guest"
    assert final.data.anchor is True
    assert h.embedder.calls == 0  # no voiceprints: nothing to embed against


async def test_enrolled_speaker_identified_provisional_and_final(h: PipelineHarness) -> None:
    await h.sync_two_profiles()
    await h.wake(ALEX_HZ)
    await command(h, ALEX_HZ)
    prov = h.of("speaker.provisional")[0]
    final = h.of("speaker.final")[0]
    assert isinstance(prov, SpeakerProvisionalEvent)
    assert isinstance(final, SpeakerFinalEvent)
    assert prov.data.profile_id == ALEX
    assert final.data.profile_id == ALEX
    assert final.data.candidates[0].profile_id == ALEX
    assert final.data.audio_ms == 1200 + 1500
    assert 0.0 <= final.data.margin <= final.data.score <= 1.0


async def test_other_speaker_identified(h: PipelineHarness) -> None:
    await h.sync_two_profiles()
    await h.wake(SAM_HZ)
    await command(h, SAM_HZ)
    final = h.of("speaker.final")[0]
    assert isinstance(final, SpeakerFinalEvent)
    assert final.data.profile_id == SAM


async def test_hotwords_household_and_provisional_profile(h: PipelineHarness) -> None:
    await h.sync_two_profiles(household=("kitchen", "Living Room"))
    await h.wake(ALEX_HZ)
    await command(h, ALEX_HZ)
    ctx = h.stt.contexts[0]
    assert ctx.mode == "command"
    assert "kitchen" in ctx.hotwords
    assert "living room" in ctx.hotwords
    assert "alex lamp" in ctx.hotwords
    assert "sam lamp" not in ctx.hotwords


async def test_false_wake_sends_empty_final_and_returns_idle(h: PipelineHarness) -> None:
    await h.wake()
    await h.feed(silence(5100))
    await h.settle()
    assert h.types()[-2:] == ["speaker.final", "stt.final"]
    final = h.of("stt.final")[-1]
    assert isinstance(final, SttFinalEvent)
    assert final.data.empty
    assert final.data.text == ""
    assert h.pipeline.state == State.IDLE
    assert h.frontend.armed


async def test_cough_is_not_speech(h: PipelineHarness) -> None:
    await h.wake()
    await h.feed(silence(500))
    await h.feed(voice(ALEX_HZ, 150))  # below the 300 ms minimum
    await h.feed(silence(500))
    assert h.pipeline.state == State.LISTENING
    assert not h.of("stt.final")


async def test_max_utterance_cuts_with_max_len() -> None:
    from beepboop_audio.config import AudioConfig, AudioSection, EndpointConfig

    config = AudioConfig(audio=AudioSection(endpoint=EndpointConfig(max_utterance_ms=2000)))
    h = PipelineHarness("go on and on", config=config)
    try:
        await h.wake()
        await h.feed(voice(ALEX_HZ, 2500))
        await h.settle()
        final = h.of("stt.final")[-1]
        assert isinstance(final, SttFinalEvent)
        assert final.data.endpoint == "max_len"
        assert LedState.TOO_LONG in h.leds.states
    finally:
        await h.pipeline.close()
        h.close()


async def test_partials_throttled(h: PipelineHarness) -> None:
    h.stt.transcripts.clear()
    h.stt.script("one two three four five six seven eight nine ten")
    await h.wake()
    await command(h, ms=2000)
    partials = h.of("stt.partial")
    # the fake emits one partial per 100 ms of audio; the pipeline lets through ≤ 1 per 200 ms
    assert 1 <= len(partials) <= 10


async def test_speak_then_done_then_idle(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    turn_id = h.sent[0].turn_id
    assert turn_id is not None
    await h.speak(turn_id, "Okay, the kitchen lights are off.")
    await h.settle()
    assert h.pipeline.state == State.SPEAKING
    await h.play(200)
    await h.settle()
    done = h.of("speak.done")
    assert len(done) == 1
    assert isinstance(done[0], SpeakDoneEvent)
    assert done[0].data.reason == "finished"
    assert done[0].data.audio_ms > 0
    assert done[0].data.t_first_audio <= done[0].data.t_last_audio
    assert h.pipeline.state == State.IDLE
    assert h.tts.spoken == ["Okay, the kitchen lights are off."]


async def test_sentences_play_in_seq_order(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    turn_id = h.sent[0].turn_id
    assert turn_id is not None
    await h.speak(turn_id, "Second.", seq=1, last=True)
    await h.speak(turn_id, "First.", seq=0, last=False)
    await h.settle()
    await h.play(200)
    await h.settle()
    assert h.tts.spoken == ["First.", "Second."]
    assert len(h.of("speak.done")) == 1  # one utterance id


async def test_cached_clip_and_repeated_sentence_skip_tts(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    turn_id = h.sent[0].turn_id
    assert turn_id is not None
    await h.speak(turn_id, None, clip="done")
    await h.settle()
    await h.play(100)
    await h.settle()
    assert h.tts.spoken == ["Done."]  # rendered once, then cached
    h2_turn = turn_id
    await h.pipeline.handle(
        CancelEvent(turn_id=h2_turn, data=CancelData(reason="user"))
    )  # no-op: turn already over


async def test_unknown_clip_without_text_is_error(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    turn_id = h.sent[0].turn_id
    assert turn_id is not None
    await h.speak(turn_id, None, clip="no.such.clip")
    await h.settle()
    done = h.of("speak.done")
    assert len(done) == 1
    assert isinstance(done[0], SpeakDoneEvent)
    assert done[0].data.reason == "error"


async def test_earcon_name_as_clip_plays_bare_sound(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    turn_id = h.sent[0].turn_id
    assert turn_id is not None
    await h.speak(turn_id, None, clip="ack")
    await h.settle()
    await h.play(50)
    await h.settle()
    assert h.tts.spoken == []
    done = h.of("speak.done")
    assert isinstance(done[0], SpeakDoneEvent)
    assert done[0].data.reason == "finished"


async def test_reply_window_opens_and_takes_a_reply(h: PipelineHarness) -> None:
    h.stt.script("the kitchen one")
    await h.wake()
    await command(h)
    first = h.sent[0].turn_id
    assert first is not None
    await h.speak(first, "Which light?", listen_after=True)
    await h.settle()
    await h.play(200)
    await h.settle()
    assert h.pipeline.state == State.REPLY_WINDOW
    await h.feed(silence(300))
    await h.feed(voice(ALEX_HZ, 1200))
    await h.feed(silence(600))
    await h.settle()
    wakes = h.of("wake")
    assert len(wakes) == 2
    reply = wakes[1]
    assert isinstance(reply, WakeEvent)
    assert reply.data.trigger == "reply"
    assert reply.turn_id != first
    final = next(e for e in h.of("speaker.final") if e.turn_id == reply.turn_id)
    assert isinstance(final, SpeakerFinalEvent)
    assert final.data.anchor is False
    stt = next(e for e in h.of("stt.final") if e.turn_id == reply.turn_id)
    assert isinstance(stt, SttFinalEvent)
    assert stt.data.text == "the kitchen one"
    assert not [e for e in h.of("speaker.provisional") if e.turn_id == reply.turn_id]


async def test_reply_window_times_out(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    first = h.sent[0].turn_id
    assert first is not None
    await h.speak(first, "Which light?", listen_after=True)
    await h.settle()
    await h.play(200)
    await h.settle()
    await h.feed(silence(8100))
    assert h.pipeline.state == State.IDLE


async def test_thinking_timeout_plays_error(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    await h.feed(silence(10_100))
    assert h.pipeline.state == State.IDLE
    assert LedState.ERROR in h.leds.states


async def test_cancel_flushes_speech(h: PipelineHarness) -> None:
    await h.wake()
    await command(h)
    turn_id = h.sent[0].turn_id
    assert turn_id is not None
    await h.speak(turn_id, "A long answer that will never finish playing out loud.")
    await h.settle()
    await h.play(3)
    await h.pipeline.handle(CancelEvent(turn_id=turn_id, data=CancelData(reason="user")))
    await h.settle()
    done = h.of("speak.done")
    assert len(done) == 1
    assert isinstance(done[0], SpeakDoneEvent)
    assert done[0].data.reason == "cancelled"
    assert h.pipeline.state == State.IDLE
    assert not h.mixer.speech_busy


async def test_cancel_while_listening(h: PipelineHarness) -> None:
    await h.wake()
    turn_id = h.sent[0].turn_id
    await h.feed(voice(ALEX_HZ, 400))
    await h.pipeline.handle(CancelEvent(turn_id=turn_id, data=CancelData(reason="user")))
    await h.settle()
    assert h.pipeline.state == State.IDLE
    assert not h.of("stt.final")


async def test_alarm_loops_until_cancelled(h: PipelineHarness) -> None:
    turn_id = new_ulid()
    await h.speak(turn_id, None, clip="alarm")
    await h.settle()
    await h.play(150)  # 3 s: the 1 s pattern loops
    assert h.mixer.alert_active
    await h.pipeline.handle(CancelEvent(turn_id=turn_id, data=CancelData(reason="user")))
    assert not h.mixer.alert_active


async def test_earcon_event_plays(h: PipelineHarness) -> None:
    await h.pipeline.handle(EarconEvent(data=EarconData(name="ack")))
    assert LedState.TOOL in h.leds.states


# --------------------------------------------------------------------- PIN ----

_hasher = PasswordHasher()


async def _pin_turn(h: PipelineHarness, spoken: str, pin: str = "4719") -> list[str]:
    h.stt.script(spoken)
    await h.wake()
    await command(h)
    turn_id = h.sent[0].turn_id
    assert turn_id is not None
    await h.pipeline.handle(
        CapturePinEvent(
            turn_id=turn_id,
            data=CapturePinData(approval_id="ap1", pin_hash=_hasher.hash(pin)),
        )
    )
    await h.until_listening_for_pin()  # the "Please say your PIN." prompt plays first
    assert h.pipeline.state == State.PIN_CAPTURE
    await h.feed(voice(ALEX_HZ, 1500))
    await h.feed(silence(600))
    await h.settle()
    return [e.data.outcome for e in h.of("pin.result") if isinstance(e, PinResultEvent)]


async def test_pin_ok() -> None:
    h = PipelineHarness("unlock the door")
    try:
        assert await _pin_turn(h, "four seven one nine") == ["ok"]
        assert h.pipeline.state == State.THINKING
        assert h.stt.contexts[-1].mode == "pin"
        assert LedState.PIN in h.leds.states
        # the digits never leave: no stt event carries them
        for event in h.sent:
            assert "four" not in event.model_dump_json()
            assert "4719" not in event.model_dump_json()
    finally:
        await h.pipeline.close()
        h.close()


async def test_pin_mismatch() -> None:
    h = PipelineHarness("unlock the door")
    try:
        assert await _pin_turn(h, "one two three four") == ["mismatch"]
    finally:
        await h.pipeline.close()
        h.close()


async def test_pin_timeout() -> None:
    h = PipelineHarness("unlock the door")
    try:
        await h.wake()
        await command(h)
        turn_id = h.sent[0].turn_id
        await h.pipeline.handle(
            CapturePinEvent(
                turn_id=turn_id,
                data=CapturePinData(
                    approval_id="ap1", pin_hash=_hasher.hash("1234"), timeout_ms=2000
                ),
            )
        )
        await h.until_listening_for_pin()
        await h.feed(silence(2100))
        await h.settle()
        outcomes = [e.data.outcome for e in h.of("pin.result") if isinstance(e, PinResultEvent)]
        assert outcomes == ["timeout"]
    finally:
        await h.pipeline.close()
        h.close()


async def test_pin_for_another_turn_is_cancelled(h: PipelineHarness) -> None:
    await h.pipeline.handle(
        CapturePinEvent(
            turn_id=new_ulid(),
            data=CapturePinData(approval_id="ap9", pin_hash=_hasher.hash("1234")),
        )
    )
    await h.settle()
    result = h.of("pin.result")
    assert isinstance(result[0], PinResultEvent)
    assert result[0].data.outcome == "cancelled"


# -------------------------------------------------------------------- mute ----


async def test_software_mute_disarms_and_reports(h: PipelineHarness) -> None:
    await h.pipeline.handle(MuteEvent(data=MuteData(on=True)))
    assert h.pipeline.state == State.MUTED
    assert h.frontend.muted
    assert not h.frontend.armed
    assert len(h.ring) == 0
    await h.wake()
    assert not h.of("wake")
    await h.pipeline.handle(MuteEvent(data=MuteData(on=False)))
    assert h.pipeline.state == State.IDLE
    states = [(e.data.muted, e.data.source) for e in h.of_type(MicStateEvent)]
    assert states == [(True, "software"), (False, "software")]


async def test_software_unmute_cannot_override_hardware(h: PipelineHarness) -> None:
    await h.pipeline.set_hardware_mute(True)
    await h.pipeline.handle(MuteEvent(data=MuteData(on=True)))
    await h.pipeline.handle(MuteEvent(data=MuteData(on=False)))
    assert h.pipeline.muted
    await h.pipeline.set_hardware_mute(False)
    assert not h.pipeline.muted


async def test_mute_mid_turn_cancels_it(h: PipelineHarness) -> None:
    await h.wake()
    await h.feed(voice(ALEX_HZ, 500))
    await h.pipeline.handle(MuteEvent(data=MuteData(on=True)))
    await h.settle()
    final = h.of("stt.final")[-1]
    assert isinstance(final, SttFinalEvent)
    assert final.data.endpoint == "cancel"
    assert h.types()[-1] == "mic.state"


# --------------------------------------------------------------- enrollment ---


async def test_enrollment_free_speech_clip_accepted(h: PipelineHarness) -> None:
    session = "s1"
    await h.pipeline.handle(
        EnrollStartEvent(
            data=EnrollStartData(session_id=session, profile_id=ALEX, mode="free", prompts=["x"])
        )
    )
    await h.feed(silence(600))
    await h.feed(voice(ALEX_HZ, 2000))
    await h.feed(silence(600))
    await h.settle()
    clips = h.of_type(EnrollClipEvent)
    assert len(clips) == 1
    clip = clips[0]
    assert clip.data.accepted
    assert clip.data.embedding is not None
    assert len(clip.data.embedding) == h.embedder.dim
    assert clip.data.quality.speech_ratio >= 0.6
    assert not h.of("wake")  # enrollment never opens a turn
    await h.pipeline.handle(EnrollStopEvent(data=EnrollStopData(session_id=session)))
    assert h.pipeline.enroll is None


async def test_enrollment_wake_mode_requires_wake_hit(h: PipelineHarness) -> None:
    await h.pipeline.handle(
        EnrollStartEvent(
            data=EnrollStartData(session_id="s2", profile_id=ALEX, mode="wake", prompts=[])
        )
    )
    await h.feed(silence(600))
    await h.feed(voice(ALEX_HZ, 900))  # no wake hit in the clip
    await h.feed(silence(600))
    await h.feed(voice(ALEX_HZ, 900), wake_at_end=True)
    await h.feed(silence(600))
    await h.settle()
    accepted = [c.data.accepted for c in h.of_type(EnrollClipEvent)]
    assert accepted == [False, True]
    assert [c.data.index for c in h.of_type(EnrollClipEvent)] == [0, 1]


async def test_enrollment_noisy_clip_rejected(h: PipelineHarness) -> None:
    await h.pipeline.handle(
        EnrollStartEvent(
            data=EnrollStartData(session_id="s3", profile_id=ALEX, mode="free", prompts=[])
        )
    )
    await h.feed(voice(SAM_HZ, 600, amplitude=0.015))  # a TV under the VAD threshold
    await h.feed(voice(ALEX_HZ, 1500, amplitude=0.05))  # ~10 dB above it: below the 15 dB gate
    await h.feed(silence(600))
    await h.settle()
    clip = h.of_type(EnrollClipEvent)[0]
    assert not clip.data.accepted
    assert clip.data.embedding is None


def test_cap_hotwords() -> None:
    many = tuple(f"word {i}" for i in range(300))
    out = cap_hotwords(("Kitchen", "kitchen  ", "a b c d e"), many)
    assert out[0] == "kitchen"
    assert "a b c d e" not in out  # > 4 tokens
    assert len(out) == 200
