"""Background samplers for ``bench ram`` (G4) and ``bench thermal`` (G7).

Both sample at 1 Hz on a daemon thread while an e2e run is in progress and
summarise on ``stop()``. Off the Pi they are *no-ops with a reason*: the RAM
sampler needs Linux ``/proc/meminfo`` (zram and OOM detection are
best-effort extras), the thermal sampler needs ``vcgencmd``. A sampler that
could not run reports ``available = False`` and never returns numbers.

OOM kills: the kernel log is scanned before and after the run (``dmesg``, or
``journalctl -k`` when ``dmesg`` is restricted) for ``Out of memory: Killed
process`` / ``oom-kill:`` lines; the per-container split comes from the
``oom_memcg=`` cgroup path on those lines.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from beepboop_bench.host import read_meminfo, read_temperature, read_throttled
from beepboop_bench.schema import RamProfile, ThermalSection
from beepboop_bench.stats import maybe_summarize

_MEMINFO = Path("/proc/meminfo")
_ZRAM_MM_STAT = Path("/sys/block/zram0/mm_stat")
_ZRAM_DISKSIZE = Path("/sys/block/zram0/disksize")

_OOM_LINE = re.compile(r"(Out of memory: Killed process|oom-kill:)")
_OOM_MEMCG = re.compile(r"oom_memcg=(?P<cg>[^,\s]+)")
_OOM_TASK = re.compile(r"task=(?P<task>[^,\s]+)")


class _Sampler:
    """1 Hz sampling loop on a daemon thread."""

    def __init__(self, interval_s: float = 1.0) -> None:
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None

    def _sample(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def start(self) -> None:
        self.started_at = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=type(self).__name__, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            self._sample()
            next_at += self.interval_s
            delay = next_at - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s * 2 + 1)
        self.stopped_at = time.monotonic()

    @property
    def duration_s(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.stopped_at if self.stopped_at is not None else time.monotonic()
        return end - self.started_at


# ---- RAM ------------------------------------------------------------------------


def read_zram() -> tuple[int, int] | None:
    """``(orig_data_size_bytes, disksize_bytes)`` of ``zram0`` or ``None``."""
    try:
        mm_stat = _ZRAM_MM_STAT.read_text(encoding="ascii").split()
        disksize = int(_ZRAM_DISKSIZE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if not mm_stat or disksize <= 0:
        return None
    return int(mm_stat[0]), disksize


def _kernel_log(runner: Callable[[list[str]], str | None]) -> tuple[list[str], str] | None:
    for cmd, source in (
        (["dmesg"], "dmesg"),
        (["journalctl", "-k", "--no-pager", "-o", "cat"], "journalctl -k"),
    ):
        if shutil.which(cmd[0]) is None:
            continue
        text = runner(cmd)
        if text is not None:
            return text.splitlines(), source
    return None


def _run(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)  # noqa: S603 fixed argv
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def parse_oom_lines(lines: list[str]) -> dict[str, int]:
    """Count OOM kills per container cgroup (or task name) from kernel log lines."""
    counts: dict[str, int] = {}
    for line in lines:
        if not _OOM_LINE.search(line):
            continue
        memcg = _OOM_MEMCG.search(line)
        task = _OOM_TASK.search(line)
        key = memcg.group("cg") if memcg else (task.group("task") if task else "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


@dataclass
class OomSnapshot:
    source: str | None
    counts: dict[str, int] = field(default_factory=dict[str, int])


def oom_snapshot(runner: Callable[[list[str]], str | None] = _run) -> OomSnapshot:
    found = _kernel_log(runner)
    if found is None:
        return OomSnapshot(source=None)
    lines, source = found
    return OomSnapshot(source=source, counts=parse_oom_lines(lines))


def oom_delta(before: OomSnapshot, after: OomSnapshot) -> dict[str, int]:
    keys = set(before.counts) | set(after.counts)
    return {
        k: after.counts.get(k, 0) - before.counts.get(k, 0)
        for k in keys
        if after.counts.get(k, 0) > before.counts.get(k, 0)
    }


class RamSampler(_Sampler):
    """1 Hz ``MemTotal - MemAvailable`` plus zram use; OOM kills from the kernel log."""

    def __init__(
        self, profile: Literal["default", "music"] = "default", interval_s: float = 1.0
    ) -> None:
        super().__init__(interval_s)
        self.profile: Literal["default", "music"] = profile
        self.used: list[int] = []
        self.zram_pct: list[float] = []
        self.mem_total: int = read_meminfo().get("MemTotal", 0)
        self.available = _MEMINFO.is_file() and self.mem_total > 0
        self.reason = (
            None
            if self.available
            else "RAM sampler needs Linux /proc/meminfo; skipped on this host"
        )
        self._oom_before: OomSnapshot | None = None

    def _sample(self) -> None:
        info = read_meminfo()
        total = info.get("MemTotal")
        avail = info.get("MemAvailable")
        if total is not None and avail is not None:
            self.used.append(total - avail)
        zram = read_zram()
        if zram is not None:
            orig, disksize = zram
            self.zram_pct.append(100.0 * orig / disksize)

    def start(self) -> None:
        if not self.available:
            return
        self._oom_before = oom_snapshot()
        super().start()

    def result(self) -> RamProfile | None:
        if not self.available or not self.used:
            return None
        after = oom_snapshot()
        before = self._oom_before or OomSnapshot(source=None)
        by_container = oom_delta(before, after) if after.source else {}
        return RamProfile(
            profile=self.profile,
            duration_s=self.duration_s,
            samples=len(self.used),
            mem_total_bytes=self.mem_total,
            peak_used_bytes=max(self.used),
            used_bytes=maybe_summarize(float(v) for v in self.used),
            zram_disksize_bytes=(read_zram() or (None, None))[1],
            zram_used_pct_peak=max(self.zram_pct) if self.zram_pct else None,
            oom_kills=sum(by_container.values()) if after.source else None,
            oom_by_container=by_container,
            oom_source=after.source,
        )


# ---- thermal ------------------------------------------------------------------


class ThermalSampler(_Sampler):
    """1 Hz ``vcgencmd measure_temp`` + ``get_throttled``."""

    def __init__(self, during: str = "e2e", interval_s: float = 1.0) -> None:
        super().__init__(interval_s)
        self.during = during
        self.temps: list[float] = []
        self.throttled_raw: list[int] = []
        self.available = shutil.which("vcgencmd") is not None
        self.reason = (
            None
            if self.available
            else "thermal sampler needs vcgencmd (Raspberry Pi); skipped on this host"
        )

    def _sample(self) -> None:
        temp = read_temperature()
        if temp is not None:
            self.temps.append(temp)
        state = read_throttled()
        if state is not None:
            self.throttled_raw.append(state.raw)

    def start(self) -> None:
        if not self.available:
            return
        super().start()

    def result(self) -> ThermalSection:
        if not self.available:
            return ThermalSection(status="skipped", reason=self.reason, during=self.during)
        if not self.throttled_raw:
            return ThermalSection(
                status="error", reason="vcgencmd returned nothing", during=self.during
            )
        union = 0
        for raw in self.throttled_raw:
            union |= raw
        return ThermalSection(
            status="measured",
            during=self.during,
            duration_s=self.duration_s,
            samples=len(self.throttled_raw),
            temp_c=maybe_summarize(self.temps),
            throttled_0x4_seen=any(raw & 0x4 for raw in self.throttled_raw),
            throttle_bits_union=union,
        )
