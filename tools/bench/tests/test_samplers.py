"""RAM / thermal samplers: parsing, deltas, and the off-Pi no-op."""

from __future__ import annotations

import platform

from beepboop_bench.samplers import (
    OomSnapshot,
    RamSampler,
    ThermalSampler,
    oom_delta,
    oom_snapshot,
    parse_oom_lines,
)

OOM_LINES = [
    "[12.1] kernel: random line",
    "[13.2] Out of memory: Killed process 1234 (python3) total-vm:1000kB, anon-rss:900kB, "
    "oom_memcg=/docker/abc123,task_memcg=/docker/abc123,task=python3,pid=1234,uid=1000",
    "[14.0] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,mems_allowed=0,"
    "oom_memcg=/docker/def456,task_memcg=/docker/def456,task=hass,pid=99,uid=0",
    "[15.0] Out of memory: Killed process 7 (sshd) total-vm:1kB",
]


def test_parse_oom_lines_groups_by_container() -> None:
    counts = parse_oom_lines(OOM_LINES)
    assert counts == {"/docker/abc123": 1, "/docker/def456": 1, "unknown": 1}


def test_oom_delta_counts_only_new_kills() -> None:
    before = OomSnapshot(source="dmesg", counts={"/docker/abc123": 1})
    after = OomSnapshot(source="dmesg", counts={"/docker/abc123": 2, "/docker/def456": 1})
    assert oom_delta(before, after) == {"/docker/abc123": 1, "/docker/def456": 1}
    assert oom_delta(after, after) == {}


def test_oom_snapshot_without_kernel_log_is_unavailable() -> None:
    snap = oom_snapshot(lambda cmd: None)
    assert snap.source is None
    assert snap.counts == {}


def test_samplers_noop_off_pi() -> None:
    ram = RamSampler()
    thermal = ThermalSampler()
    if platform.system() != "Linux":
        assert not ram.available
        assert ram.reason is not None
        assert "/proc/meminfo" in ram.reason
        ram.start()
        ram.stop()
        assert ram.result() is None
    if not thermal.available:
        assert thermal.reason is not None
        assert "vcgencmd" in thermal.reason
        thermal.start()
        thermal.stop()
        section = thermal.result()
        assert section.status == "skipped"
        assert section.throttled_0x4_seen is None


def test_thermal_result_from_samples() -> None:
    thermal = ThermalSampler()
    thermal.available = True
    thermal.temps = [50.0, 60.0, 70.0]
    thermal.throttled_raw = [0x0, 0x50000, 0x4]
    thermal.started_at = 0.0
    thermal.stopped_at = 3.0
    section = thermal.result()
    assert section.status == "measured"
    assert section.throttled_0x4_seen is True
    assert section.throttle_bits_union == 0x50004
    assert section.temp_c is not None
    assert section.temp_c.max == 70.0
    thermal.throttled_raw = [0x0, 0x50000]
    assert thermal.result().throttled_0x4_seen is False
