"""BB_AUDIO_SOCKET parsing, JSON Schema determinism, and the light-import guarantee."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from beepboop_audio.events import (
    DEFAULT_ADDRESS,
    TcpAddress,
    UnixAddress,
    address_from_env,
    parse_address,
    protocol_schema,
    schema_json,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("unix:///run/beepboop/audio.sock", UnixAddress(Path("/run/beepboop/audio.sock"))),
        ("/run/beepboop/audio.sock", UnixAddress(Path("/run/beepboop/audio.sock"))),
        ("tcp://127.0.0.1:8765", TcpAddress("127.0.0.1", 8765)),
        ("tcp://localhost:1", TcpAddress("localhost", 1)),
        ("tcp://[::1]:8765", TcpAddress("::1", 8765)),
    ],
)
def test_parse_address(value: str, expected: object) -> None:
    assert parse_address(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "unix://", "tcp://0.0.0.0:8765", "tcp://192.168.1.5:8765", "tcp://127.0.0.1", "http://x"],
)
def test_parse_address_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="BB_AUDIO_SOCKET"):
        parse_address(value)


def test_address_from_env_defaults_to_spec_path() -> None:
    assert DEFAULT_ADDRESS == "unix:///run/beepboop/audio.sock"
    assert address_from_env({}) == UnixAddress(Path("/run/beepboop/audio.sock"))
    assert address_from_env({"BB_AUDIO_SOCKET": "tcp://127.0.0.1:9"}) == TcpAddress("127.0.0.1", 9)


def test_address_str_roundtrips() -> None:
    for value in ("unix:///run/beepboop/audio.sock", "tcp://127.0.0.1:9", "tcp://[::1]:9"):
        assert str(parse_address(value)) == value


def test_schema_is_deterministic_and_complete() -> None:
    assert schema_json() == schema_json()
    schema = protocol_schema()
    assert schema["protocol"] == 1
    text = json.dumps(schema)
    for name in ("WakeEvent", "SttFinalEvent", "CapturePinEvent", "ProfilesSyncEvent"):
        assert name in text
    mapping = schema["audio_to_core"]["discriminator"]["mapping"]
    assert "hello" in mapping
    assert "stt.final" in mapping


def test_events_import_without_heavy_audio_dependencies() -> None:
    """Core imports the protocol; it must not drag in numpy or speech runtimes."""
    code = (
        "import sys, beepboop_audio.events\n"
        "heavy = {'numpy', 'sherpa_onnx', 'pymicro_wakeword', 'soundfile'} & set(sys.modules)\n"
        "print(sorted(heavy))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"
