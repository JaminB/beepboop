"""Endpointing rules (VOICE_PIPELINE §7.2, §7.3)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from beepboop_audio.config import EndpointConfig
from beepboop_audio.frames import FRAME_NS
from beepboop_audio.pipeline.endpoint import (
    Endpointer,
    EndpointEvent,
    function_words,
    wants_adaptive,
)

T0 = 1_000_000_000
E = EndpointEvent


def run(ep: Endpointer, pattern: list[tuple[bool, int]]) -> list[tuple[int, EndpointEvent]]:
    """Feed (speech, ms) runs; returns (ms since T0, event) pairs."""
    out: list[tuple[int, EndpointEvent]] = []
    t = T0
    for speech, ms in pattern:
        for _ in range(ms // 10):
            t += FRAME_NS
            out.extend(((t - T0) // 1_000_000, ev) for ev in ep.update(speech, t))
    return out


def events(ep: Endpointer, pattern: list[tuple[bool, int]]) -> list[EndpointEvent]:
    return [ev for _, ev in run(ep, pattern)]


def test_speech_start_after_min_speech() -> None:
    ep = Endpointer(EndpointConfig(), T0)
    out = run(ep, [(False, 100), (True, 400)])
    assert out == [(400, E.SPEECH_START)]
    assert ep.t_speech_start_ns == T0 + 110 * 1_000_000


def test_eos_after_hangover_with_speculation() -> None:
    ep = Endpointer(EndpointConfig(), T0)
    out = run(ep, [(True, 500), (False, 400)])
    assert [ev for _, ev in out] == [E.SPEECH_START, E.SPECULATE, E.EOS]
    assert ep.done
    assert ep.last_speech_ns is not None
    assert ep.t_eos_ns == ep.last_speech_ns + 250 * 1_000_000


def test_resume_after_speculation() -> None:
    ep = Endpointer(EndpointConfig(), T0)
    assert events(ep, [(True, 500), (False, 150), (True, 200), (False, 300)]) == [
        E.SPEECH_START,
        E.SPECULATE,
        E.RESUMED,
        E.SPECULATE,
        E.EOS,
    ]


def test_adaptive_hangover_after_function_word() -> None:
    ep = Endpointer(EndpointConfig(), T0)
    ep.set_partial("set a timer for")
    assert ep.hangover_ms == 400
    out = run(ep, [(True, 500), (False, 500)])
    eos = [t for t, ev in out if ev == E.EOS]
    assert eos == [500 + 400]  # not 500 + 250
    assert ep.t_eos_ns == T0 + (500 + 400) * 1_000_000


def test_adaptive_hangover_after_number() -> None:
    assert wants_adaptive("set it to 21", function_words("en-US"))
    assert wants_adaptive("timer for twenty", function_words("en-US"))
    assert not wants_adaptive("turn off the lights", function_words("en-US"))
    assert not wants_adaptive("", function_words("en-US"))


def test_unknown_locale_uses_english() -> None:
    assert function_words("xx-YY") == function_words("en-US")


def test_cough_ignored() -> None:
    ep = Endpointer(EndpointConfig(), T0)
    assert events(ep, [(True, 150), (False, 400), (True, 100), (False, 400)]) == []
    assert not ep.speech_started


def test_speech_start_timeout() -> None:
    ep = Endpointer(EndpointConfig(), T0)
    out = run(ep, [(False, 5100)])
    assert out == [(5000, E.TIMEOUT)]
    assert ep.update(True, T0 + 6_000_000_000) == []  # done


def test_custom_timeout_for_reply_window() -> None:
    ep = Endpointer(EndpointConfig(), T0, speech_start_timeout_ms=8000)
    assert run(ep, [(False, 8100)]) == [(8000, E.TIMEOUT)]


def test_max_utterance() -> None:
    ep = Endpointer(EndpointConfig(max_utterance_ms=2000), T0)
    assert events(ep, [(True, 2500)]) == [E.SPEECH_START, E.MAX_LEN]


@settings(max_examples=100, deadline=None)
@given(st.lists(st.tuples(st.booleans(), st.integers(1, 80).map(lambda n: n * 10)), max_size=30))
def test_endpointer_invariants(pattern: list[tuple[bool, int]]) -> None:
    ep = Endpointer(EndpointConfig(), T0)
    evs = events(ep, pattern)
    terminal = [e for e in evs if e in (E.EOS, E.TIMEOUT, E.MAX_LEN)]
    assert len(terminal) <= 1
    if terminal:
        assert evs[-1] == terminal[0]
    assert evs.count(E.SPEECH_START) <= 1
    if E.EOS in evs:
        assert E.SPEECH_START in evs
        assert ep.t_eos_ns is not None
        assert ep.last_speech_ns is not None
        assert ep.t_eos_ns >= ep.last_speech_ns
        assert ep.t_speech_start_ns is not None
        assert ep.t_speech_start_ns <= ep.last_speech_ns
    # every RESUMED follows a SPECULATE
    open_spec = False
    for e in evs:
        if e == E.SPECULATE:
            open_spec = True
        if e == E.RESUMED:
            assert open_spec
            open_spec = False
