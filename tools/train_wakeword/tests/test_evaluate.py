"""Pure-numpy parts of 06_evaluate: window averaging, refractory counting, the sweep."""

from __future__ import annotations

import numpy as np
import pytest

from train_wakeword.config import EvalConfig, PipelineConfig
from train_wakeword.evaluate import (
    Operating,
    Tally,
    detections,
    moving_mean,
    step_seconds,
    sweep_points,
)


def test_moving_mean_matches_loader_semantics() -> None:
    probs = np.array([1.0, 1.0, 0.0, 0.0, 1.0], dtype=np.float64)
    means = moving_mean(probs, 2)
    assert means.tolist() == pytest.approx([0.0, 1.0, 0.5, 0.0, 0.5])
    assert moving_mean(probs[:1], 2).size == 0


def test_detections_apply_refractory() -> None:
    means = np.array([0.99, 0.99, 0.99, 0.0, 0.99, 0.0, 0.0, 0.99], dtype=np.float64)
    assert detections(means, 0.9, refractory_steps=3) == 3
    assert detections(means, 0.9, refractory_steps=100) == 1
    assert detections(means, 0.995, refractory_steps=1) == 0


def test_sweep_points(config: PipelineConfig) -> None:
    points = sweep_points(config.evaluate)
    assert len(points) == len(config.evaluate.cutoffs) * len(config.evaluate.sliding_window_sizes)
    assert Operating(5, 0.97) in points


def test_step_seconds(config: PipelineConfig) -> None:
    assert step_seconds(config, 3) == pytest.approx(0.03)


def test_tally_counts_hits_and_events() -> None:
    cfg = EvalConfig(
        cutoffs=[0.5],
        sliding_window_sizes=[1],
        refractory_ms=0,
        max_false_accepts_per_hour=1,
        max_false_reject_rate=0.1,
    )
    points = sweep_points(cfg)
    tally = Tally()
    tally.add(np.array([0.9, 0.9, 0.1]), points, refractory_steps=0, seconds=0.09)
    tally.add(np.array([0.1, 0.1]), points, refractory_steps=0, seconds=0.06)
    assert tally.clips == 2
    assert tally.hits[points[0]] == 1
    assert tally.events[points[0]] == 2
    assert tally.seconds == pytest.approx(0.15)
