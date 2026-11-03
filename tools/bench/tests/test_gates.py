"""G1-G7 are encoded exactly as ROADMAP states them, and judged honestly."""

from __future__ import annotations

from beepboop_bench import gates
from beepboop_bench.schema import (
    E2ESection,
    LlmScenario,
    LlmSection,
    RamProfile,
    RamSection,
    ScenarioSummary,
    Sections,
    SpeakerSection,
    SpeakerVariant,
    SttSection,
    SttThreads,
    ThermalSection,
    TtsSection,
    TtsVoice,
    WakeSection,
)
from beepboop_bench.testing import result, summary

GIB = 2**30


def _thresholds(gate: gates.Gate) -> dict[str, tuple[str, float | bool | str]]:
    return {c.path: (c.op, c.threshold) for c in gate.checks}


def test_table_matches_roadmap_numbers() -> None:
    """One assertion per ROADMAP cell, so a drift in either place fails here."""
    assert [g.gate for g in gates.GATES] == ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    g1 = _thresholds(gates.G1)
    assert g1["sections.speaker.fp32.clip_2s_embed_ms.p95"] == ("<", 200.0)
    g2 = _thresholds(gates.G2)
    assert g2["sections.stt.threads_2.rtf.p50"] == ("<", 0.6)
    assert g2["sections.stt.threads_2.final_tail_ms.p50"] == ("<=", 100.0)
    assert g2["sections.stt.threads_2.final_tail_ms.p90"] == ("<=", 130.0)
    g3 = _thresholds(gates.G3)
    assert g3["sections.llm.scenarios.warm_first.ttft_ms.p50"] == ("<=", 600.0)
    assert g3["sections.llm.scenarios.warm_second_tool_result.ttft_ms.p50"] == ("<=", 500.0)
    assert gates.G3.arbiter is not None
    assert gates.G3.arbiter.path == "sections.e2e.tool.first_speech_ms.p50"
    assert gates.G3.arbiter.threshold == 2000.0
    g4 = _thresholds(gates.G4)
    assert g4["sections.ram.default.peak_used_bytes"] == ("<", 3.2 * GIB)
    assert g4["sections.ram.default.zram_used_pct_peak"] == ("<", 25.0)
    assert g4["sections.ram.default.oom_kills"] == ("==", 0)
    assert g4["sections.ram.music.oom_kills"] == ("==", 0)
    g5 = _thresholds(gates.G5)
    assert g5["sections.wake.false_accepts_per_24h"] == ("<=", 0.5)
    assert g5["sections.wake.false_reject_pct_3m"] == ("<=", 5.0)
    g6 = _thresholds(gates.G6)
    assert g6["sections.e2e.fast_path.first_speech_ms.p50"] == ("<=", 500.0)
    assert g6["sections.e2e.command_1s.first_speech_ms.p50"] == ("<=", 500.0)
    assert g6["sections.e2e.no_tool.first_audio_ms.p95"] == ("<", 1000.0)
    assert g6["sections.e2e.tool.first_audio_ms.p95"] == ("<", 1000.0)
    assert g6["sections.e2e.no_tool.first_speech_ms.p50"] == ("<", 1500.0)
    assert g6["sections.e2e.no_tool.first_speech_ms.p90"] == ("<", 1950.0)
    assert g6["sections.e2e.tool.first_speech_ms.p50"] == ("<", 2000.0)
    assert g6["sections.e2e.tool.first_speech_ms.p90"] == ("<", 2600.0)
    assert g6["sections.tts.default_voice_first_chunk_ms.p50"] == ("<=", 200.0)
    g7 = _thresholds(gates.G7)
    assert g7["sections.thermal.throttled_0x4_seen"] == ("==", False)
    assert {c.threshold for c in gates.G7.preconditions} == {"e2e", 1200.0}
    assert {c.threshold for c in gates.G4.preconditions} == {"4gb", 86400.0}
    assert {c.threshold for c in gates.G5.preconditions} == {True, 24.0}


