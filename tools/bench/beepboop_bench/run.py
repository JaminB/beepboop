"""Orchestration for ``beepboop bench``: options, refusal, running, writing, tables.

* :func:`gate_refusal` is the rule from the task: gates are only evaluated on
  a Raspberry Pi (linux/aarch64) with a real XVF3800 corpus. ``--allow-non-pi``
  lets a development run through, labelled "not a gate result".
* :func:`run` executes the requested sections in the OPERATIONS §11 order and
  returns a :class:`BenchResult`; :func:`write_result` stores it under
  ``tools/bench/results/<date>-<host>.json`` (plus ``-synthetic`` for synthetic
  corpora, and ``-N`` when the name is taken, so nothing is overwritten).
* :func:`render` prints the p50/p90/p95 tables and the gate verdicts.
"""

from __future__ import annotations

import socket
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.table import Table

from beepboop.log import get_logger
from beepboop_bench import gates as gates_mod
from beepboop_bench.corpus import Corpus
from beepboop_bench.host import HostFacts, collect_host_facts
from beepboop_bench.models import load_manifest, manifest_path, models_root
from beepboop_bench.schema import (
    NOT_A_GATE_LABEL,
    SYNTHETIC_LABEL,
    BenchResult,
    GateReport,
    Measurement,
    RamSection,
    ScenarioSummary,
    Sections,
)
from beepboop_bench.stats import Summary

if TYPE_CHECKING:
    from beepboop_bench.e2e import LlmFactory

log = get_logger(__name__)

#: ROADMAP M0: "All measured on a 4 GB Pi 4 ... The 8 GB run is recorded alongside."
GATE_RAM_VARIANT = "4gb"

Sub = Literal["speaker", "stt", "tts", "wake", "aec", "endpoint", "llm", "e2e", "ram", "thermal"]
SUBS: tuple[Sub, ...] = (
    "speaker",
    "stt",
    "tts",
    "wake",
    "aec",
    "endpoint",
    "llm",
    "e2e",
    "ram",
    "thermal",
)


@dataclass(frozen=True)
class Options:
    corpus: Path
    models: Path = field(default_factory=models_root)
    threads: int = 2
    host: str | None = None
    out: Path | None = None
    gates: bool = False
    allow_non_pi: bool = False
    concurrency: int = 2
    realtime: bool = True
    max_clips: int | None = None
    max_turns: int | None = None
    duration_s: float | None = None
    llm_live: bool = False
    llm_pairs: int = 6
    llm_cold_gap_s: float = 0.0
    ha_running: bool | None = None
    offline: bool | None = None
    aec_device: str | None = None
    ram_profile: Literal["default", "music"] = "default"
    ram_from: tuple[Path, ...] = ()
    label: str | None = None
    concurrent_stt: bool = True


class GateRefusedError(RuntimeError):
    """Raised when ``--gates`` is requested somewhere gates may not be judged."""


def gate_refusal(host: HostFacts, corpus: Corpus | None) -> str | None:
    """Why this host/corpus may not produce a gate result, or ``None`` if it may."""
    reasons: list[str] = []
    if not host.is_raspberry_pi:
        reasons.append(
            f"host is {host.system}/{host.machine} ({host.board_model or 'no device tree'}), "
            "not a Raspberry Pi"
        )
    if corpus is None:
        reasons.append("no corpus loaded")
    elif not corpus.manifest.real:
        reasons.append(
            f"corpus is {corpus.manifest.recorded_through}"
            f"{' (synthetic)' if corpus.manifest.synthetic else ''}, "
            "not recorded through the XVF3800"
        )
    return "; ".join(reasons) if reasons else None


