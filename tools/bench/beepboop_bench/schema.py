"""The result-file schema: one :class:`BenchResult` per run, one :class:`Turn` per e2e turn.

``tools/bench/results/<date>-<host>.json`` is a ``BenchResult``. The docs cite
numbers from these files, so the shape is versioned (``schema_version``) and
the JSON Schema is generated deterministically (:func:`json_schema`). Every
section carries a ``status`` so a number that was not measured is *absent*,
never zero: ``measured`` has numbers, ``skipped`` was not attempted on this
host (reason given), ``pending`` needs hardware or a human, ``error`` failed.

Stage names in :class:`Turn.stages_ms` are exactly the OPERATIONS §11 table
(:data:`STAGE_NAMES`), so the UI, the bench and CI compare like with like.
Tool spans are ``tool.<name>``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from beepboop_bench.host import HostFacts
from beepboop_bench.stats import Summary

SCHEMA_VERSION = 1

#: Fixed stage names (OPERATIONS §11 "Per-turn traces"); ``tool.<name>`` is per call.
STAGE_NAMES: tuple[str, ...] = (
    "wake",
    "speech",
    "endpoint",
    "stt_tail",
    "speaker_final",
    "fastpath",
    "ctx",
    "llm_ttft",
    "llm_first_sentence",
    "tts_first_chunk",
    "first_audio",
    "first_speech",
)

#: e2e scenarios. ``command_1s`` is the G6 "1 s command" fast-path case.
Scenario = Literal["fast_path", "command_1s", "no_tool", "tool"]
SCENARIOS: tuple[Scenario, ...] = ("fast_path", "command_1s", "no_tool", "tool")

SectionStatus = Literal["measured", "skipped", "pending", "error"]
Measurement = Literal["gate", "not a gate result"]

#: The label every non-Pi run carries (task rule 1).
NOT_A_GATE_LABEL = "not a gate result"
SYNTHETIC_LABEL = "synthetic / x86 / not a gate result"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Section(_Model):
    """Common header of every bench section."""

    status: SectionStatus
    reason: str | None = None
    notes: list[str] = Field(default_factory=list[str])


# ---- speaker (G1) -----------------------------------------------------------


class SpeakerVariant(_Model):
    model_id: str
    precision: Literal["fp32", "int8"]
    embedding_dim: int
    clip_1s_embed_ms: Summary | None = None
    clip_2s_embed_ms: Summary | None = None
    household_eer_pct: float | None = None
    eer_threshold: float | None = None
    n_speakers: int = 0
    n_clips: int = 0


class SpeakerSection(Section):
    threads: int = 2
    concurrent_stt: bool = False
    fp32: SpeakerVariant | None = None
    int8: SpeakerVariant | None = None


# ---- stt (G2) ---------------------------------------------------------------


class SttThreads(_Model):
    threads: int
    rtf: Summary | None = None
    final_tail_ms: Summary | None = None
    wer_pct: float | None = None
    wer_hotwords_pct: float | None = None
    n_clips: int = 0
    n_words: int = 0


class SttSection(Section):
    model_id: str = ""
    precision: Literal["fp32", "int8"] = "int8"
    decoding_method: str = "modified_beam_search"
    max_active_paths: int = 4
    flush_silence_s: float = 0.3
    threads_1: SttThreads | None = None
    threads_2: SttThreads | None = None


# ---- tts (G6 stage check) ---------------------------------------------------


class TtsVoice(_Model):
    voice: str
    sample_rate: int
    first_chunk_ms: Summary | None = None
    rtf: Summary | None = None
    n_sentences: int = 0


class TtsSection(Section):
    threads: int = 2
    default_voice: str = ""
    voices: dict[str, TtsVoice] = Field(default_factory=dict[str, TtsVoice])


# ---- wake (G5) --------------------------------------------------------------


class WakePerSpeaker(_Model):
    n: int
    false_rejects: int
    false_reject_pct: float


class WakeSection(Section):
    model_id: str = ""
    probability_cutoff: float = 0.0
    sliding_window_size: int = 0
    stock_model: bool = False
    soak_hours: float = 0.0
    false_accepts: int | None = None
    false_accepts_per_24h: float | None = None
    n_positive_1m: int = 0
    n_positive_3m: int = 0
    false_reject_pct_1m: float | None = None
    false_reject_pct_3m: float | None = None
    hit_latency_ms: Summary | None = None
    cpu_pct_of_core: float | None = None
    per_speaker: dict[str, WakePerSpeaker] = Field(default_factory=dict[str, WakePerSpeaker])


# ---- endpoint (G6 chooses the hangover) --------------------------------------


class HangoverResult(_Model):
    hangover_ms: int
    n: int
    truncation_pct: float
    overrun_ms: Summary | None = None


class EndpointSection(Section):
    hangovers: dict[str, HangoverResult] = Field(default_factory=dict[str, HangoverResult])
    chosen_hangover_ms: int | None = None
    n_labelled: int = 0


# ---- aec (V3) ---------------------------------------------------------------


class AecSection(Section):
    quiet_hit_rate: float | None = None
    playback_hit_rate: float | None = None
    quiet_wer_pct: float | None = None
    playback_wer_pct: float | None = None
    aec_verified: bool | None = None


# ---- llm (G3) ---------------------------------------------------------------


class LlmScenario(_Model):
    n: int
    ttft_ms: Summary | None = None
    first_text_ms: Summary | None = None
    total_ms: Summary | None = None


class LlmSection(Section):
    model_id: str = ""
    region: str = ""
    probe_measurement: str = ""
    access: str = ""
    calls_made: int = 0
    scenarios: dict[str, LlmScenario] = Field(default_factory=dict[str, LlmScenario])
    cache_prefix_tokens: int | None = None
    cache_read_seen: bool = False
    total_cost_usd: float = 0.0
    not_run: list[str] = Field(default_factory=list[str])


# ---- e2e (G6) ---------------------------------------------------------------


class ToolSpan(_Model):
    name: str
    ms: float
    risk: str | None = None
    decision: str | None = None
    factor: str | None = None


class Turn(_Model):
    """One turn's waterfall. Clock zero is the last VAD-positive frame (``t_eos``).

    ``stages_ms`` holds the OPERATIONS §11 stages as durations in ms; a stage
    that was not exercised in this run is absent or ``None`` (never 0).
    ``marks_ms`` holds instants relative to ``t_eos`` (negative = before it).
    """

    turn_id: str
    clip_id: str
    scenario: Scenario
    speaker: str | None = None
    started_at_s: float
    stages_ms: dict[str, float | None] = Field(default_factory=dict[str, float | None])
    marks_ms: dict[str, float] = Field(default_factory=dict[str, float])
    tools: list[ToolSpan] = Field(default_factory=list[ToolSpan])
    transcript: str | None = None
    expected_transcript: str | None = None
    speaker_identified: str | None = None
    ok: bool = True
    error: str | None = None


class ScenarioSummary(_Model):
    n: int
    first_audio_ms: Summary | None = None
    first_speech_ms: Summary | None = None
    stages: dict[str, Summary] = Field(default_factory=dict[str, Summary])


class E2ESection(Section):
    concurrency: int = 1
    realtime: bool = True
    ha_running: bool | None = None
    llm_live: bool = False
    offline: bool | None = None
    turns: list[Turn] = Field(default_factory=list[Turn])
    fast_path: ScenarioSummary | None = None
    command_1s: ScenarioSummary | None = None
    no_tool: ScenarioSummary | None = None
    tool: ScenarioSummary | None = None


# ---- ram (G4) ---------------------------------------------------------------


class RamProfile(_Model):
    profile: Literal["default", "music"]
    duration_s: float
    samples: int
    mem_total_bytes: int
    peak_used_bytes: int
    used_bytes: Summary | None = None
    zram_disksize_bytes: int | None = None
    zram_used_pct_peak: float | None = None
    oom_kills: int | None = None
    oom_by_container: dict[str, int] = Field(default_factory=dict[str, int])
    oom_source: str | None = None


class RamSection(Section):
    default: RamProfile | None = None
    music: RamProfile | None = None


# ---- thermal (G7) -----------------------------------------------------------


class ThermalSection(Section):
    during: str = "e2e"
    duration_s: float = 0.0
    samples: int = 0
    temp_c: Summary | None = None
    throttled_0x4_seen: bool | None = None
    throttle_bits_union: int | None = None


# ---- the file ---------------------------------------------------------------


class Sections(_Model):
    speaker: SpeakerSection | None = None
    stt: SttSection | None = None
    tts: TtsSection | None = None
    wake: WakeSection | None = None
    aec: AecSection | None = None
    endpoint: EndpointSection | None = None
    llm: LlmSection | None = None
    e2e: E2ESection | None = None
    ram: RamSection | None = None
    thermal: ThermalSection | None = None


class CorpusInfo(_Model):
    path: str
    synthetic: bool
    recorded_through: str
    n_clips: int
    n_speakers: int
    soak_hours: float = 0.0


class CheckResult(_Model):
    label: str
    path: str
    op: str
    threshold: float | bool | str
    observed: float | bool | str | None
    unit: str
    passed: bool | None


class GateResult(_Model):
    gate: str
    name: str
    status: Literal["pass", "fail", "incomplete", "skipped"]
    checks: list[CheckResult] = Field(default_factory=list[CheckResult])
    preconditions: list[CheckResult] = Field(default_factory=list[CheckResult])
    arbiter: CheckResult | None = None
    note: str | None = None


class GateReport(_Model):
    measurement: Measurement
    all_passed: bool
    gates: list[GateResult]
    run_preconditions: list[CheckResult] = Field(default_factory=list[CheckResult])


class BenchResult(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    measurement: Measurement
    label: str
    command: str
    host: HostFacts
    started_at: datetime
    finished_at: datetime | None = None
    corpus: CorpusInfo | None = None
    models: dict[str, str] = Field(default_factory=dict[str, str])
    sections: Sections = Field(default_factory=Sections)
    gates: GateReport | None = None
    notes: list[str] = Field(default_factory=list[str])

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def json_schema() -> dict[str, object]:
    """JSON Schema of :class:`BenchResult`, keys sorted so the output is stable."""
    raw = BenchResult.model_json_schema()
    return json.loads(json.dumps(raw, sort_keys=True))


def json_schema_text() -> str:
    return json.dumps(json_schema(), indent=2, sort_keys=True) + "\n"
