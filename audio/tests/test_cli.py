"""Tests for the ``beepboop-audio`` CLI skeleton."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

import beepboop_audio
from beepboop_audio.cli import main
from beepboop_protocol.log import REDACTED, configure_logging, redact_secrets


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out == f"beepboop-audio {beepboop_audio.__version__}\n"


def test_command_is_required() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_speech_runtimes_import() -> None:
    """The pinned speech runtimes install and import on this platform."""
    import pymicro_wakeword
    import sherpa_onnx  # pyright: ignore[reportMissingTypeStubs] no stubs upstream

    assert sherpa_onnx.__name__ == "sherpa_onnx"
    assert pymicro_wakeword.__name__ == "pymicro_wakeword"


def test_cli_installs_the_shared_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    """Hard rule 5 in the audio service: main() configures the shared redacting setup."""
    main(["version"])
    capsys.readouterr()
    processors = structlog.get_config()["processors"]
    assert redact_secrets in processors


def test_audio_logs_redact_pins() -> None:
    stream = io.StringIO()
    configure_logging("debug", stream=stream)
    structlog.get_logger("beepboop_audio.pin").info("pin.captured", spoken_pin="1234", digits=4)
    line = json.loads(stream.getvalue())
    assert line["spoken_pin"] == REDACTED
    assert "1234" not in stream.getvalue()


def test_serve_options_parse() -> None:
    from beepboop_audio.cli import build_parser

    args = build_parser().parse_args(
        ["serve", "--fake-mic", "x.wav", "--socket", "tcp://127.0.0.1:0", "--fake-providers"]
    )
    assert args.command == "serve"
    assert args.fake_providers
    assert str(args.fake_mic) == "x.wav"


def test_serve_without_models_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BB_CONFIG", str(tmp_path / "none.yaml"))
    code = main(["serve", "--models", str(tmp_path), "--socket", "tcp://127.0.0.1:0"])
    assert code == 1
    assert "beepboop-audio:" in capsys.readouterr().err
