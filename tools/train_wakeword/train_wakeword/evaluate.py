"""Step 06: false accepts per hour and false-reject rate on held-out data.

One streaming pass per file through ``pymicro-wakeword`` (the Pi's inference library)
yields the raw probability of every 30 ms inference; the sweep over
``sliding_window_size`` x ``probability_cutoff`` is then computed offline, with the
2 s refractory of VOICE_PIPELINE §6.2 applied to detections.

* **False rejects** come from ``positives_holdout/`` (synthetic, never trained on) and,
  when given, ``--real-clips <dir>`` with one sub-directory per speaker (consented
  bench recordings, never committed, ADR 0018).
* **False accepts** come from the ``role: ambient_eval`` datasets or ``--ambient <dir>``.

What this is not: G5. The gate is measured by ``beepboop bench wake`` on the 24 h
living-room soak corpus recorded through the XVF3800 (ROADMAP M0, VOICE_PIPELINE
§16). Until that run exists every report from this step is labelled
``synthetic / x86 / not a gate result``.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt

from train_wakeword.config import EvalConfig, PipelineConfig, Profile
from train_wakeword.log import get_logger
from train_wakeword.paths import Layout

log = get_logger(__name__)

SYNTHETIC_LABEL = "synthetic / x86 / not a gate result"
G5_NOTE = (
    "G5 is measured by `beepboop bench wake` on the soak corpus (ROADMAP M0); "
    "this estimate does not gate."
)

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class Operating:
    """One (window, cutoff) operating point of the sweep."""

    sliding_window_size: int
    probability_cutoff: float


def moving_mean(probs: FloatArray, window: int) -> FloatArray:
    """Mean of the last ``window`` values per step; the first ``window-1`` are 0 (as the loader)."""
    if len(probs) < window:
        return np.zeros(0, dtype=np.float64)
    kernel = np.ones(window, dtype=np.float64) / window
    valid = np.convolve(probs, kernel, mode="valid")
    return np.concatenate([np.zeros(window - 1, dtype=np.float64), valid])


def detections(means: FloatArray, cutoff: float, refractory_steps: int) -> int:
    """Count threshold crossings with ``refractory_steps`` of suppression after each."""
    count = 0
    blocked_until = -1
    for i, value in enumerate(means.tolist()):
        if i < blocked_until:
            continue
        if value > cutoff:
            count += 1
            blocked_until = i + refractory_steps
    return count


def sweep_points(cfg: EvalConfig) -> list[Operating]:
    return [Operating(w, c) for w in cfg.sliding_window_sizes for c in cfg.cutoffs]


def step_seconds(cfg: PipelineConfig, stride: int) -> float:
    """Seconds between two inferences (``stride`` feature frames of ``window_step_ms``)."""
    return stride * cfg.training.window_step_ms / 1000.0


@dataclass
class Tally:
    """Accumulates one group's counts across operating points."""

    clips: int = 0
    seconds: float = 0.0

    def __post_init__(self) -> None:
        self.hits: dict[Operating, int] = {}
        self.events: dict[Operating, int] = {}

    def add(
        self, probs: FloatArray, points: list[Operating], refractory_steps: int, seconds: float
    ) -> None:
        self.clips += 1
        self.seconds += seconds
        for point in points:
            n = detections(
                moving_mean(probs, point.sliding_window_size),
                point.probability_cutoff,
                refractory_steps,
            )
            self.events[point] = self.events.get(point, 0) + n
            self.hits[point] = self.hits.get(point, 0) + (1 if n else 0)


