"""The ROADMAP statistic rule: p50 meets, p90 <= 1.3x, p95 reported."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beepboop_bench.stats import (
    P90_FACTOR,
    Summary,
    maybe_summarize,
    meets_contract,
    percentile,
    summarize,
)


def test_percentile_interpolates_like_numpy() -> None:
    data = [1.0, 2.0, 3.0, 4.0]
    assert percentile(data, 50) == 2.5
    assert percentile(data, 0) == 1.0
    assert percentile(data, 100) == 4.0
    assert percentile(data, 90) == pytest.approx(3.7)


def test_percentile_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        percentile([], 50)
    with pytest.raises(ValueError, match=r"0\.\.100"):
        percentile([1.0], 101)
    with pytest.raises(ValueError, match="NaN"):
        percentile([1.0, math.nan], 50)


def test_summarize_fields() -> None:
    s = summarize([5, 1, 3])
    assert (s.n, s.p50, s.min, s.max, s.mean) == (3, 3.0, 1.0, 5.0, 3.0)
    assert s.p90 == pytest.approx(4.6)
    assert s.p95 == pytest.approx(4.8)


def test_summarize_empty_raises_and_maybe_returns_none() -> None:
    with pytest.raises(ValueError, match="at least one"):
        summarize([])
    assert maybe_summarize([]) is None
    assert maybe_summarize([2.0]) is not None


@given(st.lists(st.floats(min_value=0, max_value=1e6, allow_nan=False), min_size=1, max_size=200))
def test_summary_is_ordered(values: list[float]) -> None:
    s = summarize(values)
    assert s.min <= s.p50 <= s.p90 <= s.p95 <= s.max
    assert s.n == len(values)


def test_meets_contract_applies_both_bounds() -> None:
    assert meets_contract(Summary(n=3, p50=400, p90=600, p95=900, min=1, max=900, mean=5), 500)
    assert not meets_contract(Summary(n=3, p50=400, p90=700, p95=900, min=1, max=900, mean=5), 500)
    assert not meets_contract(Summary(n=3, p50=501, p90=520, p95=900, min=1, max=900, mean=5), 500)
    assert P90_FACTOR == 1.3
