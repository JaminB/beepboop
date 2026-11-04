"""``SpeakerIdentifier``: centroids, cohort, AS-norm, calibration and the accept rule.

Core logic, not a plugin (VOICE_PIPELINE §8, §17): only the embedder swaps.
Voiceprints arrive in ``profiles.sync`` and are held in memory only.

Scoring (§8.3):

1. ``s(p) = max_j cos(e, c_pj)`` over the profile's ``k <= 4`` centroids.
2. Adaptive symmetric normalisation against a cohort (the bundled
   ``cohort_<model_id>.npy`` when installed, plus the other household
   members' centroids), top-N = 50: ``z = ½((s - mu_e)/sd_e + (s - mu_p)/sd_p)``.
   With fewer than ``MIN_COHORT`` cohort vectors the statistics fall back to
   fixed impostor-score defaults (``FALLBACK_MU``/``FALLBACK_SIGMA``).
3. Calibration: ``conf = 1 / (1 + exp(-(a·z + b)))``. ``a``/``b`` are
   defaults (targets, refitted in M0); the per-household calibration reaches
   audio as the thresholds in ``profiles.sync.calib``.
4. Accept rule at the ``act`` thresholds of the top profile (§8.4):
   ``top1`` if ``conf ≥ τ_act`` and ``margin ≥ m_act``, else ``guest``.

Fusion (§8.2): ``z_final = 0.4·z_wake + 0.6·z_cmd`` per candidate.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from beepboop_audio.speaker.clustering import centroids as compute_centroids
from beepboop_audio.speaker.clustering import normalise_rows
from beepboop_audio.types import Embedding, Identity, IdentityStage

GUEST = "guest"
TOP_N = 50
MIN_COHORT = 10
FALLBACK_MU = 0.10  # target: CAM++ impostor cosine mean, measured in M0 (bench speaker)
FALLBACK_SIGMA = 0.10  # target
CALIB_A = 1.5  # target logistic slope
CALIB_B = -3.0  # target logistic offset: z = 2.6 -> 0.70, z = 3.5 -> 0.90
FUSE_WAKE = 0.4
FUSE_CMD = 0.6
MIN_CMD_MS = 500  # shorter command audio: the final uses the wake score alone
ADAPT_MAX = 50

type Matrix = npt.NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Per-profile thresholds from ``voiceprints.calib_json`` (defaults: VOICE_PIPELINE §8.4)."""

    tau_act: float = 0.70
    margin_act: float = 0.10
    tau_personal: float = 0.90
    margin_personal: float = 0.20


@dataclass(slots=True)
class Voiceprint:
    profile_id: str
    centroids: Matrix
    thresholds: Thresholds = field(default_factory=Thresholds)
    anchors: list[Embedding] = field(default_factory=list[Embedding])
    adaptation: list[Embedding] = field(default_factory=list[Embedding])
    negatives: list[Embedding] = field(default_factory=list[Embedding])


def calibrate(z: float, a: float = CALIB_A, b: float = CALIB_B) -> float:
    x = a * z + b
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def uncalibrate(conf: float, a: float = CALIB_A, b: float = CALIB_B) -> float:
    c = min(max(conf, 1e-9), 1 - 1e-9)
    return (math.log(c / (1 - c)) - b) / a


def _stats(scores: npt.NDArray[np.float32]) -> tuple[float, float]:
    top = np.sort(scores)[-TOP_N:]
    sigma = float(top.std())
    return float(top.mean()), sigma if sigma > 1e-6 else FALLBACK_SIGMA