def _wavs(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.wav") if p.is_file())


def _group(root: Path, wav: Path, depth: int) -> str:
    parts = wav.relative_to(root).parts[:depth]
    return "/".join(parts) if parts else "all"


def run(
    cfg: PipelineConfig,
    layout: Layout,
    profile: Profile,
    *,
    tflite: Path | None = None,
    ambient_dirs: list[Path] | None = None,
    real_clips: Path | None = None,
    max_files: int | None = None,
    require_targets: bool = False,
) -> int:
    """Evaluate and write ``results/<date>-eval.json``; 1 only if ``require_targets`` and missed."""
    from train_wakeword._vendor import Scorer

    model = tflite or layout.tflite
    if not model.exists():
        log.error(
            "evaluate.no_model", expected=str(model), hint="run 05_export first or pass --tflite"
        )
        return 1
    ev = cfg.evaluate
    scorer = Scorer(model, cfg.wake_word)
    dt = step_seconds(cfg, scorer.stride)
    refractory_steps = round(ev.refractory_ms / 1000.0 / dt)
    points = sweep_points(ev)

    positive_groups: dict[str, Tally] = {}
    positive_roots: list[tuple[str, Path, int]] = [("synthetic", layout.positives_holdout, 2)]
    if real_clips is not None:
        positive_roots.append(("real", real_clips, 1))
    for kind, root, depth in positive_roots:
        for wav in _wavs(root)[:max_files]:
            probs = scorer.score(wav)
            seconds = len(probs) * dt
            for key in (f"{kind}", f"{kind}/{_group(root, wav, depth)}"):
                positive_groups.setdefault(key, Tally()).add(
                    probs, points, refractory_steps, seconds
                )

    ambient_roots = ambient_dirs or [
        layout.negative_wav(d.id) for d in cfg.datasets_for(profile, "ambient_eval")
    ]
    ambient = Tally()
    for root in ambient_roots:
        for wav in _wavs(root)[:max_files]:
            probs = scorer.score(wav)
            ambient.add(probs, points, refractory_steps, len(probs) * dt)

    hours = ambient.seconds / 3600.0
    table: list[dict[str, object]] = []
    passing: list[Operating] = []
    for point in points:
        faph = (ambient.events.get(point, 0) / hours) if hours > 0 else None
        synthetic = positive_groups.get("synthetic")
        frr = (
            (1 - synthetic.hits.get(point, 0) / synthetic.clips)
            if synthetic and synthetic.clips
            else None
        )
        real = positive_groups.get("real")
        frr_real = (1 - real.hits.get(point, 0) / real.clips) if real and real.clips else None
        ok = (
            faph is not None
            and frr is not None
            and faph <= ev.max_false_accepts_per_hour
            and frr <= ev.max_false_reject_rate
        )
        if ok:
            passing.append(point)
        table.append(
            {
                "sliding_window_size": point.sliding_window_size,
                "probability_cutoff": point.probability_cutoff,
                "false_accepts_per_hour": faph,
                "false_reject_rate_synthetic": frr,
                "false_reject_rate_real": frr_real,
                "meets_targets": ok,
            }
        )
    recommended = max(
        passing, key=lambda p: (p.probability_cutoff, -p.sliding_window_size), default=None
    )
    per_group = {
        key: {
            "clips": t.clips,
            "false_reject_rate": {
                f"w{p.sliding_window_size}_c{p.probability_cutoff}": 1 - t.hits.get(p, 0) / t.clips
                for p in points
            },
        }
        for key, t in sorted(positive_groups.items())
        if t.clips
    }
    report: dict[str, object] = {
        "label": SYNTHETIC_LABEL,
        "gate": G5_NOTE,
        "model": str(model),
        "wake_word": cfg.wake_word,
        "profile": profile,
        "host": platform.node(),
        "machine": platform.machine(),
        "evaluated_at": datetime.now(UTC).isoformat(),
        "inference_step_s": dt,
        "refractory_ms": ev.refractory_ms,
        "targets": {
            "max_false_accepts_per_hour": ev.max_false_accepts_per_hour,
            "max_false_reject_rate": ev.max_false_reject_rate,
        },
        "ambient": {"hours": hours, "sources": [str(r) for r in ambient_roots]},
        "positives": per_group,
        "sweep": table,
        "recommended": (
            {
                "sliding_window_size": recommended.sliding_window_size,
                "probability_cutoff": recommended.probability_cutoff,
            }
            if recommended
            else None
        ),
    }
    layout.results.mkdir(parents=True, exist_ok=True)
    out = layout.results / f"{datetime.now(UTC):%Y-%m-%d}-eval-{platform.node()}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    log.info(
        "evaluate.done",
        report=str(out),
        ambient_hours=round(hours, 2),
        recommended=report["recommended"],
    )
    if require_targets and recommended is None:
        log.error("evaluate.targets_missed", label=SYNTHETIC_LABEL)
        return 1
    return 0
