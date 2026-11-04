"""The wake decision rule shared by wake providers (VOICE_PIPELINE §6.2).

A **hit** fires when the mean of the last ``sliding_window_size`` model
probabilities (one per 30 ms evaluation) exceeds ``probability_cutoff``, and
no hit fired within ``refractory_ms``. The Silero VAD gate is applied by the
front end, which owns the VAD state.
"""

from __future__ import annotations

from collections import deque


class WakeDecider:
    def __init__(
        self, *, probability_cutoff: float, sliding_window_size: int, refractory_ms: int
    ) -> None:
        self.probability_cutoff = probability_cutoff
        self.sliding_window_size = sliding_window_size
        self.refractory_ns = refractory_ms * 1_000_000
        self._window: deque[float] = deque(maxlen=sliding_window_size)
        self._last_hit_ns: int | None = None

    def update(self, prob: float, t_ns: int) -> float | None:
        """Add one evaluation; returns the window mean if it is a hit, else None."""
        self._window.append(prob)
        if len(self._window) < self.sliding_window_size:
            return None
        mean = sum(self._window) / len(self._window)
        if mean <= self.probability_cutoff:
            return None
        if self._last_hit_ns is not None and t_ns - self._last_hit_ns < self.refractory_ns:
            return None
        self._last_hit_ns = t_ns
        self._window.clear()
        return mean

    def reset(self) -> None:
        """Forget the window (the refractory clock survives a reset on purpose)."""
        self._window.clear()


__all__ = ["WakeDecider"]
