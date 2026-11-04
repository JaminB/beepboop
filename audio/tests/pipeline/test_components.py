"""Mixer, earcons, front end, PIN, enrollment gates, ring buffer (VOICE_PIPELINE §3-§13)."""

from __future__ import annotations

import numpy as np
import pytest
from argon2 import PasswordHasher

from beepboop_audio.frames import RingBuffer, samples_to_ms, to_float32, to_int16
from beepboop_audio.pipeline import pin
from beepboop_audio.pipeline.earcons import EARCON_NAMES, Earcons, synthesize
from beepboop_audio.pipeline.enroll import EnrollmentSession, Quality, gate, measure
from beepboop_audio.pipeline.frontend import FrontEnd
from beepboop_audio.pipeline.leds import (
    FakeMuteButton,
    LedState,
    LogLed,
    NoMuteButton,
    RecordingLed,
)
from beepboop_audio.pipeline.mixer import DUCK_GAIN, PERIOD_SAMPLES, Mixer, SpeechItem
from beepboop_audio.pipeline.vad import VadFramer
from beepboop_audio.testing import EnergyVad, FakeWake, silence, tone, voice
from beepboop_protocol import EarconName

# ------------------------------------------------------------------ ring --------


def test_ring_wraps_and_returns_newest() -> None:
    ring = RingBuffer(seconds=0.01)  # 160 samples
    ring.push(np.arange(100, dtype=np.int16))
    ring.push(np.arange(100, 200, dtype=np.int16))
    assert len(ring) == 160
    assert ring.last(5).tolist() == [195, 196, 197, 198, 199]
    assert ring.last(1000)[0] == 40
    ring.push(np.arange(1000, dtype=np.int16))
    assert ring.last(3).tolist() == [997, 998, 999]


def test_ring_clear_zeroes() -> None:
    ring = RingBuffer(seconds=0.01)
    ring.push(np.full(160, 7, dtype=np.int16))
    ring.clear()
    assert len(ring) == 0
    assert ring.last(10).size == 0
    assert not ring._buf.any()  # pyright: ignore[reportPrivateUsage] - zeroed, not just forgotten


def test_int16_float_roundtrip() -> None:
    x = np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)
    back = to_int16(to_float32(x))
    assert np.abs(back.astype(int) - x.astype(int)).max() <= 1


# ------------------------------------------------------------------ earcons -----


def test_every_earcon_exists_and_is_bounded() -> None:
    earcons = Earcons()
    assert set(EARCON_NAMES) == set(EarconName.__args__)
    for name in EARCON_NAMES:
        pcm = earcons.get(name)
        ms = samples_to_ms(len(pcm))
        assert ms <= (1000 if name == "alarm" else 250), name
        peak = np.abs(pcm.astype(np.float32)).max() / 32768.0
        assert peak <= 10 ** (-12 / 20) + 1e-3  # -12 dBFS


def test_earcon_theme_override(tmp_path: object) -> None:
    from pathlib import Path

    import soundfile  # pyright: ignore[reportMissingTypeStubs]

    theme = Path(str(tmp_path))
    # A theme is a household's own asset; tests create one to read back.
    soundfile.write(str(theme / "ack.wav"), np.zeros(800, dtype=np.float32), 8000)
    earcons = Earcons(theme)
    assert len(earcons.get("ack")) == 1600  # resampled 8 kHz -> 16 kHz
    assert len(earcons.get("wake")) == len(synthesize("wake"))


# ------------------------------------------------------------------ mixer -------


def test_mixer_plays_speech_then_reports_end() -> None:
    times = iter(range(0, 10**12, 20_000_000))
    mixer = Mixer(clock=lambda: next(times))
    started: list[int] = []
    ended: list[tuple[int, bool]] = []
    item = SpeechItem(
        "t", "u", on_start=started.append, on_end=lambda t, n, c: ended.append((n, c))
    )
    item.append(tone(440, 50))
    item.close()
    mixer.enqueue(item)
    out = [mixer.next_period() for _ in range(5)]
    assert any(np.abs(p).max() > 0 for p in out)
    assert started
    assert ended == [(800, False)]
    assert not mixer.speech_busy


