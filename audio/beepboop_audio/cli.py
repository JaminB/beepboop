"""The ``beepboop-audio`` command line.

The audio container image stays lean, so this CLI uses ``argparse`` rather
than Typer. CLAUDE.md §9 lists its commands; ``serve [--fake-mic <wav>]`` is
added together with the service loop.

Logging goes through the shared redacting setup in ``beepboop_protocol.log``
(CLAUDE.md hard rule 5, ADR 0019): the audio service handles spoken PINs, so it
must redact exactly as core does.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from beepboop_audio import __version__
from beepboop_protocol.log import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (separate from ``main`` so tests can use it)."""
    parser = argparse.ArgumentParser(
        prog="beepboop-audio",
        description="beepboop audio service (wake, VAD, speaker ID, STT, TTS).",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("version", help="print the installed beepboop-audio version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    configure_logging()
    if args.command == "version":
        sys.stdout.write(f"beepboop-audio {__version__}\n")
        return 0
    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":
    raise SystemExit(main())