class SpeakerIdentifier:
    """Identify an embedding against the household's voiceprints."""

    def __init__(
        self,
        embedder: tuple[str, str],
        *,
        cohort: Matrix | None = None,
        calib_a: float = CALIB_A,
        calib_b: float = CALIB_B,
    ) -> None:
        self.embedder = embedder
        self._bundled = normalise_rows(cohort) if cohort is not None and len(cohort) else None
        self._a = calib_a
        self._b = calib_b
        self._prints: dict[str, Voiceprint] = {}
        self._profile_stats: dict[str, list[tuple[float, float]]] = {}
        self._z: OrderedDict[Identity, dict[str, float]] = OrderedDict()

    # -- voiceprints -----------------------------------------------------------

    @property
    def profiles(self) -> tuple[str, ...]:
        return tuple(self._prints)

    def load(self, prints: list[Voiceprint]) -> None:
        """Replace every voiceprint (a ``profiles.sync`` snapshot)."""
        self._prints = {
            p.profile_id: Voiceprint(
                p.profile_id,
                normalise_rows(np.asarray(p.centroids, dtype=np.float32)),
                p.thresholds,
                list(p.anchors),
                list(p.adaptation),
                list(p.negatives),
            )
            for p in prints
            if p.profile_id != GUEST and len(p.centroids)
        }
        self._refresh_cohort()

    def clear(self) -> None:
        self._prints = {}
        self._profile_stats = {}
        self._z.clear()

    def thresholds(self, profile_id: str) -> Thresholds:
        vp = self._prints.get(profile_id)
        return vp.thresholds if vp is not None else Thresholds()

    def _cohort_for(self, profile_id: str | None) -> Matrix | None:
        parts: list[Matrix] = []
        if self._bundled is not None:
            parts.append(self._bundled)
        parts.extend(vp.centroids for pid, vp in self._prints.items() if pid != profile_id)
        if not parts:
            return None
        cohort = np.concatenate(parts).astype(np.float32)
        return cohort if len(cohort) >= MIN_COHORT else None

    def _refresh_cohort(self) -> None:
        self._profile_stats = {}
        for pid, vp in self._prints.items():
            cohort = self._cohort_for(pid)
            if cohort is None:
                self._profile_stats[pid] = [(FALLBACK_MU, FALLBACK_SIGMA)] * len(vp.centroids)
            else:
                sims = vp.centroids @ cohort.T
                self._profile_stats[pid] = [_stats(row) for row in sims]
        self._z.clear()

    # -- scoring ---------------------------------------------------------------

    def z_scores(self, emb: Embedding) -> dict[str, float]:
        """AS-normalised score per enrolled profile."""
        e = np.asarray(emb, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(e))
        if norm > 0:
            e = e / norm
        out: dict[str, float] = {}
        for pid, vp in self._prints.items():
            sims = vp.centroids @ e
            j = int(np.argmax(sims))
            s = float(sims[j])
            mu_p, sd_p = self._profile_stats[pid][j]
            cohort = self._cohort_for(pid)
            if cohort is None:
                mu_e, sd_e = FALLBACK_MU, FALLBACK_SIGMA
            else:
                mu_e, sd_e = _stats(cohort @ e)
            out[pid] = 0.5 * ((s - mu_e) / sd_e + (s - mu_p) / sd_p)
        return out

    def _decide(self, z: dict[str, float], *, stage: IdentityStage, anchor: bool) -> Identity:
        confs = sorted(
            ((pid, calibrate(v, self._a, self._b)) for pid, v in z.items()),
            key=lambda kv: (-kv[1], kv[0]),
        )
        top1 = confs[0][1] if confs else 0.0
        top2 = confs[1][1] if len(confs) > 1 else 0.0
        margin = top1 - top2
        profile_id = GUEST
        if confs:
            t = self.thresholds(confs[0][0])
            if top1 >= t.tau_act and margin >= t.margin_act:
                profile_id = confs[0][0]
        identity = Identity(
            profile_id=profile_id,
            score=round(top1, 4),
            margin=round(margin, 4),
            stage=stage,
            anchor=anchor,
            candidates=tuple((pid, round(c, 4)) for pid, c in confs[:3]),
            embedder=self.embedder,
        )
        self._z[identity] = z
        while len(self._z) > 16:
            self._z.popitem(last=False)
        return identity

    def identify(
        self,
        emb: Embedding,
        *,
        stage: IdentityStage,
        anchor: bool,
        prior: npt.NDArray[np.float32] | None = None,
    ) -> Identity:
        """Identify one embedding. ``prior`` is the v0.2 presence prior; it is applied in core
        (VOICE_PIPELINE §8.6), so the audio service never takes one."""
        if prior is not None:
            raise ValueError("the presence prior is applied in core, never in the audio service")
        return self._decide(self.z_scores(emb), stage=stage, anchor=anchor)

    def fuse(self, wake: Identity, cmd: Identity) -> Identity:
        """``z = 0.4·z_wake + 0.6·z_cmd`` per profile; the result is the ``final`` stage."""
        z_wake = self._z.get(wake) or {
            p: uncalibrate(c, self._a, self._b) for p, c in wake.candidates
        }
        z_cmd = self._z.get(cmd) or {p: uncalibrate(c, self._a, self._b) for p, c in cmd.candidates}
        floor = uncalibrate(0.0, self._a, self._b)
        pids = set(z_wake) | set(z_cmd)
        z = {p: FUSE_WAKE * z_wake.get(p, floor) + FUSE_CMD * z_cmd.get(p, floor) for p in pids}
        return self._decide(z, stage="final", anchor=wake.anchor or cmd.anchor)

    def as_final(self, identity: Identity) -> Identity:
        """Re-label a wake-only identity as the final one (command audio < 0.5 s)."""
        z = self._z.get(identity) or {
            p: uncalibrate(c, self._a, self._b) for p, c in identity.candidates
        }
        return self._decide(z, stage="final", anchor=identity.anchor)

    # -- adaptation (v0.2; the buffers exist so v0.1 code paths are exercised) -------------

    def add_adaptation(self, profile_id: str, emb: Embedding) -> None:
        vp = self._prints.get(profile_id)
        if vp is None or profile_id == GUEST:
            return
        vp.adaptation.append(np.asarray(emb, dtype=np.float32))
        del vp.adaptation[:-ADAPT_MAX]

    def add_negative(self, profile_id: str, emb: Embedding) -> None:
        vp = self._prints.get(profile_id)
        if vp is None:
            return
        vp.negatives.append(np.asarray(emb, dtype=np.float32))
        del vp.negatives[:-ADAPT_MAX]

    def rebuild(self) -> None:
        """Recompute centroids from anchors + adaptation (enrollment anchors always count)."""
        for vp in self._prints.values():
            samples = vp.anchors + vp.adaptation
            if samples:
                vp.centroids = compute_centroids(np.stack(samples).astype(np.float32))
        self._refresh_cohort()


__all__ = [
    "GUEST",
    "MIN_CMD_MS",
    "SpeakerIdentifier",
    "Thresholds",
    "Voiceprint",
    "calibrate",
    "uncalibrate",
]