def test_mixer_underrun_waits_for_more_pcm() -> None:
    mixer = Mixer()
    item = SpeechItem("t", "u")
    mixer.enqueue(item)
    assert not mixer.next_period().any()
    item.append(tone(440, 20))
    assert mixer.next_period().any()
    assert mixer.speech_busy  # not closed yet
    item.close()
    mixer.next_period()
    assert not mixer.speech_busy


def test_mixer_flush_reports_cancelled() -> None:
    mixer = Mixer()
    ended: list[bool] = []
    a = SpeechItem("t1", "u1", on_end=lambda _t, _n, c: ended.append(c))
    b = SpeechItem("t2", "u2", on_end=lambda _t, _n, c: ended.append(c))
    for it in (a, b):
        it.append(tone(440, 200))
        it.close()
        mixer.enqueue(it)
    assert mixer.flush_speech("t1") == [a]
    assert ended == [True]
    assert mixer.speech_busy


def test_alert_ducks_speech() -> None:
    mixer = Mixer()
    item = SpeechItem("t", "u")
    item.append(np.full(PERIOD_SAMPLES * 4, 10000, dtype=np.int16))
    item.close()
    mixer.enqueue(item)
    plain = int(mixer.next_period()[0])
    mixer.play_alert(np.zeros(PERIOD_SAMPLES, dtype=np.int16), turn_id="a", loop=True)
    ducked = int(mixer.next_period()[0])
    assert plain == 10000
    assert ducked == pytest.approx(10000 * DUCK_GAIN, abs=2)
    assert mixer.stop_alerts("a") == 1


def test_earcon_mixes_over_speech_and_clips() -> None:
    mixer = Mixer()
    mixer.play_earcon(np.full(PERIOD_SAMPLES, 30000, dtype=np.int16))
    mixer.play_earcon(np.full(PERIOD_SAMPLES, 30000, dtype=np.int16))
    assert int(mixer.next_period()[0]) == 32767


def test_queue_ms() -> None:
    mixer = Mixer()
    item = SpeechItem("t", "u")
    item.append(silence(500))
    mixer.enqueue(item)
    assert mixer.queue_ms() == 500


# ------------------------------------------------------------------ front end ---


def make_frontend(gate_on: bool = True) -> tuple[FrontEnd, RingBuffer]:
    ring = RingBuffer()
    return FrontEnd(FakeWake(), VadFramer(EnergyVad()), ring, vad_gate=gate_on), ring


def marked(pcm: np.ndarray) -> np.ndarray:
    out = pcm.copy()
    out[0] = FakeWake.MARK
    return out


def test_frontend_hit_needs_arming_and_vad() -> None:
    fe, _ = make_frontend()
    speech = voice(200, 400)
    frames = [speech[i : i + 160] for i in range(0, len(speech), 160)]
    for f in frames[:-1]:
        assert fe.process(f, 1).wake is None  # type: ignore[union-attr]
    out = fe.process(marked(frames[-1]), 42)
    assert out is not None
    assert out.wake is None
    fe.arm(True)
    out = fe.process(marked(frames[-1]), 43)
    assert out is not None
    assert out.wake is not None
    assert out.wake.t_wake_ns == 43  # stamped with the capture time


def test_frontend_vad_gate_blocks_silent_hits() -> None:
    fe, _ = make_frontend()
    fe.arm(True)
    for _ in range(40):
        fe.process(silence(10), 1)
    out = fe.process(marked(silence(10)), 2)
    assert out is not None
    assert out.wake is None
    assert fe.gated_hits == 1
    fe2, _ = make_frontend(gate_on=False)
    fe2.arm(True)
    out2 = fe2.process(marked(silence(10)), 2)
    assert out2 is not None
    assert out2.wake is not None


def test_frontend_mute_drops_everything() -> None:
    fe, ring = make_frontend()
    fe.process(voice(200, 10), 1)
    assert len(ring) == 160
    fe.set_muted(True)
    assert len(ring) == 0
    assert fe.process(voice(200, 10), 2) is None
    assert len(ring) == 0
    fe.set_muted(False)
    assert fe.process(voice(200, 10), 3) is not None


