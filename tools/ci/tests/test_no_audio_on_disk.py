"""``tools/ci/no_audio_on_disk.py``: the fs scan for ROADMAP v0.1 acceptance 8."""

from __future__ import annotations

from pathlib import Path

from no_audio_on_disk import looks_like_audio, main, scan


def test_scan_finds_audio_by_suffix_and_magic_and_skips_models(tmp_path: Path) -> None:
    (tmp_path / "data" / "sub").mkdir(parents=True)
    (tmp_path / "data" / "clip.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    (tmp_path / "data" / "mystery.bin").write_bytes(b"fLaC\x00\x00")
    (tmp_path / "data" / "beepboop.db").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "weights.npy").write_bytes(b"\x93NUMPY")
    found = sorted(p.name for p in scan(tmp_path))
    assert found == ["clip.wav", "mystery.bin"]
    assert looks_like_audio(tmp_path / "data" / "beepboop.db") is False
    assert main([str(tmp_path)]) == 1
    assert main([str(tmp_path / "data" / "sub"), str(tmp_path / "nope")]) == 0
