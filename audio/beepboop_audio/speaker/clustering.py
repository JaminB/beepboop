"""Multi-centroid voiceprints: spherical k-means with k chosen by silhouette (VOICE_PIPELINE §8.3).

Each profile has ``k <= 4`` unit-vector centroids. ``k = 1`` is the default;
a larger ``k`` is taken only when its mean silhouette (cosine distance) beats
``MIN_SILHOUETTE`` (a target, calibrated in M0) and every cluster keeps at
least two members, so a single odd clip never becomes its own centroid.
Deterministic: k-means++ seeding from a fixed RNG seed.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

type Matrix = npt.NDArray[np.float32]

MAX_CENTROIDS = 4
MIN_SILHOUETTE = 0.10
_ITERATIONS = 50


def normalise_rows(x: Matrix) -> Matrix:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (x / norms).astype(np.float32)


def _kmeans(x: Matrix, k: int, seed: int) -> tuple[Matrix, npt.NDArray[np.int64]]:
    rng = np.random.default_rng(seed)
    n = len(x)
    centres = [x[int(rng.integers(n))]]
    for _ in range(1, k):
        sims = np.max(x @ np.stack(centres).T, axis=1)
        dist = np.clip(1.0 - sims, 0.0, None) ** 2
        total = float(dist.sum())
        idx = int(rng.integers(n)) if total <= 0 else int(rng.choice(n, p=dist / total))
        centres.append(x[idx])
    c = normalise_rows(np.stack(centres))
    labels = np.zeros(n, dtype=np.int64)
    for _ in range(_ITERATIONS):
        labels_new = np.argmax(x @ c.T, axis=1).astype(np.int64)
        for j in range(k):
            members = x[labels_new == j]
            if len(members):
                c[j] = members.mean(axis=0)
        c = normalise_rows(c)
        if np.array_equal(labels_new, labels):
            break
        labels = labels_new
    return c, labels


def silhouette(x: Matrix, labels: npt.NDArray[np.int64]) -> float:
    """Mean silhouette with cosine distance; 0.0 when undefined (one cluster)."""
    clusters = np.unique(labels)
    if len(clusters) < 2:
        return 0.0
    dist = 1.0 - np.clip(x @ x.T, -1.0, 1.0)
    values: list[float] = []
    for i in range(len(x)):
        own = labels == labels[i]
        n_own = int(own.sum())
        if n_own <= 1:
            values.append(0.0)
            continue
        a = float(dist[i, own].sum() / (n_own - 1))
        b = min(float(dist[i, labels == c].mean()) for c in clusters if c != labels[i])
        values.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return float(np.mean(values))


def centroids(embeddings: Matrix, *, max_k: int = MAX_CENTROIDS, seed: int = 0) -> Matrix:
    """Unit centroids, shape ``(k, dim)``, for one profile's embeddings."""
    if len(embeddings) == 0:
        raise ValueError("no embeddings")
    x = normalise_rows(embeddings.astype(np.float32))
    best = normalise_rows(x.mean(axis=0, keepdims=True))
    best_score = MIN_SILHOUETTE
    for k in range(2, min(max_k, len(x) // 2) + 1):
        c, labels = _kmeans(x, k, seed)
        counts = np.bincount(labels, minlength=k)
        if counts.min() < 2:
            continue
        score = silhouette(x, labels)
        if score > best_score:
            best, best_score = c, score
    return best


def similarity(a: Matrix, b: Matrix) -> float:
    """Max cosine between any centroid of ``a`` and any of ``b`` (VOICE_PIPELINE §9.3)."""
    return float(np.max(normalise_rows(a) @ normalise_rows(b).T))


__all__ = [
    "MAX_CENTROIDS",
    "MIN_SILHOUETTE",
    "centroids",
    "normalise_rows",
    "silhouette",
    "similarity",
]
