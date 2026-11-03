"""Builders for tests: a fake Pi host, a latency summary, and a result shell.

Importable by any package's tests (``from beepboop_bench.testing import ...``);
nothing here touches hardware or models.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beepboop_bench.host import HostFacts
from beepboop_bench.schema import BenchResult, CorpusInfo, Measurement, Sections
from beepboop_bench.stats import Summary


def host_facts(*, pi: bool = True, ram: str | None = "4gb") -> HostFacts:
    """A Raspberry Pi 4 (``pi=True``) or a Windows x86 box."""
    mem = {"4gb": 3_900_000_000, "8gb": 8_100_000_000, None: None}[ram]
    return HostFacts(
        hostname="pi",
        system="Linux" if pi else "Windows",
        release="6.6",
        machine="aarch64" if pi else "AMD64",
        python="3.12.10",
        cpu_count=4,
        board_model="Raspberry Pi 4 Model B Rev 1.5" if pi else None,
        mem_total_bytes=mem,
        is_raspberry_pi=pi,
        pi_ram_variant=ram if pi else None,
        vcgencmd_available=pi,
        throttled_at_start=None,
        captured_at=datetime(2026, 11, 2, tzinfo=UTC),
    )


def summary(p50: float, p90: float | None = None, p95: float | None = None) -> Summary:
    p90 = p50 if p90 is None else p90
    p95 = p90 if p95 is None else p95
    return Summary(n=10, p50=p50, p90=p90, p95=p95, min=p50, max=p95, mean=p50)


def result(
    *,
    sections: Sections | None = None,
    pi: bool = True,
    ram: str | None = "4gb",
    synthetic: bool = False,
    measurement: Measurement = "gate",
) -> BenchResult:
    return BenchResult(
        measurement=measurement,
        label="test",
        command="bench all --gates",
        host=host_facts(pi=pi, ram=ram),
        started_at=datetime(2026, 11, 2, tzinfo=UTC),
        corpus=CorpusInfo(
            path="corpus",
            synthetic=synthetic,
            recorded_through="synthetic-tts" if synthetic else "xvf3800",
            n_clips=200,
            n_speakers=6,
            soak_hours=24.0,
        ),
        sections=sections or Sections(),
    )
