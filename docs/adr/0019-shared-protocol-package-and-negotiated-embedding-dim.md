# 0019. One light protocol package shared by both services; the embedding length is negotiated in `hello`

> Status: Accepted · 2026-11-03 · Applies to: M0, v0.1, v0.2, v0.3+

The audio ⇄ core event protocol and the redacting log setup live in their own workspace package, `beepboop-protocol` (`protocol/`, import `beepboop_protocol`), which both services depend on. The speaker-embedding length is no longer a protocol constant: audio announces it in `hello.speaker.dim` and both sides validate every embedding against it. The v0.1 value is 512. The protocol major becomes 2.

Related: [docs/ARCHITECTURE.md §4](../ARCHITECTURE.md#4-audio--core-event-protocol) · [docs/VOICE_PIPELINE.md §8.1](../VOICE_PIPELINE.md#81-model) · [ADR 0008](0008-audio-core-process-split.md) · [ADR 0009](0009-microwakeword-and-sherpa-onnx.md) · [ADR 0018](0018-no-audio-retention.md)

## Context

M0 built the protocol inside `audio/beepboop_audio/events/`. Core needs those models, but depending on `beepboop-audio` would put sherpa-onnx and numpy into core's image. Audio also needs the hard-rule-5 redaction processor (it handles spoken PINs), which lived in core. Separately, the M0 bench found that both pinned English VoxCeleb CAM++ exports for sherpa-onnx produce **512**-dimensional embeddings (`models/LICENSES.md`), while the spec and code pinned 192. VOICE_PIPELINE §17 already calls `SpeakerProvider` model-agnostic, and `hello` already carries `dim`.

## Decision

`protocol/` is a uv workspace member depending only on pydantic and structlog. It holds the event models, codec, channel and handshakes, socket server and client, the in-process fake, the JSON Schema, and `beepboop_protocol.log` (structlog setup and redaction). `beepboop` and `beepboop-audio` both depend on it; `beepboop.log` remains as a re-export for core code. A test fails if importing the package loads numpy or any speech runtime. An embedding is a list of 1 to 4096 finite floats; after the handshake, each channel drops a received `enroll.clip` or `profiles.sync` whose embedding length differs from the peer's `hello.speaker.dim` and refuses to send one. The protocol major is 2, because 192-float peers cannot talk to 512-float peers. No protocol 1 build was ever released.

## Consequences

- Core's image gets the protocol without speech runtimes; both services log through one redaction processor.
- A speaker-model change with a different dimension needs no protocol change. It still forces re-enrollment ([ADR 0018](0018-no-audio-retention.md)).
- `profiles.sync` carries 512 floats per centroid: about 6 KB of JSON per centroid, well within the 1 MiB line limit for a household.

## Alternatives considered

- **Move the speech runtimes to an optional extra of `beepboop-audio`**: core would still install the audio package and its import surface.
- **Keep a global constant set to 512**: the next embedder change would need another protocol bump.
- **Keep protocol major 1**: the change is unreleased, but a stale peer would fail on every embedding instead of at `hello`.
