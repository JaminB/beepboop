# 0017. A gated roadmap: M0 proves the Pi before v0.1 begins

> Status: Accepted · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

Development proceeds in four milestones: M0 (measure and gate), v0.1 (one house that knows your voice), v0.2 (your accounts, your devices), v0.3+ (memory, routines, more rooms). M0 has explicit go/no-go gates whose failure changes the design by ADR rather than by moving targets.

Related: [docs/ROADMAP.md](../ROADMAP.md) · [CLAUDE.md](../../CLAUDE.md) · [ADR 0015](0015-latency-contract-and-fast-path.md) · [ADR 0003](0003-local-first-speech.md)

## Context

Every Pi 4 performance figure in the design is an estimate: speaker-ID latency is scaled from x86 single-thread numbers, streaming-STT RTF comes from a vendor statement and one unverified blog, Bedrock TTFT for 5.x models has no published figure, and HA's RAM on a Pi 4 is a range. Three of those estimates sit on the critical path of the latency contract, and two plausible outcomes (local STT too slow; 4 GB too tight) would each change the default architecture. Writing v0.1 features before knowing would risk rewriting the audio service or the bill of materials. Prior art (Mycroft) shows what happens when a voice project commits to hardware and features before proving the pipeline.

## Decision

M0 ("prove the Pi", 2–3 weeks) builds only `beepboop bench`, the wake-word training pipeline, the event protocol models, a minimal Bedrock provider and the Compose skeleton, then measures seven gates on a 4 GB Pi with HA running: speaker ID p95 < 200 ms, STT RTF < 0.6 at 2 threads, Haiku 5.5 TTFT p50 < 700 ms, peak RAM < 3.2 GB, wake false accepts ≤ 0.5 per 24 h, the end-to-end contract, and no thermal throttling with the reference fan. It also verifies five pass/fail items (HA loopback binding, headless onboarding, XVF3800 AEC routing, container ALSA stability, OTP key access). A failed gate is resolved by a superseding ADR choosing the documented fallback (cloud STT default, 8 GB minimum, contract renegotiation). v0.1 ships the full local loop, the seven modules (`ha`, `timers`, `lists`, `weather`, `caldav`, `music`, `persona`), web confirm, the web UI and ops basics. v0.2 adds Tier 1 HTTPS, the external MCP door, OAuth modules, phone confirm, the OpenAI-compatible provider, cloud speech and one-click updates. v0.3+ adds memory, routines, barge-in, parental controls, satellites and the third-party MCP proxy. Each milestone carries testable acceptance criteria and an explicit out-of-scope list.

## Consequences

- The first weeks produce numbers and tooling, not features; the bench stays as the CI performance gate forever.
- Design changes forced by hardware are visible decisions with ADRs, not silent slippage.
- Features can move between milestones by PR; gates and the "indefinitely out of scope" list move only by ADR.
- The team must own at least one 4 GB and one 8 GB Pi 4 with the reference mic and fan case.

## Alternatives considered

- **Start with v0.1 features and benchmark later**: risks rewriting audio or BOM after the fact.
- **Target the Pi 5 instead**: more headroom, but the user's hardware is a Pi 4 and the fleet of existing Pi 4s is larger.
- **Ship cloud speech first, localize later**: inverts the privacy default and removes the offline path.
- **A single big v1**: no feedback until everything exists.
