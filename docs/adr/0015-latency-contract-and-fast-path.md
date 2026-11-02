# 0015. The latency contract and the deterministic fast path; no local LLM

> Status: Accepted · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

beepboop commits to a four-number latency contract measured from end-of-speech, carries it with a deterministic local fast path for common device commands, and runs no LLM on the Pi. M0 must prove the numbers on real hardware before v0.1 begins.

Related: [docs/ROADMAP.md](../ROADMAP.md) · [docs/VOICE_PIPELINE.md](../VOICE_PIPELINE.md) · [docs/OPERATIONS.md §11](../OPERATIONS.md#11-observability) · [ADR 0010](0010-llm-provider-protocol-bedrock-default.md) · [ADR 0017](0017-phased-roadmap.md)

## Context

The user's requirement was a reply within 1.5 seconds. The budget analysis shows a no-tool LLM turn at ≈1.28 s (endpoint 250, STT tail 100, context 30, Haiku TTFT ≈650 with a cached prefix and warm TLS, TTS first chunk 200, output 50) but a tool-using turn at ≈1.7 s because two LLM round trips cannot reliably fit. No component has published Pi 4 numbers under concurrent load with HA running. A local LLM cannot help: a 3B model prefilling a 3k-token prompt on a Pi 4 takes many seconds. Prior art agrees that the most-used commands (lights, timers, stop, lists) are simple enough to match deterministically.

## Decision

The contract, p50 from end-of-speech with HA running under `beepboop bench e2e`:

| Turn | First audible feedback | First spoken word | Offline |
|---|---|---|---|
| Fast path | — | **≈ 0.5 s** | **yes** |
| LLM, no tool | < 1.0 s | **< 1.5 s** | no |
| LLM + tool | **< 1.0 s** (earcon or cached ack) | **< 2.0 s** | no |

The fast path is a deterministic grammar in `core/beepboop/agent/` over the profile's granted entity index; the intents it covers are listed in [docs/LLM_PROVIDERS.md §12.1](../LLM_PROVIDERS.md). It runs the same `policy.authorize` as everything else, speaks from cached TTS clips ("Done"), and keeps working with no network. Everything else goes to the LLM with: a pre-warmed Bedrock connection on wake, a cached stable prefix, effort low, sentence-chunked streaming into TTS, and an earcon or pre-recorded ack when a tool call is pending. When the LLM is unreachable, the device says so within 1 s. Speculative LLM starts on partial transcripts are off in v0.1. Every turn records a stage waterfall; `beepboop bench` runs the contract in CI on a real Pi.

> **Amended 2026-11-01.** How the table is measured: **clock zero is the physical end of the user's speech** (the last VAD-positive frame), so the VAD hangover is an `endpoint` stage *inside* the budget. **Statistic:** p50 meets the number; p90 ≤ 1.3× the number; p95 is reported, not gated. The 1 s audible-feedback earcon is the one **p95 promise**. This table is the only statement of the contract; [docs/ROADMAP.md](../ROADMAP.md) holds the M0 gate thresholds (G1–G7) that test it, and other docs link rather than restate.

## Consequences

- The user explicitly accepted the 2.0 s tool-turn figure with sub-1-second audible feedback; the docs state it rather than promising 1.5 s for everything.
- The fast path grammar is a maintenance surface and must stay small; anything ambiguous goes to the LLM.
- Gate failures in M0 change the design (cloud STT, 8 GB minimum, or a renegotiated contract) by ADR, not by quietly moving targets.
- "Target" labels remain on every number until measured results with dates replace them.

## Alternatives considered

- **1.5 s for every turn**: not achievable with two LLM round trips on current TTFT.
- **A local small LLM as router or fallback**: too slow to prefill; a grammar is faster and testable.
- **Speculative LLM start on partials**: wastes tokens and risks acting on misheard text; revisit after v0.1.
- **HA's `conversation/process` as the fast path**: acts before our ACL check; a native grammar over our granted index is safer.
