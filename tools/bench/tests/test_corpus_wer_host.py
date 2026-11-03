"""Corpus manifest validation, WER, host facts parsing."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from beepboop_bench.audio import (
    SAMPLE_RATE,
    frames,
    last_seconds,
    read_wav,
    trim_trailing_silence,
    write_wav,
)
from beepboop_bench.corpus import Clip, Corpus, CorpusManifest, Speaker, write_manifest
from beepboop_bench.host import (
    ThrottleState,
    parse_temperature,
    parse_throttled,
    pi_ram_variant,
    read_meminfo,
)
from beepboop_bench.wer import normalize, wer_pct, word_errors

# ---- corpus ---------------------------------------------------------------------


def test_real_corpus_must_be_xvf3800() -> None:
    with pytest.raises(ValueError, match="XVF3800"):
        CorpusManifest(name="x", synthetic=False, recorded_through="other-mic")
    m = CorpusManifest(name="x", synthetic=False, recorded_through="xvf3800")
    assert m.real
    assert not CorpusManifest(name="x", synthetic=True, recorded_through="synthetic-tts").real


def test_clip_validation() -> None:
    with pytest.raises(ValueError, match="transcript"):
        Clip(id="c", path="c.wav", kind="command", speaker="a")
    with pytest.raises(ValueError, match="speaker"):
        Clip(id="c", path="c.wav", kind="wake")
    with pytest.raises(ValueError, match="unknown speaker"):
        CorpusManifest(
            name="x",
            synthetic=True,
            recorded_through="synthetic-tts",
            clips=[Clip(id="c", path="c.wav", kind="command", speaker="ghost", transcript="hi")],
        )
    with pytest.raises(ValueError, match="duplicate clip"):
        CorpusManifest(
            name="x",
            synthetic=True,
            recorded_through="synthetic-tts",
            speakers=[Speaker(id="a")],
            clips=[
                Clip(id="c", path="c.wav", kind="command", speaker="a", transcript="hi"),
                Clip(id="c", path="d.wav", kind="command", speaker="a", transcript="hi"),
            ],
        )


def test_corpus_load_round_trip(tmp_path: Path) -> None:
    write_wav(tmp_path / "clips" / "a.wav", np.zeros(SAMPLE_RATE, dtype=np.float32))
    manifest = CorpusManifest(
        name="t",
        synthetic=True,
        recorded_through="synthetic-tts",
        speakers=[Speaker(id="a", kind="synthetic")],
        clips=[
            Clip(
                id="a",
                path="clips/a.wav",
                kind="command",
                speaker="a",
                transcript="hi",
                scenario="tool",
            )
        ],
    )
    write_manifest(tmp_path, manifest)
    corpus = Corpus.load(tmp_path)
    assert corpus.manifest == manifest
    assert corpus.manifest.commands("tool")[0].id == "a"
    assert corpus.info().n_clips == 1
    (tmp_path / "clips" / "a.wav").unlink()
    with pytest.raises(FileNotFoundError, match="missing clip"):
        Corpus.load(tmp_path)
    with pytest.raises(FileNotFoundError, match="record_fixture"):
        Corpus.load(tmp_path / "nowhere")


# ---- audio helpers ----------------------------------------------------------------


def test_wav_round_trip_and_helpers(tmp_path: Path) -> None:
    tone = (0.5 * np.sin(np.arange(SAMPLE_RATE) * 2 * np.pi * 440 / SAMPLE_RATE)).astype(np.float32)
    padded = np.concatenate([tone, np.zeros(SAMPLE_RATE // 2, dtype=np.float32)])
    write_wav(tmp_path / "t.wav", padded)
    back = read_wav(tmp_path / "t.wav")
    assert len(back) == len(padded)
    assert np.max(np.abs(back - padded)) < 1e-3
    assert len(trim_trailing_silence(padded)) == pytest.approx(SAMPLE_RATE, abs=2)
    assert len(last_seconds(tone, 2.0)) == 2 * SAMPLE_RATE
    assert len(frames(tone)) == 100


# ---- WER ------------------------------------------------------------------------


def test_wer() -> None:
    assert normalize("Turn OFF, the lights!") == ["turn", "off", "the", "lights"]
    assert word_errors("turn off the lights", "turn off the lights") == (0, 4)
    assert word_errors("turn off the lights", "turn of the light") == (2, 4)
    assert word_errors("a b", "") == (2, 2)
    assert wer_pct([("a b c d", "a b c x"), ("e f", "e f")]) == pytest.approx(100 / 6)
    assert wer_pct([]) is None


# ---- host -----------------------------------------------------------------------


def test_throttle_bits() -> None:
    state = parse_throttled("throttled=0x50005")
    assert state == ThrottleState.from_raw(0x50005)
    assert state.under_voltage
    assert state.currently_throttled
    assert state.under_voltage_since_boot
    assert state.throttled_since_boot
    assert not state.arm_freq_capped
    assert not state.soft_temp_limit
    assert not parse_throttled("0x0").currently_throttled
    assert parse_temperature("temp=45.3'C") == 45.3


def test_meminfo_and_ram_variant(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(
        "MemTotal:        3884304 kB\nMemAvailable:    2000000 kB\nHugePages_Total:       0\n"
    )
    info = read_meminfo(tmp_path / "meminfo")
    assert info["MemTotal"] == 3884304 * 1024
    assert info["HugePages_Total"] == 0
    assert read_meminfo(tmp_path / "missing") == {}
    assert pi_ram_variant(info["MemTotal"]) == "4gb"
    assert pi_ram_variant(8 * 2**30) == "8gb"
    assert pi_ram_variant(16 * 2**30) is None
    assert pi_ram_variant(None) is None
