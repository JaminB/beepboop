"""ROADMAP v0.1 acceptance 8: no audio bytes on disk after a turn (ADR 0018).

Scans one or more roots (``/srv/beepboop`` and ``/data`` on the Pi runner) for files that
look like audio: by extension (``.wav``, ``.flac``, ``.ogg``, ``.mp3``, ``.opus``, ``.raw``,
``.pcm``, ``.npy``) or by magic bytes (RIFF/WAVE, fLaC, OggS, ID3). The model weights
folder (``models/``) and the fixture corpus (``tools/bench/corpus``) are skipped because
they are inputs, not captures.

Usage::

    python tools/ci/no_audio_on_disk.py /srv/beepboop /data

Exit status is 1 when any audio-looking file is found, 0 otherwise. A missing root is
reported and skipped (the x86 CI has no ``/srv/beepboop``).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

AUDIO_SUFFIXES: frozenset[str] = frozenset(
    {".wav", ".flac", ".ogg", ".mp3", ".opus", ".raw", ".pcm", ".npy", ".aiff", ".m4a"}
)
#: (offset, bytes) pairs; RIFF files also need "WAVE" at offset 8.
MAGIC: tuple[tuple[int, bytes], ...] = ((0, b"fLaC"), (0, b"OggS"), (0, b"ID3"), (8, b"WAVE"))
SKIP_DIR_NAMES: frozenset[str] = frozenset({"models", "corpus", ".git", "node_modules"})


def looks_like_audio(path: Path) -> bool:
    if path.suffix.lower() in AUDIO_SUFFIXES:
        return True
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    return any(head[off : off + len(sig)] == sig for off, sig in MAGIC if off == 0)


def scan(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if any(part in SKIP_DIR_NAMES for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_file() and not path.is_symlink() and looks_like_audio(path):
            yield path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args(argv)
    found: list[Path] = []
    for root in args.roots:
        if not root.is_dir():
            print(f"skip: {root} is not a directory")
            continue
        found.extend(scan(root))
    for path in found:
        print(f"audio on disk: {path}")
    print(f"{len(found)} audio-looking file(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
