"""Latency statistics in the shape the ROADMAP statistic rule wants.

ROADMAP "The latency contract": *p50 meets the number; p90 <= 1.3x the number;
p95 is reported, not gated* (the one exception, the audible-feedback earcon,
is a p95 promise and G1 is written as a p95). Every bench section therefore
reports the same :class:`Summary` so the gate table can read ``p50``, ``p90``
and ``p95`` by name.

Percentiles use linear interpolation between order statistics (numpy's
default), computed here in pure Python so importing this module stays cheap.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

#: ROADMAP statistic rule: p90 may exceed the p50 number by this factor.
P90_FACTOR = 1.3


class Summary(BaseModel):
    """Order statistics of one latency series. Values keep the series' unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int
    p50: float
    p90: float
    p95: float
    min: float
    max: float
    mean: float


def percentile(values: Iterable[float], q: float) -> float:
    """``q``-th percentile (0..100) with linear interpolation; empty input raises."""
    data = sorted(float(v) for v in values)
    if not data:
        msg = "percentile of an empty series"
        raise ValueError(msg)
    if not 0.0 <= q <= 100.0:
        msg = f"q must be within 0..100, got {q}"
        raise ValueError(msg)
    if any(math.isnan(v) for v in data):
        msg = "percentile of a series containing NaN"
        raise ValueError(msg)
    position = (len(data) - 1) * q / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return data[lower]
    weight = position - lower
    return data[lower] + (data[upper] - data[lower]) * weight


def summarize(values: Iterable[float]) -> Summary:
    """Build a :class:`Summary`; empty input raises ``ValueError``."""
    data = [float(v) for v in values]
    if not data:
        msg = "summarize() needs at least one value"
        raise ValueError(msg)
    return Summary(
        n=len(data),
        p50=percentile(data, 50),
        p90=percentile(data, 90),
        p95=percentile(data, 95),
        min=min(data),
        max=max(data),
        mean=sum(data) / len(data),
    )


def maybe_summarize(values: Iterable[float]) -> Summary | None:
    """Like :func:`summarize` but ``None`` for an empty series (never invents a number)."""
    data = [float(v) for v in values]
    return summarize(data) if data else None


def meets_contract(summary: Summary, p50_limit: float) -> bool:
    """Apply the ROADMAP rule: p50 <= limit and p90 <= 1.3 x limit."""
    return summary.p50 <= p50_limit and summary.p90 <= P90_FACTOR * p50_limit
