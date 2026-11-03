"""``--ram-from``: G4's two 24 h soaks are judged together, never invented."""

from __future__ import annotations

from pathlib import Path

import pytest

from beepboop_bench import gates
from beepboop_bench import run as runner
from beepboop_bench.schema import (
    NOT_A_GATE_LABEL,
    E2ESection,
    Measurement,
    RamProfile,
    RamSection,
    Sections,
)
from beepboop_bench.testing import result

GIB = 2**30


def _profile(name: str, *, hours: float = 25.0) -> RamProfile:
    return RamProfile(
        profile="music" if name == "music" else "default",
        duration_s=hours * 3600,
        samples=int(hours * 3600),
        mem_total_bytes=4 * GIB,
        peak_used_bytes=3 * GIB,
        zram_used_pct_peak=10.0,
        oom_kills=0,
    )


def _write(
    tmp_path: Path,
    name: str,
    *,
    sections: Sections | None = None,
    pi: bool = True,
    ram: str | None = "4gb",
    measurement: Measurement = "gate",
) -> Path:
    path = tmp_path / name
    runner.write_result(path, result(sections=sections, pi=pi, ram=ram, measurement=measurement))
    return path


def test_import_merges_the_other_profile(tmp_path: Path) -> None:
    earlier = _write(
        tmp_path,
        "default.json",
        sections=Sections(ram=RamSection(status="measured", default=_profile("default"))),
    )
    this = result(
        sections=Sections(
            e2e=E2ESection(status="measured", ha_running=True),  # run precondition
            ram=RamSection(status="measured", music=_profile("music")),
        )
    )
    sections = this.sections
    runner.import_ram_profiles(sections, this, earlier, "gate")
    assert sections.ram is not None
    assert sections.ram.default is not None
    assert sections.ram.default.duration_s == 25 * 3600
    assert sections.ram.music is not None  # this run's own profile is kept
    assert any("ram.default imported from default.json" in n for n in sections.ram.notes)
    g4 = next(g for g in gates.evaluate(this, measurement="gate").gates if g.gate == "G4")
    assert g4.status == "pass"


def test_import_into_a_run_without_a_ram_section(tmp_path: Path) -> None:
    earlier = _write(
        tmp_path,
        "both.json",
        sections=Sections(
            ram=RamSection(status="measured", default=_profile("default"), music=_profile("music"))
        ),
    )
    this = result()
    runner.import_ram_profiles(this.sections, this, earlier, "gate")
    assert this.sections.ram is not None
    assert this.sections.ram.status == "measured"
    assert this.sections.ram.default is not None
    assert this.sections.ram.music is not None


def test_import_refuses_a_non_gate_file_into_a_gate_run(tmp_path: Path) -> None:
    earlier = _write(
        tmp_path,
        "dev.json",
        pi=False,
        measurement=NOT_A_GATE_LABEL,
        sections=Sections(ram=RamSection(status="measured", default=_profile("default"))),
    )
    this = result()
    with pytest.raises(runner.GateRefusedError, match="labelled"):
        runner.import_ram_profiles(this.sections, this, earlier, "gate")


def test_import_refuses_another_board(tmp_path: Path) -> None:
    earlier = _write(
        tmp_path,
        "8gb.json",
        ram="8gb",
        sections=Sections(ram=RamSection(status="measured", default=_profile("default"))),
    )
    this = result()
    with pytest.raises(runner.GateRefusedError, match="recorded on"):
        runner.import_ram_profiles(this.sections, this, earlier, "gate")


def test_import_needs_a_profile(tmp_path: Path) -> None:
    earlier = _write(tmp_path, "empty.json")
    this = result()
    with pytest.raises(ValueError, match="no RAM profile"):
        runner.import_ram_profiles(this.sections, this, earlier, "gate")
