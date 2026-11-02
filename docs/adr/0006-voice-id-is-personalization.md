# 0006. Voice ID is personalization, not authentication

> Status: Accepted · 2026-11-01 · Applies to: v0.1, v0.2, v0.3+

Speaker identification selects a profile and gates the `personal` tier with a threshold and a margin. It never satisfies the `critical` tier on its own, and the documentation says so plainly.

Related: [docs/VOICE_PIPELINE.md](../VOICE_PIPELINE.md) · [docs/IDENTITY_AND_TRUST.md](../IDENTITY_AND_TRUST.md) · [ADR 0005](0005-risk-tiers-and-grants.md) · [ADR 0014](0014-second-factor-web-confirm.md)

## Context

Speaker embeddings are good at closed-set identification among a handful of household voices, especially with a text-dependent wake phrase, and bad at resisting replay, voice cloning, TV audio and look-alike siblings. One study measured about 30 % EER on young children with adult-trained models, and children's voices change month to month. Google's own Voice Match documentation warns that a sound-alike voice can reach personal results. The user's original framing had speaker ID as "fast and local" while also being the sole router for personal data; those goals conflict unless the trust model is explicit.

## Decision

The audio service computes a provisional identity from the ~1 s wake-word pre-roll and a final identity from wake plus the first ≤ 2 s of the command, in parallel with STT so it adds no latency. Scoring is cosine similarity against k centroids per profile with AS-norm against the household plus a ~200-embedding cohort; accept iff `top1 ≥ τ_tier` and `top1 − top2 ≥ margin`, otherwise the Guest profile. The result populates `Principal.confidence` and `Principal.margin`, which the policy uses for the `personal` tier only. `critical` always requires a second factor regardless of score. Adaptation uses only high-margin, uncorrected turns; "that wasn't me" adds a negative example and an audit entry. Enrollment is through the Pi's own mic at several positions, with quality gates and a household similarity matrix shown in the UI. Anti-spoofing is not promised; a liveness hook ships off by default in v0.3+.

## Consequences

- A child misidentified as a parent is still stopped by the second factor and by the Child ceiling; this is tested in the eval suite.
- The UI must explain confidence honestly ("sounds like you: 94 %") and show the margin on every turn.
- Guest is a real profile with safe defaults, because misses land there.
- Model swaps invalidate enrollments; `model_id` and `model_sha` are stored with every voiceprint, and because no audio is kept ([ADR 0018](0018-no-audio-retention.md)) a swap means each person re-enrolls (about two minutes).

> **Amended 2026-11-01.** The last consequence originally said "re-embedding is a documented flow". Re-embedding needs stored enrollment audio, which [ADR 0018](0018-no-audio-retention.md) rules out; the flow is re-enrollment.

## Alternatives considered

- **Voice as a login factor**: insecure against replay; legally fraught as biometric authentication.
- **Always ask "who is this?"**: destroys the hands-free experience.
- **Per-user wake words only**: a useful prior later, not an identity.
