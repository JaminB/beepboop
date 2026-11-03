"""``bench llm`` (G3): wraps the Bedrock TTFT probe in core.

The probe lives at ``core/beepboop/providers/llm/bedrock/probe.py`` and is
imported lazily: if it (or boto3) is absent the section is ``skipped``, and
nothing here invents a number. It makes real, billed calls (cents), so it only
runs when asked (``--llm-live``); the default is ``skipped`` with that reason.
The probe's own ``ProbeResult`` is kept whole in ``raw`` form on disk by
``beepboop llm probe``; here it is reduced to the schema's :class:`LlmSection`.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Protocol, cast

from beepboop_bench.schema import LlmScenario, LlmSection
from beepboop_bench.stats import maybe_summarize

NOT_LIVE_REASON = "Bedrock probe not run: pass --llm-live (makes about 20 billed calls)"


class _Sample(Protocol):
    scenario: str
    ttft_ms: int | None
    first_text_ms: int | None
    total_ms: int | None


class _ProbeResult(Protocol):
    measurement: str
    access: str
    calls_made: int
    samples: list[_Sample]
    cache_prefix_tokens: int | None
    cache_read_seen: bool
    total_cost_usd: float
    not_run: list[str]
    notes: list[str]


ProbeRunner = Callable[[int, float, str, bool], _ProbeResult]


def probe_available() -> bool:
    try:
        importlib.import_module("beepboop.providers.llm.bedrock.probe")
    except ImportError:
        return False
    return True


def section_from_probe(result: _ProbeResult, *, model_id: str, region: str) -> LlmSection:
    by_scenario: dict[str, list[_Sample]] = {}
    for sample in result.samples:
        by_scenario.setdefault(sample.scenario, []).append(sample)
    scenarios: dict[str, LlmScenario] = {}
    for name, samples in by_scenario.items():
        scenarios[name] = LlmScenario(
            n=len(samples),
            ttft_ms=maybe_summarize(float(s.ttft_ms) for s in samples if s.ttft_ms is not None),
            first_text_ms=maybe_summarize(
                float(s.first_text_ms) for s in samples if s.first_text_ms is not None
            ),
            total_ms=maybe_summarize(float(s.total_ms) for s in samples if s.total_ms is not None),
        )
    status = "measured" if result.access == "ok" and scenarios else "error"
    return LlmSection(
        status=status,
        reason=None if status == "measured" else f"probe access={result.access}",
        model_id=model_id,
        region=region,
        probe_measurement=result.measurement,
        access=result.access,
        calls_made=result.calls_made,
        scenarios=scenarios,
        cache_prefix_tokens=result.cache_prefix_tokens,
        cache_read_seen=result.cache_read_seen,
        total_cost_usd=result.total_cost_usd,
        not_run=list(result.not_run),
        notes=list(result.notes),
    )


def _default_runner(
    pairs: int, cold_gap_s: float, host: str, gate: bool
) -> tuple[_ProbeResult, str, str]:
    """Build the real provider (ambient AWS credentials) and run the probe."""
    boto3 = importlib.import_module("boto3")
    probe = importlib.import_module("beepboop.providers.llm.bedrock.probe")
    provider_mod = importlib.import_module("beepboop.providers.llm.bedrock.provider")
    settings = provider_mod.BedrockSettings.from_manifest(credentials="ambient")
    region = cast("str | None", boto3.Session().region_name) or cast("str", settings.region)
    model_id = cast("str", settings.model_fast)
    config = probe.ProbeConfig(model_id=model_id, region=region, pairs=pairs, cold_gap_s=cold_gap_s)

    def make_provider() -> object:
        return provider_mod.BedrockProvider(provider_mod.make_client(region), model_id)

    result = asyncio.run(
        probe.run_probe(
            config,
            make_provider,
            label="beepboop bench llm",
            host=host,
            measurement="gate" if gate else "preliminary",
        )
    )
    return cast("_ProbeResult", result), model_id, region


def bench_llm(
    *,
    live: bool,
    pairs: int = 6,
    cold_gap_s: float = 0.0,
    host: str = "dev",
    gate: bool = False,
) -> LlmSection:
    if not probe_available():
        return LlmSection(
            status="skipped",
            reason="Bedrock probe (beepboop.providers.llm.bedrock.probe) not importable",
        )
    if not live:
        return LlmSection(status="skipped", reason=NOT_LIVE_REASON)
    try:
        result, model_id, region = _default_runner(pairs, cold_gap_s, host, gate)
    except Exception as exc:  # the probe's failure modes are the AWS SDK's; recorded, not hidden
        return LlmSection(status="error", reason=f"{type(exc).__name__}: {exc}")
    return section_from_probe(result, model_id=model_id, region=region)
