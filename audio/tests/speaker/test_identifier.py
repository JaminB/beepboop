"""SpeakerIdentifier: AS-norm, calibration, accept rule, centroids, fusion (VOICE_PIPELINE §8)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from beepboop_audio.speaker.clustering import centroids, normalise_rows, silhouette, similarity
from beepboop_audio.speaker.identifier import (
    GUEST,
    MIN_COHORT,
    SpeakerIdentifier,
    Thresholds,
    Voiceprint,
    calibrate,
    uncalibrate,
)

DIM = 32
EMBEDDER = ("model", "sha")


def unit(v: np.ndarray) -> np.ndarray:
    return (v / np.linalg.norm(v)).astype(np.float32)


def basis(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def near(i: int, noise: float = 0.05, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return unit(basis(i) + noise * rng.standard_normal(DIM).astype(np.float32))


def ident(*prints: Voiceprint, cohort: np.ndarray | None = None) -> SpeakerIdentifier:
    s = SpeakerIdentifier(EMBEDDER, cohort=cohort)
    s.load(list(prints))
    return s


def vp(pid: str, *vectors: np.ndarray, thresholds: Thresholds | None = None) -> Voiceprint:
    return Voiceprint(pid, np.stack(vectors).astype(np.float32), thresholds or Thresholds())


def test_no_profiles_is_guest() -> None:
    s = ident()
    out = s.identify(near(0), stage="provisional", anchor=True)
    assert out.profile_id == GUEST
    assert out.score == 0.0
    assert out.margin == 0.0
    assert out.candidates == ()
    assert out.embedder == EMBEDDER


def test_genuine_speaker_accepted() -> None:
    s = ident(vp("alex", basis(0)), vp("sam", basis(1)))
    out = s.identify(near(0), stage="final", anchor=True)
    assert out.profile_id == "alex"
    assert out.candidates[0][0] == "alex"
    assert out.score >= 0.7
    assert out.margin >= 0.1


def test_stranger_falls_back_to_guest() -> None:
    s = ident(vp("alex", basis(0)), vp("sam", basis(1)))
    out = s.identify(near(5), stage="final", anchor=True)
    assert out.profile_id == GUEST
    assert out.candidates  # scores still reported for core's per-tier rule


def test_ambiguous_speaker_fails_margin() -> None:
    both = unit(basis(0) + basis(1))
    s = ident(vp("alex", basis(0)), vp("sam", basis(1)))
    out = s.identify(both, stage="final", anchor=True)
    assert out.margin < 0.1
    assert out.profile_id == GUEST


def test_per_profile_act_threshold_is_used() -> None:
    strict = Thresholds(tau_act=0.999999, margin_act=0.0)
    s = ident(vp("alex", basis(0), thresholds=strict), vp("sam", basis(1)))
    out = s.identify(near(0, noise=0.3), stage="final", anchor=True)
    assert out.profile_id == GUEST


def test_multi_centroid_matches_either_mode() -> None:
    s = ident(vp("alex", basis(0), basis(2)), vp("sam", basis(1)))
    assert s.identify(near(2), stage="final", anchor=True).profile_id == "alex"
    assert s.identify(near(0), stage="final", anchor=True).profile_id == "alex"


def test_guest_row_never_becomes_a_voiceprint() -> None:
    s = ident(vp(GUEST, basis(0)))
    assert s.profiles == ()


def test_as_norm_with_bundled_cohort() -> None:
    rng = np.random.default_rng(1)
    cohort = normalise_rows(rng.standard_normal((MIN_COHORT * 5, DIM)).astype(np.float32))
    s = ident(vp("alex", basis(0)), vp("sam", basis(1)), cohort=cohort)
    z = s.z_scores(near(0))
    assert z["alex"] > z["sam"]
    assert s.identify(near(0), stage="final", anchor=True).profile_id == "alex"


def test_calibration_inverse() -> None:
    for z in (-3.0, 0.0, 1.5, 4.0):
        assert math.isclose(uncalibrate(calibrate(z)), z, abs_tol=1e-6)
    assert calibrate(-1000.0) == pytest.approx(0.0)
    assert calibrate(1000.0) == pytest.approx(1.0)


def test_fusion_weights_command_more() -> None:
    s = ident(vp("alex", basis(0)), vp("sam", basis(1)))
    wake = s.identify(near(0), stage="provisional", anchor=True)
    cmd = s.identify(near(1), stage="final", anchor=True)
    fused = s.fuse(wake, cmd)
    assert fused.stage == "final"
    z = dict(fused.candidates)
    assert z["sam"] > 0
    assert z["alex"] > 0
    # 0.6 on the command: Sam's fused confidence beats Alex's
    assert z["sam"] > z["alex"]


def test_as_final_relabels() -> None:
    s = ident(vp("alex", basis(0)), vp("sam", basis(1)))
    wake = s.identify(near(0), stage="provisional", anchor=True)
    final = s.as_final(wake)
    assert final.stage == "final"
    assert final.profile_id == wake.profile_id


def test_prior_is_refused() -> None:
    s = ident(vp("alex", basis(0)))
    with pytest.raises(ValueError, match="core"):
        s.identify(near(0), stage="final", anchor=True, prior=np.ones(1, dtype=np.float32))


def test_adaptation_buffers_and_rebuild() -> None:
    s = ident(vp("alex", basis(0)), vp("sam", basis(1)))
    for i in range(60):
        s.add_adaptation("alex", near(0, seed=i))
        s.add_negative("alex", near(1, seed=i))
    s.add_adaptation(GUEST, near(0))  # never for guest
    s.rebuild()
    assert s.identify(near(0), stage="final", anchor=True).profile_id == "alex"


@settings(max_examples=60, deadline=None)
@given(
    seed=st.integers(0, 10_000),
    n_profiles=st.integers(0, 5),
)
def test_identity_invariants(seed: int, n_profiles: int) -> None:
    rng = np.random.default_rng(seed)
    prints = [
        vp(
            f"p{i}",
            *normalise_rows(rng.standard_normal((int(rng.integers(1, 5)), DIM)).astype(np.float32)),
        )
        for i in range(n_profiles)
    ]
    s = ident(*prints)
    emb = unit(rng.standard_normal(DIM).astype(np.float32))
    out = s.identify(emb, stage="final", anchor=bool(seed % 2))
    assert 0.0 <= out.score <= 1.0
    assert 0.0 <= out.margin <= out.score
    top = out.candidates[0][0] if out.candidates else GUEST
    assert len(out.candidates) <= 3
    scores = [c for _, c in out.candidates]
    assert scores == sorted(scores, reverse=True)
    assert out.profile_id in (GUEST, top)
    if out.profile_id != GUEST:
        t = s.thresholds(out.profile_id)
        assert out.score >= t.tau_act
        assert out.margin >= t.margin_act


# ---------------------------------------------------------------- clustering --


def test_centroids_one_cluster_for_one_voice() -> None:
    x = np.stack([near(0, seed=i) for i in range(10)])
    assert centroids(x).shape == (1, DIM)


def test_centroids_split_two_modes() -> None:
    x = np.stack([near(0, seed=i) for i in range(8)] + [near(1, seed=i + 50) for i in range(8)])
    c = centroids(x)
    assert c.shape[0] == 2
    assert np.allclose(np.linalg.norm(c, axis=1), 1.0, atol=1e-5)


def test_centroids_never_more_than_four() -> None:
    x = np.stack([near(i % 8, seed=i) for i in range(64)])
    assert 1 <= centroids(x).shape[0] <= 4


def test_centroids_reject_empty() -> None:
    with pytest.raises(ValueError, match="no embeddings"):
        centroids(np.zeros((0, DIM), dtype=np.float32))


def test_silhouette_and_similarity() -> None:
    x = np.stack([basis(0), basis(0), basis(1), basis(1)])
    assert silhouette(x, np.array([0, 0, 1, 1])) > 0.9
    assert silhouette(x, np.array([0, 0, 0, 0])) == 0.0
    assert similarity(np.stack([basis(0)]), np.stack([basis(0), basis(1)])) == pytest.approx(1.0)
