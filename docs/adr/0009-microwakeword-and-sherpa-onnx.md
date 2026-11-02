# 0009. microWakeWord for wake, sherpa-onnx for VAD, speaker ID, STT and TTS

> Status: Accepted · 2026-11-01 · Applies to: M0, v0.1, v0.2

The wake word "Hey beepboop" is a custom microWakeWord model run by `pymicro-wakeword`. Everything else in the local speech stack runs on sherpa-onnx: Silero VAD, CAM++ speaker embeddings, a streaming zipformer STT model and Piper-format VITS voices for TTS.

Related: [docs/VOICE_PIPELINE.md](../VOICE_PIPELINE.md) · [docs/OPERATIONS.md §13](../OPERATIONS.md#13-model-and-licensing-hygiene) · [ADR 0003](0003-local-first-speech.md) · [ADR 0006](0006-voice-id-is-personalization.md)

## Context

Porcupine's free tier ended 2026-06-30 and it is proprietary, which disqualifies it for an MIT project. openWakeWord is Apache-2.0 code but its pretrained models are CC BY-NC-SA, it has had no release since February 2024, and its training recipe is heavier. microWakeWord is Apache-2.0 end to end, ships manylinux aarch64 wheels (`pymicro-wakeword` 2.5.0), runs at a 30 ms stride, and is what the Home Assistant ecosystem and future ESP32 satellites use. For speaker ID, CAM++ has the best accuracy per FLOP in the comparison (1.72 GFLOPs, 0.73 % EER), roughly 2.5× faster than ECAPA-TDNN or ResNet34; scaled estimates put a 2 s embedding at 60–130 ms on a Pi 4, the only candidate with headroom under the 200 ms target. sherpa-onnx (Apache-2.0, 2.0.1) runs VAD, speaker embedding, streaming STT and TTS in one C++ runtime with Python bindings, and its official Pi 4 tables show real-time Whisper-tiny and real-time streaming zipformer. The maintained Piper engine (`piper1-gpl`) is GPL-3.0; linking it into an MIT process is not acceptable, but sherpa-onnx runs Piper-format voices directly.

## Decision

Wake: microWakeWord via `pymicro-wakeword`, with the stock "Hey beepboop" model produced by a reproducible pipeline in `tools/train_wakeword/` (Piper synthetic clips plus negatives, fine-tuned on household clips SEED-style). openWakeWord is an optional plugin behind the same `WakeProvider` protocol. Speech: sherpa-onnx for Silero VAD, CAM++ (WeSpeaker VoxCeleb, 192-d, fp32 first; int8 only after a household A/B), streaming zipformer transducer (English small) with contextual biasing from per-profile hotwords, and Piper-format VITS voices (low or medium) synthesized sentence by sentence. Kokoro is opt-in. whisper.cpp is the offline-fallback plugin; Moonshine is a plugin after a license check. Every model is listed in `models/manifest.yaml` with its license and downloaded at install.

## Consequences

- One runtime to pin, benchmark and understand; one set of thread settings to tune.
- No GPL code in a beepboop process; per-voice licenses are tracked individually.
- A custom wake model "requires a lot of experimentation" per its own README; M0 gate G5 measures false accepts on a soak corpus and the pipeline is documented so it can be regenerated.
- Model swaps for speaker ID invalidate enrollments; `model_id`/`model_sha` travel with voiceprints.

## Alternatives considered

- **openWakeWord by default**: NC models, stale project.
- **Porcupine**: proprietary, free tier gone.
- **ECAPA-TDNN or ResNet34**: 2–3× slower for no accuracy gain.
- **faster-whisper or whisper.cpp as primary STT**: batch decoding after end-of-speech; too slow for the tail budget.
- **`piper1-gpl` directly**: GPL.
