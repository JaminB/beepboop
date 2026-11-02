# 0003. Speech is local by default; cloud speech is an opt-in plugin

> Status: Accepted · 2026-11-01 · Applies to: M0, v0.1, v0.2

Wake word, VAD, speaker ID, STT and TTS all run on the Pi by default. Cloud STT and TTS exist as opt-in plugins, enabled per household, with the privacy ledger stating who hears your audio.

Related: [docs/VOICE_PIPELINE.md](../VOICE_PIPELINE.md) · [docs/DATA_AND_PRIVACY.md](../DATA_AND_PRIVACY.md) · [ADR 0008](0008-audio-core-process-split.md) · [ADR 0009](0009-microwakeword-and-sherpa-onnx.md) · [ADR 0015](0015-latency-contract-and-fast-path.md)

## Context

Audio is the most sensitive data a home assistant handles, and voiceprints are biometric data under GDPR Art. 9 and BIPA. Users distrust devices that stream their rooms to the cloud, and Google's and Amazon's own documentation shows cloud voice assistants getting worse, not better, at the trust problem. On the engineering side, a streaming local STT model gives partial results while the user speaks, so only a ~100 ms tail remains after end-of-speech, and local TTS can start on the first sentence without a network round trip. The research found sherpa-onnx streaming zipformer "runs in real time on a Raspberry Pi 4" and Piper-format voices synthesize well under real time, but no concurrent Pi 4 measurements exist. Cloud STT (Deepgram Nova-3 claims < 300 ms) would also remove the only offline path.

## Decision

Local speech is the default and the only configuration shipped in v0.1. The audio container runs with `network_mode: none`, which makes "audio never leaves the Pi" a property of the deployment rather than a promise. Cloud STT (Deepgram) and TTS (Amazon Polly) ship in v0.2 as providers behind the same `SttProvider` and `TtsProvider` protocols, enabled through a Compose override that gives audio a network and flips the privacy ledger. Wyoming is used only at the edges to consume external STT/TTS servers or future satellites. English ships first; a `locale` travels with every profile and event so other languages can be added without redesign.

## Consequences

- The fast path works offline end to end, which the latency contract depends on.
- M0 must prove local STT fits (gate G2). If it does not, cloud STT becomes the default by a superseding ADR, and the README says so prominently.
- Model licensing becomes our problem: see [ADR 0009](0009-microwakeword-and-sherpa-onnx.md) and [docs/OPERATIONS.md §13](../OPERATIONS.md#13-model-and-licensing-hygiene).
- Local speech consumes most of the audio container's RAM budget and both of its cores.

## Alternatives considered

- **Cloud-first speech**: simpler and more accurate, but no offline path, more latency variance, and every utterance leaves the house.
- **Hybrid with automatic cloud fallback on load**: silently changes who hears your audio; rejected. Fallback to local when cloud is unreachable is fine and shipped.
- **HA's Assist pipeline with Wyoming as the internal bus**: adds a process hop per audio chunk, has no auth, and would make speaker ID a bolt-on.