def measurement_for(host: HostFacts, corpus: Corpus | None) -> tuple[Measurement, str]:
    """``"gate"`` only on the 4 GB Pi with the real corpus (ROADMAP: "All measured on a 4 GB
    Pi 4 ... The 8 GB run is recorded alongside"); everything else says so in its label."""
    refusal = gate_refusal(host, corpus)
    if refusal is None:
        if host.pi_ram_variant == GATE_RAM_VARIANT:
            return "gate", f"gate run on {host.board_model} ({host.pi_ram_variant})"
        return NOT_A_GATE_LABEL, (
            f"{NOT_A_GATE_LABEL}: {host.board_model} with {host.pi_ram_variant or 'unknown RAM'}, "
            "recorded alongside the 4 GB gate run (ROADMAP judges G1-G7 on the 4 GB board)"
        )
    if corpus is not None and corpus.manifest.synthetic:
        return NOT_A_GATE_LABEL, SYNTHETIC_LABEL
    return NOT_A_GATE_LABEL, f"{NOT_A_GATE_LABEL}: {refusal}"


def import_ram_profiles(
    sections: Sections, result: BenchResult, path: Path, measurement: Measurement
) -> None:
    """Merge the RAM profiles of an earlier result file into this run (``--ram-from``).

    G4 needs two 24 h soaks (``default`` and ``music``) judged together; each
    ``bench ram`` run writes its own file, so the second run (or ``bench all``)
    imports the other. Imported profiles are never invented: they are copied
    whole from the file, which must come from the same host and RAM variant,
    and from a gate run when this run is one. Every import is noted on the
    section.
    """
    other = BenchResult.model_validate_json(path.read_text(encoding="utf-8"))
    ram = other.sections.ram
    if ram is None or (ram.default is None and ram.music is None):
        msg = f"{path} has no RAM profile to import"
        raise ValueError(msg)
    if measurement == "gate" and other.measurement != "gate":
        msg = f"refusing to import RAM profiles from {path}: it is labelled {other.measurement!r}"
        raise GateRefusedError(msg)
    same_board = (other.host.hostname, other.host.pi_ram_variant) == (
        result.host.hostname,
        result.host.pi_ram_variant,
    )
    if not same_board:
        msg = (
            f"refusing to import RAM profiles from {path}: recorded on {other.host.hostname} "
            f"({other.host.pi_ram_variant}), this run is on {result.host.hostname} "
            f"({result.host.pi_ram_variant})"
        )
        raise GateRefusedError(msg)
    target = sections.ram or RamSection(status="measured")
    updates: dict[str, object] = {"status": "measured", "reason": None}
    notes = list(target.notes)
    for name in ("default", "music"):
        profile = getattr(ram, name)
        if profile is None:
            continue
        updates[name] = profile
        notes.append(
            f"ram.{name} imported from {path.name} "
            f"({profile.duration_s:.0f} s sampled, run started {other.started_at.isoformat()})"
        )
    updates["notes"] = notes
    sections.ram = target.model_copy(update=updates)


def host_label(opts: Options, host: HostFacts) -> str:
    if opts.host:
        return opts.host
    if host.is_raspberry_pi and host.pi_ram_variant:
        return f"pi4-{host.pi_ram_variant}"
    return socket.gethostname().lower().replace(" ", "-")


def results_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "results"


def result_path(opts: Options, result: BenchResult, host: HostFacts) -> Path:
    if opts.out is not None:
        return opts.out
    date = result.started_at.date().isoformat()
    suffix = "-synthetic" if (result.corpus is not None and result.corpus.synthetic) else ""
    base = results_dir() / f"{date}-{host_label(opts, host)}{suffix}"
    candidate = base.with_suffix(".json")
    n = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{n}").with_suffix(".json")
        n += 1
    return candidate


def write_result(path: Path, result: BenchResult) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.to_json(), encoding="utf-8", newline="\n")
    return path


# ---- running --------------------------------------------------------------------


