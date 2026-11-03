"""Facts about the machine a bench ran on, and the "is this a Pi?" test.

Every result file carries :class:`HostFacts` so a number can never be read
without knowing which board, RAM size, kernel and throttle state produced
it (CLAUDE.md §13: say where a number was measured). The Pi-specific probes
(``/proc/device-tree/model``, ``vcgencmd``) degrade to ``None`` elsewhere;
nothing here guesses.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

#: ``vcgencmd get_throttled`` bits (OPERATIONS §8).
THROTTLE_UNDER_VOLTAGE = 0x1
THROTTLE_ARM_FREQ_CAPPED = 0x2
THROTTLE_CURRENTLY = 0x4
THROTTLE_SOFT_TEMP_LIMIT = 0x8
#: Bits 16-19 mirror 0x1..0x8 as "has happened since boot".
THROTTLE_SINCE_BOOT_SHIFT = 16

_DEVICE_TREE_MODEL = Path("/proc/device-tree/model")
_MEMINFO = Path("/proc/meminfo")

#: Gate runs are defined on a 4 GB Pi 4; MemTotal there is about 3.7-3.9 GiB.
PI_4GB_MEMTOTAL_RANGE = (3.0 * 2**30, 4.5 * 2**30)
PI_8GB_MEMTOTAL_RANGE = (7.0 * 2**30, 8.5 * 2**30)


class ThrottleState(BaseModel):
    """Decoded ``vcgencmd get_throttled`` value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: int
    under_voltage: bool
    arm_freq_capped: bool
    currently_throttled: bool
    soft_temp_limit: bool
    under_voltage_since_boot: bool
    arm_freq_capped_since_boot: bool
    throttled_since_boot: bool
    soft_temp_limit_since_boot: bool

    @classmethod
    def from_raw(cls, raw: int) -> ThrottleState:
        since = raw >> THROTTLE_SINCE_BOOT_SHIFT
        return cls(
            raw=raw,
            under_voltage=bool(raw & THROTTLE_UNDER_VOLTAGE),
            arm_freq_capped=bool(raw & THROTTLE_ARM_FREQ_CAPPED),
            currently_throttled=bool(raw & THROTTLE_CURRENTLY),
            soft_temp_limit=bool(raw & THROTTLE_SOFT_TEMP_LIMIT),
            under_voltage_since_boot=bool(since & THROTTLE_UNDER_VOLTAGE),
            arm_freq_capped_since_boot=bool(since & THROTTLE_ARM_FREQ_CAPPED),
            throttled_since_boot=bool(since & THROTTLE_CURRENTLY),
            soft_temp_limit_since_boot=bool(since & THROTTLE_SOFT_TEMP_LIMIT),
        )


class HostFacts(BaseModel):
    """What the result file records about the machine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hostname: str
    system: str
    release: str
    machine: str
    python: str
    cpu_count: int | None
    board_model: str | None
    mem_total_bytes: int | None
    is_raspberry_pi: bool
    pi_ram_variant: str | None
    vcgencmd_available: bool
    throttled_at_start: ThrottleState | None
    captured_at: datetime


def read_board_model() -> str | None:
    """``/proc/device-tree/model`` (e.g. ``Raspberry Pi 4 Model B Rev 1.5``) or ``None``."""
    try:
        return _DEVICE_TREE_MODEL.read_bytes().rstrip(b"\x00").decode("utf-8", "replace")
    except OSError:
        return None


def read_meminfo(path: Path = _MEMINFO) -> dict[str, int]:
    """Parse ``/proc/meminfo`` into bytes per key; empty when unavailable."""
    try:
        text = path.read_text(encoding="ascii")
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        unit = parts[1].lower() if len(parts) > 1 else ""
        out[key.strip()] = value * 1024 if unit == "kb" else value
    return out


def vcgencmd(*args: str) -> str | None:
    """Run ``vcgencmd`` and return its stdout, or ``None`` when it is unavailable."""
    exe = shutil.which("vcgencmd")
    if exe is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 fixed executable, fixed args
            [exe, *args], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def parse_throttled(text: str) -> ThrottleState:
    """Parse ``throttled=0x50000`` (or a bare hex number)."""
    _, _, value = text.strip().rpartition("=")
    return ThrottleState.from_raw(int(value, 16))


def read_throttled() -> ThrottleState | None:
    text = vcgencmd("get_throttled")
    return parse_throttled(text) if text else None


def parse_temperature(text: str) -> float:
    """Parse ``temp=45.0'C`` into Celsius."""
    _, _, value = text.strip().partition("=")
    return float(value.rstrip("'C"))


def read_temperature() -> float | None:
    text = vcgencmd("measure_temp")
    return parse_temperature(text) if text else None


def is_raspberry_pi(board_model: str | None, system: str, machine: str) -> bool:
    """A gate host is Linux on aarch64 whose device tree says Raspberry Pi."""
    return (
        system == "Linux"
        and machine == "aarch64"
        and board_model is not None
        and board_model.startswith("Raspberry Pi")
    )


def pi_ram_variant(mem_total_bytes: int | None) -> str | None:
    """``"4gb"`` / ``"8gb"`` by MemTotal, else ``None``."""
    if mem_total_bytes is None:
        return None
    low, high = PI_4GB_MEMTOTAL_RANGE
    if low <= mem_total_bytes <= high:
        return "4gb"
    low, high = PI_8GB_MEMTOTAL_RANGE
    if low <= mem_total_bytes <= high:
        return "8gb"
    return None


def collect_host_facts() -> HostFacts:
    """Gather :class:`HostFacts` for this machine (cheap; a few file reads)."""
    system = platform.system()
    machine = platform.machine()
    board = read_board_model()
    mem = read_meminfo().get("MemTotal")
    throttled = read_throttled()
    return HostFacts(
        hostname=socket.gethostname(),
        system=system,
        release=platform.release(),
        machine=machine,
        python=platform.python_version(),
        cpu_count=os.cpu_count(),
        board_model=board,
        mem_total_bytes=mem,
        is_raspberry_pi=is_raspberry_pi(board, system, machine),
        pi_ram_variant=pi_ram_variant(mem),
        vcgencmd_available=shutil.which("vcgencmd") is not None,
        throttled_at_start=throttled,
        captured_at=datetime.now(UTC),
    )