def test_resolve_walks_nested_mappings() -> None:
    data = {"a": {"b": {"c": 1.5}}, "flat": True}
    assert gates.resolve(data, "a.b.c") == 1.5
    assert gates.resolve(data, "a.b.missing") is None
    assert gates.resolve(data, "a.b.c.deeper") is None
    assert gates.resolve(data, "flat") is True


def test_missing_metric_is_skipped_never_pass() -> None:
    report = gates.evaluate(result(), measurement="gate")
    assert all(g.status == "skipped" for g in report.gates)
    assert not report.all_passed
    for g in report.gates:
        for c in g.checks:
            assert c.passed is None
            assert c.observed is None


def _passing_sections() -> Sections:
    return Sections(
        speaker=SpeakerSection(
            status="measured",
            threads=2,
            concurrent_stt=True,
            fp32=SpeakerVariant(
                model_id="campp",
                precision="fp32",
                embedding_dim=512,
                clip_2s_embed_ms=summary(120, 150, 190),
            ),
        ),
        stt=SttSection(
            status="measured",
            threads_2=SttThreads(threads=2, rtf=summary(0.4, 0.5), final_tail_ms=summary(80, 100)),
        ),
        tts=TtsSection(
            status="measured",
            default_voice="en_US-lessac-low",
            voices={
                "en_US-lessac-low": TtsVoice(
                    voice="en_US-lessac-low", sample_rate=16000, first_chunk_ms=summary(150, 180)
                )
            },
        ),
        wake=WakeSection(
            status="measured",
            model_id="hey_beepboop",
            stock_model=True,
            soak_hours=24.0,
            false_accepts_per_24h=0.0,
            false_reject_pct_3m=3.0,
        ),
        llm=LlmSection(
            status="measured",
            scenarios={
                "warm_first": LlmScenario(n=6, ttft_ms=summary(550, 600)),
                "warm_second_tool_result": LlmScenario(n=6, ttft_ms=summary(450, 480)),
            },
        ),
        e2e=E2ESection(
            status="measured",
            ha_running=True,
            offline=True,
            fast_path=ScenarioSummary(n=10, first_speech_ms=summary(450, 550)),
            command_1s=ScenarioSummary(n=10, first_speech_ms=summary(400, 500)),
            no_tool=ScenarioSummary(
                n=10, first_audio_ms=summary(900, 950, 990), first_speech_ms=summary(1400, 1500)
            ),
            tool=ScenarioSummary(
                n=10, first_audio_ms=summary(900, 950, 990), first_speech_ms=summary(1900, 2100)
            ),
        ),
        ram=RamSection(
            status="measured",
            default=RamProfile(
                profile="default",
                duration_s=90000,
                samples=90000,
                mem_total_bytes=4 * GIB,
                peak_used_bytes=3 * GIB,
                zram_used_pct_peak=10.0,
                oom_kills=0,
            ),
            music=RamProfile(
                profile="music",
                duration_s=90000,
                samples=90000,
                mem_total_bytes=4 * GIB,
                peak_used_bytes=3 * GIB,
                zram_used_pct_peak=12.0,
                oom_kills=0,
            ),
        ),
        thermal=ThermalSection(
            status="measured", during="e2e", duration_s=1300, samples=1300, throttled_0x4_seen=False
        ),
    )


def test_all_gates_pass_on_a_complete_pi_run() -> None:
    report = gates.evaluate(result(sections=_passing_sections()), measurement="gate")
    assert {g.gate: g.status for g in report.gates} == dict.fromkeys(
        ["G1", "G2", "G3", "G4", "G5", "G6", "G7"], "pass"
    )
    assert report.all_passed


def test_p90_rule_fails_a_gate() -> None:
    sections = _passing_sections()
    assert sections.e2e is not None
    sections.e2e.fast_path = ScenarioSummary(n=10, first_speech_ms=summary(450, 700))  # p90 > 650
    report = gates.evaluate(result(sections=sections), measurement="gate")
    g6 = next(g for g in report.gates if g.gate == "G6")
    assert g6.status == "fail"
    assert not report.all_passed