def _llm_factory() -> LlmFactory | None:
    try:
        import boto3

        from beepboop.providers.llm.bedrock.provider import (
            BedrockProvider,
            BedrockSettings,
            make_client,
        )
    except ImportError:
        return None
    settings = BedrockSettings.from_manifest(credentials="ambient")
    region: str = boto3.Session().region_name or settings.region

    def make() -> BedrockProvider:
        return BedrockProvider(make_client(region), settings.model_fast)

    return make


def run(subs: Sequence[Sub], opts: Options) -> BenchResult:
    """Run the named sections and return the result (nothing written yet)."""
    from beepboop_bench.aec import bench_aec
    from beepboop_bench.e2e import E2EOptions, bench_e2e
    from beepboop_bench.llm import bench_llm
    from beepboop_bench.samplers import RamSampler, ThermalSampler
    from beepboop_bench.stages import (
        bench_endpoint,
        bench_speaker,
        bench_stt,
        bench_tts,
        bench_wake,
        resolve_models,
    )

    host = collect_host_facts()
    corpus = Corpus.load(opts.corpus) if opts.corpus.exists() else None
    if opts.gates:
        refusal = gate_refusal(host, corpus)
        if refusal is not None and not opts.allow_non_pi:
            raise GateRefusedError(
                f"refusing to evaluate gates: {refusal} (use --allow-non-pi for a development run)"
            )
    measurement, label = measurement_for(host, corpus)
    if opts.label:
        label = f"{label}; {opts.label}"
    manifest = load_manifest(manifest_path(opts.models))
    models = resolve_models(manifest, opts.models)
    result = BenchResult(
        measurement=measurement,
        label=label,
        command="bench "
        + (" ".join(subs) if len(subs) < len(SUBS) else "all")
        + (" --gates" if opts.gates else ""),
        host=host,
        started_at=datetime.now(UTC),
        corpus=corpus.info() if corpus else None,
        models=dict(sorted(models.ids.items())),
    )
    sections = Sections()
    want = set(subs)
    if corpus is None and want & {"speaker", "stt", "wake", "endpoint", "e2e", "ram", "thermal"}:
        result.notes.append(f"no corpus at {opts.corpus}: corpus-driven sections skipped")

    def e2e_opts(duration: float | None = None) -> E2EOptions:
        return E2EOptions(
            concurrency=opts.concurrency,
            realtime=opts.realtime,
            threads=opts.threads,
            llm_live=opts.llm_live,
            ha_running=opts.ha_running,
            offline=opts.offline,
            max_turns=opts.max_turns,
            duration_s=duration if duration is not None else opts.duration_s,
        )

    if "speaker" in want and corpus:
        log.info("bench.section", section="speaker")
        sections.speaker = bench_speaker(
            corpus,
            models,
            threads=opts.threads,
            concurrent_stt=opts.concurrent_stt,
            max_clips=opts.max_clips,
        )
    if "stt" in want and corpus:
        log.info("bench.section", section="stt")
        sections.stt = bench_stt(corpus, models, max_clips=opts.max_clips)
    if "tts" in want:
        log.info("bench.section", section="tts")
        sections.tts = bench_tts(models, threads=opts.threads)
    if "wake" in want and corpus:
        log.info("bench.section", section="wake")
        sections.wake = bench_wake(corpus, models)
    if "aec" in want:
        log.info("bench.section", section="aec")
        sections.aec = bench_aec(
            models, device=opts.aec_device, scratch=Path(tempfile.gettempdir())
        )
    if "endpoint" in want and corpus:
        log.info("bench.section", section="endpoint")
        sections.endpoint = bench_endpoint(corpus, models)
    if "llm" in want:
        log.info("bench.section", section="llm")
        sections.llm = bench_llm(
            live=opts.llm_live,
            pairs=opts.llm_pairs,
            cold_gap_s=opts.llm_cold_gap_s,
            host=host_label(opts, host),
            gate=measurement == "gate",
        )
    factory = _llm_factory() if opts.llm_live else None
    if "e2e" in want and corpus and not ({"ram", "thermal"} & want):
        log.info("bench.section", section="e2e")
        sections.e2e = bench_e2e(corpus, models, e2e_opts(), llm_factory=factory)
    if ("ram" in want or "thermal" in want) and corpus:
        log.info("bench.section", section="e2e+samplers")
        ram = RamSampler(opts.ram_profile)
        thermal = ThermalSampler("e2e")
        if "ram" in want:
            ram.start()
        if "thermal" in want:
            thermal.start()
        duration = (
            opts.duration_s
            if opts.duration_s is not None
            else (1200.0 if "thermal" in want else None)
        )
        sections.e2e = bench_e2e(corpus, models, e2e_opts(duration), llm_factory=factory)
        if "ram" in want:
            ram.stop()
            profile = ram.result()
            if profile is None:
                sections.ram = RamSection(status="skipped", reason=ram.reason or "no samples")
            elif opts.ram_profile == "music":
                sections.ram = RamSection(status="measured", music=profile)
            else:
                sections.ram = RamSection(status="measured", default=profile)
            sections.ram.notes.append(
                "G4 needs 24 h with the default profile and again with music on"
            )
        if "thermal" in want:
            thermal.stop()
            sections.thermal = thermal.result()
    for path in opts.ram_from:
        log.info("bench.ram_import", path=str(path))
        import_ram_profiles(sections, result, path, measurement)
    result.sections = sections
    result.finished_at = datetime.now(UTC)
    if opts.gates:
        result.gates = gates_mod.evaluate(result, measurement=measurement)
    return result


