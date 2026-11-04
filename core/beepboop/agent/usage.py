"""Per-turn token usage and cost (docs/LLM_PROVIDERS.md §10).

Every ``Usage`` event of a turn is summed and priced with ``providers/llm/pricing.yaml``
(USD per million tokens, per model id). Cost is stored as integer micro-USD on the
``turns`` row. Rows marked ``verify: true`` are list-price estimates; a turn priced from
one carries ``estimate=True``. Budgets are v0.2 (ROADMAP); v0.1 only records.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Literal, cast

import yaml

from beepboop.providers.llm.protocol import Usage

CacheTtl = Literal["5m", "1h"]


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens for one model id."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    estimate: bool = True

    def micro_usd(self, usage: Usage, ttl: CacheTtl = "5m") -> int:
        """USD per MTok times tokens is micro-USD: the per-MTok price *is* the per-token µUSD."""
        write = self.cache_write_1h if ttl == "1h" else self.cache_write_5m
        total = (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * self.cache_read
            + usage.cache_write_tokens * write
        )
        return round(total)


@cache
def load_pricing() -> dict[str, Price]:
    """Parse ``beepboop/providers/llm/pricing.yaml``."""
    text = resources.files("beepboop.providers.llm").joinpath("pricing.yaml").read_text("utf-8")
    raw = cast(dict[str, dict[str, object]], yaml.safe_load(text) or {})
    out: dict[str, Price] = {}
    for model, row in raw.items():
        out[str(model)] = Price(
            input=float(cast(float, row["in"])),
            output=float(cast(float, row["out"])),
            cache_read=float(cast(float, row["cache_read"])),
            cache_write_5m=float(cast(float, row["cache_write_5m"])),
            cache_write_1h=float(cast(float, row["cache_write_1h"])),
            estimate=bool(row.get("verify", False)),
        )
    return out


@dataclass(slots=True)
class TurnUsage:
    """Sum of every LLM call in one turn (a tool turn makes two or more calls)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_micro_usd: int = 0
    priced: bool = True
    estimate: bool = False
    ttft_ms: int | None = None

    def add(self, usage: Usage, model_id: str | None, *, ttl: CacheTtl = "5m") -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        if self.ttft_ms is None and usage.ttft_ms is not None:
            self.ttft_ms = usage.ttft_ms
        price = load_pricing().get(model_id or "")
        if price is None:
            self.priced = False
            return
        self.cost_micro_usd += price.micro_usd(usage, ttl)
        self.estimate = self.estimate or price.estimate


__all__ = ["CacheTtl", "Price", "TurnUsage", "load_pricing"]
