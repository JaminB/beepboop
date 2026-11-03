"""Tests for the ``beepboop-audio`` CLI skeleton."""

from __future__ import annotations

import pytest

import beepboop_audio
from beepboop_audio.cli import main


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
