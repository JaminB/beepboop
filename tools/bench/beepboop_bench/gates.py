"""The M0 go/no-go gates G1-G7, encoded exactly as docs/ROADMAP.md "M0: Prove the Pi".

ROADMAP is the **only** place the gate numbers are decided; this table is a
transcription of it, and :func:`evaluate` is the ``bench all --gates`` judge.
If the two ever disagree, ROADMAP wins and this file is the bug.

Reading the table
    * ``checks``: the thresholds. Every one must pass.
    * ``preconditions``: facts that must hold for the number to count
      (duration of the soak, the model being the stock one, both RAM profiles
      measured). An unmet precondition makes the gate ``incomplete``, never a
      pass.
    * ``arbiter``: only G3 has one. ROADMAP: "the arbiter is tool-turn
      ``first_speech`` p50 <= 2.0 s". Whenever the arbiter was measured it
      decides: the two TTFT numbers may miss and G3 still passes, and they may
      hold while G3 fails. Without an arbiter sample the TTFT checks decide.
    * The statistic rule (ROADMAP "The latency contract"): *p50 meets the
      number; p90 <= 1.3x; p95 reported, not gated*, except where ROADMAP
      names p95 itself (G1 and the audible-feedback earcon in G6).

Run-level preconditions (:data:`RUN_PRECONDITIONS`): ROADMAP says every gate
is "measured on a **4 GB** Pi 4 ... HA running ... during ``bench e2e``" and
"the 8 GB run is recorded alongside". The refusal in :mod:`beepboop_bench.run`
stops a non-Pi or synthetic run before it gets here; ``--allow-non-pi`` lets
development runs through with the report labelled "not a gate result". An
8 GB Pi is not refused (its file is the "alongside" record) but, like any run
with an unmet run precondition, none of its gates can end as ``pass``.

Metric paths are dotted paths into ``BenchResult.model_dump()``; a missing or
``None`` value means "not measured" and the check is ``skipped`` (it never
counts as a pass).
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from beepboop_bench.schema import (
    BenchResult,
    CheckResult,
    GateReport,
    GateResult,
    Measurement,
)
from beepboop_bench.stats import P90_FACTOR

Op = Literal["<", "<=", "==", ">=", ">"]

_OPS: dict[Op, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    ">=": operator.ge,
    ">": operator.gt,
}

GIB = 2**30


@dataclass(frozen=True)
class Check:
    """``<metric at path> <op> <threshold>``."""

    label: str
    path: str
    op: Op
    threshold: float | bool | str
    unit: str = ""


@dataclass(frozen=True)
class Gate:
    gate: str
    name: str
    checks: tuple[Check, ...]
    preconditions: tuple[Check, ...] = field(default_factory=tuple)
    arbiter: Check | None = None
    note: str = ""


def _p50_and_p90(
    label: str, path: str, op: Op, limit: float, unit: str = "ms"
) -> tuple[Check, ...]:
    """The ROADMAP statistic rule for one number: p50 meets it, p90 <= 1.3x."""
    return (
        Check(f"{label} p50", f"{path}.p50", op, limit, unit),
        Check(f"{label} p90 (<= 1.3x)", f"{path}.p90", op, P90_FACTOR * limit, unit),
    )


# ---- the table (docs/ROADMAP.md, "Go/no-go gates") ---------------------------

RUN_PRECONDITIONS: tuple[Check, ...] = (
    Check("host is a Raspberry Pi (linux/aarch64)", "host.is_raspberry_pi", "==", True),
    Check(
        "gate board is the 4 GB Pi 4 (8 GB is recorded alongside)",
        "host.pi_ram_variant",
        "==",
        "4gb",
    ),
    Check("corpus is real (recorded through the XVF3800)", "corpus.synthetic", "==", False),
    Check("Home Assistant running during the run", "sections.e2e.ha_running", "==", True),
)

G1 = Gate(
    "G1",
    "Speaker ID (CAM++ fp32, 2 threads) latency",
    checks=(
        Check("2 s clip embed p95", "sections.speaker.fp32.clip_2s_embed_ms.p95", "<", 200.0, "ms"),
    ),
    preconditions=(
        Check("2 threads", "sections.speaker.threads", "==", 2),
        Check("concurrent with STT", "sections.speaker.concurrent_stt", "==", True),
    ),
    note="ROADMAP G1: p95 < 200 ms for a 2 s clip, concurrent with STT.",
)

G2 = Gate(
    "G2",
    "Streaming STT (zipformer small, 2 threads)",
    checks=(
        *_p50_and_p90("RTF", "sections.stt.threads_2.rtf", "<", 0.6, ""),
        *_p50_and_p90(
            "final tail after t_eos", "sections.stt.threads_2.final_tail_ms", "<=", 100.0
        ),
    ),
    preconditions=(Check("2 threads measured", "sections.stt.threads_2.threads", "==", 2),),
    note=(
        "ROADMAP G2: p50 RTF < 0.6 and p50 final tail (t_eos flush -> final) <= 100 ms, "
        "p90 <= 1.3x each."
    ),
)

G3 = Gate(
    "G3",
    "Bedrock Haiku 5.5 TTFT, pre-warmed TLS, home connection",
    checks=(
        Check(
            "warm first-call TTFT p50",
            "sections.llm.scenarios.warm_first.ttft_ms.p50",
            "<=",
            600.0,
            "ms",
        ),
        Check(
            "warm second (cached, tool-result) call TTFT p50",
            "sections.llm.scenarios.warm_second_tool_result.ttft_ms.p50",
            "<=",
            500.0,
            "ms",
        ),
    ),
    arbiter=Check(
        "tool-turn first_speech p50", "sections.e2e.tool.first_speech_ms.p50", "<=", 2000.0, "ms"
    ),
    note=(
        "ROADMAP G3: warm first-call p50 <= 600 ms, second call p50 <= 500 ms; the arbiter is "
        "tool-turn first_speech p50 <= 2.0 s. Cold cache (gap >= 6 min) is reported, not gated."
    ),
)

G4 = Gate(
    "G4",
    "RAM",
    checks=(
        Check(
            "peak (MemTotal - MemAvailable), default profile",
            "sections.ram.default.peak_used_bytes",
            "<",
            3.2 * GIB,
            "bytes",
        ),
        Check(
            "zram used, default profile", "sections.ram.default.zram_used_pct_peak", "<", 25.0, "%"
        ),
        Check(
            "OOM kills over 24 h, default profile",
            "sections.ram.default.oom_kills",
            "==",
            0,
            "kills",
        ),
        Check(
            "peak (MemTotal - MemAvailable), music on",
            "sections.ram.music.peak_used_bytes",
            "<",
            3.2 * GIB,
            "bytes",
        ),
        Check("zram used, music on", "sections.ram.music.zram_used_pct_peak", "<", 25.0, "%"),
        Check("OOM kills over 24 h, music on", "sections.ram.music.oom_kills", "==", 0, "kills"),
    ),
    preconditions=(
        Check("4 GB Pi", "host.pi_ram_variant", "==", "4gb"),
        Check(
            "default profile sampled >= 24 h", "sections.ram.default.duration_s", ">=", 86400.0, "s"
        ),
        Check("music profile sampled >= 24 h", "sections.ram.music.duration_s", ">=", 86400.0, "s"),
    ),
    note=(
        "ROADMAP G4: peak used < 3.2 GiB on a 4 GB (nominal) Pi, zram used < 25 %, zero OOM "
        "kills of any container over 24 h, default profile and again with music on."
    ),
)

G5 = Gate(
    "G5",
    'Wake word, stock "Hey beepboop" model',
    checks=(
        Check(
            "false accepts per 24 h (soak corpus)",
            "sections.wake.false_accepts_per_24h",
            "<=",
            0.5,
            "/24h",
        ),
        Check("false rejects at 3 m", "sections.wake.false_reject_pct_3m", "<=", 5.0, "%"),
    ),
    preconditions=(
        Check("stock hey_beepboop model", "sections.wake.stock_model", "==", True),
        Check("soak corpus >= 24 h", "sections.wake.soak_hours", ">=", 24.0, "h"),
    ),
    note="ROADMAP G5: <= 0.5 false accepts per 24 h on the soak corpus, <= 5 % false rejects "
    "at 3 m.",
)

G6 = Gate(
    "G6",
    "End-to-end latency contract (ADR 0015)",
    checks=(
        *_p50_and_p90(
            "fast path first_speech", "sections.e2e.fast_path.first_speech_ms", "<=", 500.0
        ),
        *_p50_and_p90(
            "1 s command first_speech", "sections.e2e.command_1s.first_speech_ms", "<=", 500.0
        ),
        Check(
            "no-tool first audio p95 (earcon promise)",
            "sections.e2e.no_tool.first_audio_ms.p95",
            "<",
            1000.0,
            "ms",
        ),
        Check(
            "tool first audio p95 (earcon promise)",
            "sections.e2e.tool.first_audio_ms.p95",
            "<",
            1000.0,
            "ms",
        ),
        *_p50_and_p90("no-tool first_speech", "sections.e2e.no_tool.first_speech_ms", "<", 1500.0),
        *_p50_and_p90("tool first_speech", "sections.e2e.tool.first_speech_ms", "<", 2000.0),
        *_p50_and_p90(
            "TTS first chunk (stage check)",
            "sections.tts.default_voice_first_chunk_ms",
            "<=",
            200.0,
        ),
    ),
    preconditions=(Check("fast path measured offline", "sections.e2e.offline", "==", True),),
    note=(
        "ROADMAP G6: p50 fast path <= 0.5 s offline incl. the 1 s command case; first audio "
        "< 1.0 s (p95, earcon); no-tool < 1.5 s; tool < 2.0 s; p90 <= 1.3x each; TTS first "
        "chunk p50 <= 200 ms."
    ),
)

G7 = Gate(
    "G7",
    "Thermal",
    checks=(Check("0x4 throttle bit seen", "sections.thermal.throttled_0x4_seen", "==", False),),
    preconditions=(
        Check("sampled during e2e", "sections.thermal.during", "==", "e2e"),
        Check("20 minutes", "sections.thermal.duration_s", ">=", 1200.0, "s"),
    ),
    note="ROADMAP G7: no 0x4 throttle bit during a 20-minute bench e2e with the reference fan "
    "case.",
)

GATES: tuple[Gate, ...] = (G1, G2, G3, G4, G5, G6, G7)


# ---- evaluation --------------------------------------------------------------


def resolve(data: Mapping[str, object], path: str) -> object | None:
    """Walk ``a.b.c`` through nested mappings; ``None`` when any step is missing."""
    node: object | None = data
    for part in path.split("."):
        node = _child(node, part)
        if node is None:
            return None
    return node


def _child(node: object, key: str) -> object | None:
    if isinstance(node, Mapping):
        return cast("Mapping[str, object]", node).get(key)
    return None


def _flatten(result: BenchResult) -> dict[str, object]:
    """``model_dump`` plus the derived paths the table refers to."""
    data: dict[str, object] = result.model_dump(mode="python")
    tts = result.sections.tts
    if tts is not None and tts.default_voice in tts.voices:
        voice = tts.voices[tts.default_voice]
        if voice.first_chunk_ms is not None:
            sections = cast("dict[str, object]", data["sections"])
            tts_data = cast("dict[str, object]", sections["tts"])
            tts_data["default_voice_first_chunk_ms"] = voice.first_chunk_ms.model_dump()
    return data


def run_check(data: Mapping[str, object], check: Check) -> CheckResult:
    observed = resolve(data, check.path)
    passed: bool | None
    shown: float | bool | str | None
    if observed is None:
        passed, shown = None, None
    elif isinstance(check.threshold, bool | str) or isinstance(observed, bool | str):
        # Equality on flags and labels; any other operator on them is a table bug.
        passed = observed == check.threshold if check.op == "==" else None
        shown = observed if isinstance(observed, bool | str) else None
    elif isinstance(observed, int | float):
        passed = _OPS[check.op](float(observed), float(check.threshold))
        shown = float(observed)
    else:
        passed, shown = None, None
    return CheckResult(
        label=check.label,
        path=check.path,
        op=check.op,
        threshold=check.threshold,
        observed=shown,
        unit=check.unit,
        passed=passed,
    )


def evaluate_gate(data: Mapping[str, object], gate: Gate) -> GateResult:
    checks = [run_check(data, c) for c in gate.checks]
    pre = [run_check(data, c) for c in gate.preconditions]
    arbiter = run_check(data, gate.arbiter) if gate.arbiter else None

    if all(c.passed is None for c in checks) and (arbiter is None or arbiter.passed is None):
        status: Literal["pass", "fail", "incomplete", "skipped"] = "skipped"
    elif any(p.passed is not True for p in pre):
        status = "incomplete"
    elif arbiter is not None and arbiter.passed is not None:
        # A measured arbiter decides, whichever way the plain checks went.
        status = "pass" if arbiter.passed else "fail"
    elif all(c.passed is True for c in checks):
        status = "pass"
    elif any(c.passed is None for c in checks):
        status = "incomplete"
    else:
        status = "fail"
    return GateResult(
        gate=gate.gate,
        name=gate.name,
        status=status,
        checks=checks,
        preconditions=pre,
        arbiter=arbiter,
        note=gate.note,
    )


def evaluate(result: BenchResult, *, measurement: Measurement) -> GateReport:
    """Judge every gate against ``result``. ``measurement`` is the run's label."""
    data = _flatten(result)
    run_pre = [run_check(data, c) for c in RUN_PRECONDITIONS]
    gates = [evaluate_gate(data, g) for g in GATES]
    unmet = [p.label for p in run_pre if p.passed is not True]
    if unmet:
        # Nothing passes off the Pi, without the real corpus or without HA: a
        # development run can show "would pass", never "pass".
        gates = [
            g.model_copy(
                update={
                    "status": "incomplete",
                    "note": f"checks met but run preconditions unmet ({'; '.join(unmet)})",
                }
            )
            if g.status == "pass"
            else g
            for g in gates
        ]
    all_passed = all(g.status == "pass" for g in gates) and not unmet
    return GateReport(
        measurement=measurement, all_passed=all_passed, gates=gates, run_preconditions=run_pre
    )