# ---- rendering --------------------------------------------------------------------


def _fmt(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:,.3f}" if unit == "" and abs(value) < 10 else f"{value:,.1f}{unit}"


def _row(table: Table, name: str, s: Summary | None, unit: str = " ms") -> None:
    if s is None:
        table.add_row(name, "—", "—", "—", "—", "0")
    else:
        table.add_row(
            name,
            _fmt(s.p50, unit),
            _fmt(s.p90, unit),
            _fmt(s.p95, unit),
            _fmt(s.max, unit),
            str(s.n),
        )


def render(result: BenchResult, console: Console | None = None) -> None:
    console = console or Console()
    console.print(f"[bold]{result.command}[/bold]  {result.label}")
    console.print(
        f"host: {result.host.hostname} {result.host.system}/{result.host.machine} "
        f"{result.host.board_model or ''}"
    )
    s = result.sections
    table = Table(
        title="stage latencies (ROADMAP statistic rule: p50 meets, p90 <= 1.3x, p95 reported)"
    )
    for col in ("metric", "p50", "p90", "p95", "max", "n"):
        table.add_column(col, justify="right" if col != "metric" else "left")
    if s.speaker and s.speaker.fp32:
        _row(table, "speaker fp32 embed, 1 s clip", s.speaker.fp32.clip_1s_embed_ms)
        _row(table, "speaker fp32 embed, 2 s clip", s.speaker.fp32.clip_2s_embed_ms)
    for n, st in (
        ("1", s.stt.threads_1 if s.stt else None),
        ("2", s.stt.threads_2 if s.stt else None),
    ):
        if st:
            _row(table, f"stt RTF, {n} thread(s)", st.rtf, "")
            _row(table, f"stt final tail, {n} thread(s)", st.final_tail_ms)
    if s.tts:
        for voice in s.tts.voices.values():
            _row(table, f"tts first chunk {voice.voice}", voice.first_chunk_ms)
            _row(table, f"tts RTF {voice.voice}", voice.rtf, "")
    if s.wake:
        _row(table, "wake hit latency", s.wake.hit_latency_ms)
    if s.llm:
        for name, sc in s.llm.scenarios.items():
            _row(table, f"llm ttft {name}", sc.ttft_ms)
    if s.e2e:
        for scenario in ("fast_path", "command_1s", "no_tool", "tool"):
            summary: ScenarioSummary | None = getattr(s.e2e, scenario)
            if summary:
                _row(table, f"e2e {scenario} first_audio", summary.first_audio_ms)
                _row(table, f"e2e {scenario} first_speech", summary.first_speech_ms)
                for stage in ("endpoint", "stt_tail", "speaker_final", "tts_first_chunk"):
                    if stage in summary.stages:
                        _row(table, f"e2e {scenario} {stage}", summary.stages[stage])
    console.print(table)
    facts = Table(title="other numbers")
    facts.add_column("metric")
    facts.add_column("value", justify="right")
    if s.speaker and s.speaker.fp32:
        facts.add_row("household EER (%)", _fmt(s.speaker.fp32.household_eer_pct))
        facts.add_row("speaker: concurrent with STT", str(s.speaker.concurrent_stt))
    if s.stt and s.stt.threads_2:
        facts.add_row("stt WER (%), 2 threads", _fmt(s.stt.threads_2.wer_pct))
    if s.wake:
        facts.add_row("wake model", f"{s.wake.model_id} (stock={s.wake.stock_model})")
        facts.add_row("false accepts / 24 h", _fmt(s.wake.false_accepts_per_24h))
        facts.add_row("false rejects 3 m (%)", _fmt(s.wake.false_reject_pct_3m))
        facts.add_row("wake CPU (% of one core)", _fmt(s.wake.cpu_pct_of_core))
    if s.endpoint:
        for h in s.endpoint.hangovers.values():
            over = h.overrun_ms.p50 if h.overrun_ms else None
            facts.add_row(
                f"endpoint hangover {h.hangover_ms} ms: truncation % / overrun p50 ms",
                f"{h.truncation_pct:.1f} / {_fmt(over)}",
            )
        facts.add_row("chosen hangover (ms)", str(s.endpoint.chosen_hangover_ms))
    if s.ram and s.ram.default:
        facts.add_row("RAM peak used (GiB)", _fmt(s.ram.default.peak_used_bytes / 2**30))
    if s.thermal and s.thermal.status == "measured":
        facts.add_row("thermal 0x4 seen", str(s.thermal.throttled_0x4_seen))
    for name in (
        "speaker",
        "stt",
        "tts",
        "wake",
        "aec",
        "endpoint",
        "llm",
        "e2e",
        "ram",
        "thermal",
    ):
        section = getattr(s, name)
        if section is not None and section.status != "measured":
            facts.add_row(f"{name}: {section.status}", section.reason or "")
    console.print(facts)
    if result.gates:
        render_gates(result.gates, console)


