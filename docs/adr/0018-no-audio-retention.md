# 0018. No audio is ever persisted; a speaker-model change means re-enrollment

> Status: Accepted · 2026-11-01 · Applies to: M0, v0.1

"No audio kept" is absolute in v0.1. PCM exists only in the audio service's memory for the duration of a turn. There are no opt-in exceptions: no enrollment clips, no false-wake queue, no debug captures. A speaker-model change invalidates voiceprints and each person re-enrolls (about two minutes).

Related: [docs/DATA_AND_PRIVACY.md §1](../DATA_AND_PRIVACY.md#1-principles) · [docs/DATA_AND_PRIVACY.md §7](../DATA_AND_PRIVACY.md#7-retention-defaults) · [docs/VOICE_PIPELINE.md §1](../VOICE_PIPELINE.md#1-principles) · [docs/VOICE_PIPELINE.md §9](../VOICE_PIPELINE.md#9-enrollment) · [ADR 0006](0006-voice-id-is-personalization.md) · [ADR 0008](0008-audio-core-process-split.md) · [ADR 0011](0011-sqlite-single-file.md)

## Context

The first draft promised "no audio kept" and then carved out three opt-in exceptions: encrypted enrollment clips (`voiceprints.keep_audio`, `voice_samples.audio_ref`) so a speaker-model upgrade could re-embed instead of re-enrolling; a false-wake clip queue for wake-word retraining; and a debug retention switch. The consistency review (CON-17) found the README, the privacy ledger and the audio-service hard rule all state the promise without the exceptions, and that each exception stores voice recordings on the device the promise says never stores them. The user ruled on 2026-11-01: absolute in v0.1.

The cost is small. Re-enrollment through the Pi's mic takes about two minutes per person, and speaker-model changes are rare, explicit host operations. The wake-word false-accept gate (G5) is measured from the consented bench corpus, not from household clips.

## Decision

The audio service never writes PCM to any persistent medium and core never receives it. `keep_audio`, `audio_ref`/`audio_enc`, the `enroll/` directory, the "keep my enrollment recordings" setting, the false-wake clip buffer and review page, and any debug audio retention are removed from the spec and may not return without a superseding ADR. Enrollment and adaptation store embeddings only, tagged with `{model_id, model_sha}`. When `model_id` changes, core marks every voiceprint stale, everyone is identified as Guest until re-enrolled, and the UI explains why and offers the two-minute flow. "Report false wake" in Activity marks the turn; there is no clip to review. ROADMAP v0.1 acceptance criterion 8 scans the filesystem for audio bytes after a turn.

## Consequences

- One promise, stated identically everywhere, with a CI test behind it; no consent text, retention row or export path for clips.
- A speaker-model upgrade is a household event; the update flow warns before switching models.
- Wake-word retraining from household false wakes is impossible in v0.1. A v0.2 ADR may propose an opt-in, encrypted, bounded queue (the review's sketch: 7 days, ≤ 50 clips, admin opt-in) and supersede this one; [ROADMAP](../ROADMAP.md#v02-your-accounts-your-devices) carries a one-line note.
- Audio debugging uses the bench corpus and `beepboop-audio serve --fake-mic`, never live captures.

## Alternatives considered

- **Opt-in encrypted enrollment clips**: convenient for model upgrades, but makes the promise false for the most invested users and adds a file store, export path and deletion cascade for a rare event.
- **Opt-in false-wake queue in v0.1**: useful training signal, but stores exactly the audio a TV-triggered false wake captured, possibly a stranger's voice.
- **Crash-time dump of the in-memory ring buffer**: still PCM on disk; rejected for the same reason.
