# 0008. Audio and core are separate processes; only text events cross

> Status: Accepted · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

`beepboop-audio` owns PCM end to end: wake word, VAD, speaker ID, STT, TTS, earcons and spoken-PIN capture. `beepboop-core` owns everything else. They exchange only JSON-lines text events over a unix socket.

Related: [docs/ARCHITECTURE.md](../ARCHITECTURE.md) · [docs/VOICE_PIPELINE.md](../VOICE_PIPELINE.md) · [docs/OPERATIONS.md](../OPERATIONS.md) · [ADR 0003](0003-local-first-speech.md) · [ADR 0014](0014-second-factor-web-confirm.md)

## Context

The latency-critical path is wake → VAD → STT → (fast path or LLM) → TTS → DAC. Every process hop that carries audio costs serialization and scheduling jitter; HA's Wyoming-based pipelines pay this per chunk. Inference needs pinned cores and real-time priority, while core needs an asyncio loop, a web server and database writes; one process cannot serve both well under the GIL. Privacy rules ("no audio kept", "PINs never reach the LLM") are easier to enforce when audio physically cannot reach the code that talks to the cloud. The kernel's OOM killer must not take the audio service mid-utterance because HA had a bad day.

## Decision

Two containers. The audio service holds the ALSA device, runs with `network_mode: none`, is pinned to cores 2–3 with `nice -5` and a real-time capture thread, and has its own `mem_limit` and a lower `oom_score_adj`. It emits `wake`, `speaker.provisional {profile_id|GUEST, score, margin}`, `speaker.final {...}`, `stt.partial {text}`, `stt.final {text, locale}` and consumes `speak {text, voice, turn_id}`, `cancel {turn_id}` and `capture_pin {turn_id, profile_id}`; in PIN mode it hashes and compares locally and replies with a boolean. Framing is JSON-lines over a unix socket in a shared tmpfs; the protocol is defined by pydantic models in `audio/beepboop_audio/events/`. Wyoming is used only at the edges: a client adapter for external STT/TTS servers and, later, a satellite server. Core never sees PCM, and the audio service never sees the network.

## Consequences

- The latency-critical path has zero audio hops; M0 can measure each stage independently.
- Core can be developed and tested with a fake audio service (`--fake-mic`), and the audio service with a fake core.
- Barge-in is a `cancel` event plus local playback control, with no cross-process audio coordination.
- Two deployables to version together; the event protocol is versioned and backwards compatible within a minor release.
- Cloud speech plugins (v0.2) must be an explicit override that gives audio a network, which is the desired visibility.

## Alternatives considered

- **One process**: GIL contention between inference and the web server; an OOM or exception in core kills audio.
- **Wyoming as the internal bus**: a hop per audio chunk, no auth, and speaker ID would not fit its message types.
- **Audio in HA's Assist pipeline**: HA has no speaker ID and would make beepboop a downstream intent handler.