def _shown(observed: float | bool | str | None) -> str:
    if observed is None:
        return "not measured"
    if isinstance(observed, bool | str):
        return str(observed)
    return f"{observed:,.1f}" if abs(observed) >= 10 else f"{observed:.3f}"


def _mark(passed: bool | None) -> str:
    return "ok " if passed else ("-- " if passed is None else "FAIL")


def render_gates(report: GateReport, console: Console) -> None:
    table = Table(title=f"M0 gates ({report.measurement})")
    for col in ("gate", "status", "checks"):
        table.add_column(col)
    for g in report.gates:
        lines = [
            f"{_mark(c.passed)} {c.label}: {_shown(c.observed)} {c.op} {c.threshold}{c.unit}"
            for c in g.checks
        ]
        lines += [f"pre {'ok' if p.passed else 'unmet'}: {p.label}" for p in g.preconditions]
        color = {"pass": "green", "fail": "red", "incomplete": "yellow", "skipped": "dim"}[g.status]
        table.add_row(f"{g.gate} {g.name}", f"[{color}]{g.status}[/{color}]", "\n".join(lines))
    console.print(table)
    verdict = "ALL GATES PASS" if report.all_passed else "GATES NOT PASSED"
    console.print(f"[bold]{verdict}[/bold] ({report.measurement})")