def test_leds_and_mute_buttons() -> None:
    rec = RecordingLed()
    for s in (LedState.IDLE, LedState.IDLE, LedState.LISTENING):
        rec.show(s)
    assert rec.states == [LedState.IDLE, LedState.LISTENING]
    LogLed().show(LedState.MUTED)
    assert NoMuteButton().poll() is None
    assert FakeMuteButton(True).poll() is True


# ------------------------------------------------------------------ PIN ---------

_hasher = PasswordHasher()


@pytest.mark.parametrize(
    ("text", "digits"),
    [
        ("four seven one nine", b"4719"),
        ("Oh, one, two.", b"012"),
        ("for to ate won", b"4281"),
        ("4719", b"4719"),
        ("nine-nine", b"99"),
    ],
)
def test_words_to_digits(text: str, digits: bytes) -> None:
    assert bytes(pin.words_to_digits(text) or b"") == digits


def test_words_to_digits_rejects_words() -> None:
    assert pin.words_to_digits("four seven banana") is None


def test_verify_ok_mismatch_and_zeroing() -> None:
    h = _hasher.hash("4719")
    buf = bytearray(b"4719")
    assert pin.verify(h, buf, min_len=4, max_len=8) == "ok"
    assert buf == bytearray(4)  # zeroed
    assert pin.verify(h, bytearray(b"4710"), min_len=4, max_len=8) == "mismatch"
    assert pin.verify(h, bytearray(b"471"), min_len=4, max_len=8) == "mismatch"  # too short
    assert pin.verify("$argon2id$garbage", bytearray(b"4719"), min_len=4, max_len=8) == "mismatch"


def test_outcome_for_text() -> None:
    h = _hasher.hash("1234")
    assert pin.outcome_for_text("one two three four", h, min_len=4, max_len=8) == "ok"
    assert pin.outcome_for_text("", h, min_len=4, max_len=8) == "timeout"
    assert pin.outcome_for_text("hello there", h, min_len=4, max_len=8) == "mismatch"


# ------------------------------------------------------------------ enrollment --


def test_measure_snr_clipping_ratio() -> None:
    clip = voice(200, 1000, amplitude=0.3)
    noise = voice(900, 500, amplitude=0.003)
    q = measure(clip, noise, speech_frames=80, total_frames=100)
    assert q.snr_db > 30
    assert q.clipping == 0.0
    assert q.speech_ratio == 0.8
    assert q.duration_ms == 1000
    clipped = np.full(16000, 32767, dtype=np.int16)
    assert measure(clipped, silence(500), 100, 100).clipping == 1.0
    assert measure(clip, silence(500), 100, 100).snr_db == 99.0  # digital silence before


def test_gates() -> None:
    good = Quality(snr_db=30, clipping=0.0, speech_ratio=0.9, duration_ms=2000, speech_ms=1500)
    assert gate(good, mode="free", wake_fired=False) is None
    assert gate(good, mode="wake", wake_fired=False) == "no_wake"
    assert gate(good, mode="wake", wake_fired=True) is None
    short = Quality(30, 0.0, 0.9, 500, 400)
    assert gate(short, mode="free", wake_fired=True) == "too_short"
    assert gate(short, mode="wake", wake_fired=True) == "too_short"
    assert gate(Quality(10, 0.0, 0.9, 2000, 1500), mode="free", wake_fired=False) == "noisy"
    assert gate(Quality(30, 0.01, 0.9, 2000, 1500), mode="free", wake_fired=False) == "clipping"
    assert gate(Quality(30, 0.0, 0.3, 2000, 600), mode="free", wake_fired=False) == "no_speech"


def test_consistency_accepts_second_try() -> None:
    s = EnrollmentSession("s", "p", "free")
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert s.consistency(a) is None
    s.accepted.append(a)
    assert s.consistency(b) == "different"
    assert s.consistency(b) is None  # genuine variety is allowed on the second try
    assert [s.next_index(), s.next_index()] == [0, 1]