def test_g3_arbiter_rescues_slow_ttft() -> None:
    sections = _passing_sections()
    assert sections.llm is not None
    sections.llm.scenarios["warm_first"] = LlmScenario(n=6, ttft_ms=summary(900))
    report = gates.evaluate(result(sections=sections), measurement="gate")
    g3 = next(g for g in report.gates if g.gate == "G3")
    assert g3.status == "pass"
    assert g3.arbiter is not None
    assert g3.arbiter.passed is True
    sections.e2e = (
        sections.e2e.model_copy(
            update={
                "tool": ScenarioSummary(
                    n=10, first_audio_ms=summary(900), first_speech_ms=summary(2500, 2600)
                )
            }
        )
        if sections.e2e
        else None
    )
    g3 = next(
        g
        for g in gates.evaluate(result(sections=sections), measurement="gate").gates
        if g.gate == "G3"
    )
    assert g3.status == "fail"


def test_g3_arbiter_decides_even_when_ttft_passes() -> None:
    """ROADMAP: "the arbiter is tool-turn first_speech p50 <= 2.0 s"; a measured arbiter decides."""
    sections = _passing_sections()
    assert sections.e2e is not None
    sections.e2e.tool = ScenarioSummary(
        n=10, first_audio_ms=summary(900), first_speech_ms=summary(2100, 2200)
    )
    g3 = next(
        g
        for g in gates.evaluate(result(sections=sections), measurement="gate").gates
        if g.gate == "G3"
    )
    assert all(c.passed is True for c in g3.checks)
    assert g3.arbiter is not None
    assert g3.arbiter.passed is False
    assert g3.status == "fail"
    # Without an arbiter sample the TTFT checks decide.
    sections.e2e.tool = ScenarioSummary(n=10, first_audio_ms=summary(900))
    g3 = next(
        g
        for g in gates.evaluate(result(sections=sections), measurement="gate").gates
        if g.gate == "G3"
    )
    assert g3.arbiter is not None
    assert g3.arbiter.passed is None
    assert g3.status == "pass"


def test_eight_gb_pi_is_recorded_alongside_never_a_pass() -> None:
    """ROADMAP: all gates are measured on the 4 GB Pi; the 8 GB run is recorded alongside."""
    report = gates.evaluate(result(sections=_passing_sections(), ram="8gb"), measurement="gate")
    assert not report.all_passed
    assert all(g.status == "incomplete" for g in report.gates)
    unmet = [p for p in report.run_preconditions if p.passed is not True]
    assert [p.path for p in unmet] == ["host.pi_ram_variant"]
    assert all("4 GB" in (g.note or "") for g in report.gates)


def test_unmet_precondition_is_incomplete_not_pass() -> None:
    sections = _passing_sections()
    assert sections.wake is not None
    sections.wake.stock_model = False
    report = gates.evaluate(result(sections=sections), measurement="gate")
    g5 = next(g for g in report.gates if g.gate == "G5")
    assert g5.status == "incomplete"
    assert sections.ram is not None
    assert sections.ram.music is not None
    sections.ram.music.duration_s = 3600
    g4 = next(
        g
        for g in gates.evaluate(result(sections=sections), measurement="gate").gates
        if g.gate == "G4"
    )
    assert g4.status == "incomplete"


def test_off_pi_run_can_never_pass() -> None:
    report = gates.evaluate(
        result(sections=_passing_sections(), pi=False), measurement="not a gate result"
    )
    assert not report.all_passed
    assert all(g.status != "pass" for g in report.gates)
    assert any("run preconditions unmet" in (g.note or "") for g in report.gates)
    assert report.measurement == "not a gate result"


def test_synthetic_corpus_can_never_pass() -> None:
    report = gates.evaluate(
        result(sections=_passing_sections(), synthetic=True), measurement="not a gate result"
    )
    assert not report.all_passed
    assert any(
        p.label.startswith("corpus is real") and p.passed is False for p in report.run_preconditions
    )
