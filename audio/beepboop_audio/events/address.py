"""Where the event socket lives: ``BB_AUDIO_SOCKET`` parsing.

Production is always a unix socket (ARCHITECTURE §4.1)::

    BB_AUDIO_SOCKET=unix:///run/beepboop/audio.sock     # the default
    BB_AUDIO_SOCKET=/run/beepboop/audio.sock            # bare path, same thing

**Dev/test only:** Windows has no ``asyncio`` unix-socket server, so a
loopback TCP address can be selected *explicitly*::

    BB_AUDIO_SOCKET=tcp://127.0.0.1:8765

Only loopback hosts are accepted; the audio container has no network
(``network_mode: none``) and the protocol has no authentication, so a TCP
listener on any other interface would be a hole. There is no automatic
fallback: if unix sockets are unavailable and no ``tcp://`` address was
given, starting the server fails with a message saying so.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ENV_VAR = "BB_AUDIO_SOCKET"
DEFAULT_ADDRESS = "unix:///run/beepboop/audio.sock"


@dataclass(frozen=True, slots=True)
class UnixAddress:
    path: Path

    def __str__(self) -> str:
        return f"unix://{self.path.as_posix()}"


@dataclass(frozen=True, slots=True)
class TcpAddress:
    """Loopback TCP, for development and tests only."""

    host: str
    port: int

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"tcp://{host}:{self.port}"


SocketAddress = UnixAddress | TcpAddress


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_address(value: str) -> SocketAddress:
    """Parse a ``BB_AUDIO_SOCKET`` value; raises ``ValueError`` when invalid."""
    value = value.strip()
    if not value:
        raise ValueError(f"{ENV_VAR} is empty")
    if value.startswith("unix://"):
        path = value.removeprefix("unix://")
        if not path:
            raise ValueError(f"{ENV_VAR}: unix:// address has no path")
        return UnixAddress(Path(path))
    if value.startswith("tcp://"):
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if host is None or port is None:
            raise ValueError(f"{ENV_VAR}: tcp:// address needs host:port")
        if not _is_loopback(host):
            raise ValueError(f"{ENV_VAR}: tcp:// is for dev/tests and must be loopback")
        return TcpAddress(host, port)
    if "://" in value:
        raise ValueError(f"{ENV_VAR}: unsupported scheme in {value!r}")
    return UnixAddress(Path(value))


def address_from_env(env: Mapping[str, str] | None = None) -> SocketAddress:
    """The socket address from ``BB_AUDIO_SOCKET``, defaulting to the spec path."""
    source = os.environ if env is None else env
    return parse_address(source.get(ENV_VAR) or DEFAULT_ADDRESS)


__all__ = [
    "DEFAULT_ADDRESS",
    "ENV_VAR",
    "SocketAddress",
    "TcpAddress",
    "UnixAddress",
    "address_from_env",
    "parse_address",
]
