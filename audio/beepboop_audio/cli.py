"""The ``beepboop-audio`` command line.

The audio container image stays lean, so this CLI uses ``argparse`` rather
than Typer. CLAUDE.md §9 lists its commands:

* ``version``: print the installed package version.
* ``serve [--fake-mic <wav>]``: run the audio service. Without ``--fake-mic``
  it opens ALSA (``audio.alsa_device`` / ``BB_ALSA_DEVICE``); with it a WAV
  file is the microphone and playback is discarded (development on any host).
  ``--fake-providers`` swaps every model for the deterministic fakes in
  :mod:`beepboop_audio.testing` (no model files needed).

Logging goes through the shared redacting setup in ``beepboop_protocol.log``
(CLAUDE.md hard rule 5, ADR 0019): the audio service handles spoken PINs, so it
must redact exactly as core does.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import structlog

from beepboop_audio import __version__
from beepboop_protocol import address_from_env, parse_address
from beepboop_protocol.log import configure_logging

log = structlog.get_logger(__name__)

HEARTBEAT = Path("/run/beepboop/audio.heartbeat")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (separate from ``main`` so tests can use it)."""
    parser = argparse.ArgumentParser(
        prog="beepboop-audio",
        description="beepboop audio service (wake, VAD, speaker ID, STT, TTS).",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("version", help="print the installed beepboop-audio version")
    serve = commands.add_parser("serve", help="run the audio service")
    serve.add_argument("--fake-mic", type=Path, help="feed this WAV file instead of ALSA")
    serve.add_argument("--config", type=Path, help="household config (default $BB_CONFIG)")
    serve.add_argument("--models", type=Path, help="models root (default $BB_MODELS_DIR)")
    serve.add_argument("--socket", help="event socket address (default $BB_AUDIO_SOCKET)")
    serve.add_argument(
        "--fake-providers",
        action="store_true",
        help="use the deterministic fakes instead of the speech models",
    )
    serve.add_argument("--log-level", default="info")
    return parser


def _serve(args: argparse.Namespace) -> int:
    from beepboop_audio.config import load_config
    from beepboop_audio.models import ModelMissingError, ModelStore
    from beepboop_audio.pipeline.io import (
        CaptureDevice,
        FileCapture,
        NullPlayback,
        PlaybackDevice,
    )
    from beepboop_audio.service import (
        AudioService,
        Providers,
        load_cohort,
        load_providers,
        load_vad,
    )
    from beepboop_audio.testing import (
        EnergyVad,
        FakeEmbedder,
        FakeStt,
        FakeTts,
        FakeWake,
    )

    config = load_config(args.config)
    if args.models is not None:
        config = config.model_copy(update={"models_dir": args.models})
    store = ModelStore(config.models_dir)
    address = parse_address(args.socket) if args.socket else address_from_env()
    try:
        if args.fake_providers:
            providers = Providers(FakeWake(), FakeEmbedder(), FakeStt(), FakeTts())
            vad = EnergyVad()
            cohort = None
        else:
            providers = load_providers(config, store)
            vad = load_vad(store)
            speaker = providers.speaker
            cohort = load_cohort(store, speaker.model_id) if speaker is not None else None
        capture: CaptureDevice
        playback: PlaybackDevice
        if args.fake_mic is not None:
            capture = FileCapture(args.fake_mic, channel=0)
            playback = NullPlayback()
        else:
            from beepboop_audio.pipeline.alsa import AlsaCapture, AlsaPlayback

            a = config.audio
            capture = AlsaCapture(
                a.alsa_device, channels=a.capture_channels, channel=a.capture_channel
            )
            playback = AlsaPlayback(a.alsa_device, channels=a.playback_channels)
    except (ModelMissingError, RuntimeError, OSError) as exc:
        log.error("audio.start_failed", error=str(exc))
        sys.stderr.write(f"beepboop-audio: {exc}\n")
        return 1
    heartbeat = HEARTBEAT if HEARTBEAT.parent.is_dir() else None
    service = AudioService(
        config,
        providers,
        vad=vad,
        capture=capture,
        playback=playback,
        cohort=cohort,
        heartbeat=heartbeat,
        theme_dir=config.models_dir / "earcons",
    )

    async def run() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
        return await service.run(address, stop)

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "version":
        configure_logging()
        sys.stdout.write(f"beepboop-audio {__version__}\n")
        return 0
    if args.command == "serve":
        configure_logging(str(args.log_level).upper())
        return _serve(args)
    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":
    raise SystemExit(main())
