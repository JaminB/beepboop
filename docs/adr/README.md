# Architecture Decision Records

> Status: Draft spec · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

One short record per settled design decision: what forced it, what we chose, what it costs, and what we rejected. ADRs outrank topic docs and are outranked only by `docs/research/DECISIONS.md`. When a topic doc and an ADR disagree, the topic doc is wrong; fix it in the same PR.

Related: [CLAUDE.md](../../CLAUDE.md) · [CONTRIBUTING.md](../../CONTRIBUTING.md) · [docs/ARCHITECTURE.md](../ARCHITECTURE.md) · [docs/ROADMAP.md](../ROADMAP.md)

## Index

| # | Title | Status | Implements |
|---|---|---|---|
| [0001](0001-home-assistant-backbone.md) | Home Assistant Core is the integration backbone, over one WebSocket, behind our policy | Accepted | [HOME_ASSISTANT.md](../HOME_ASSISTANT.md) |
| [0002](0002-python-core.md) | Python 3.12, FastAPI and `uv` for both services | Accepted | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| [0003](0003-local-first-speech.md) | Speech is local by default; cloud speech is an opt-in plugin | Accepted | [VOICE_PIPELINE.md](../VOICE_PIPELINE.md) |
| [0004](0004-beepboop-is-an-mcp-server.md) | beepboop is an MCP server: one registry, two front doors | Accepted | [MCP.md](../MCP.md) |
| [0005](0005-risk-tiers-and-grants.md) | Four risk tiers and one grant mechanism, enforced in middleware | Accepted | [IDENTITY_AND_TRUST.md](../IDENTITY_AND_TRUST.md) |
| [0006](0006-voice-id-is-personalization.md) | Voice ID is personalization, not authentication | Accepted | [IDENTITY_AND_TRUST.md](../IDENTITY_AND_TRUST.md), [VOICE_PIPELINE.md](../VOICE_PIPELINE.md) |
| [0007](0007-module-contract.md) | The module contract: a folder, a manifest, a mandatory AGENT.md, no SDK imports | Accepted | [MODULES.md](../MODULES.md) |
| [0008](0008-audio-core-process-split.md) | Audio and core are separate processes; only text events cross | Accepted | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| [0009](0009-microwakeword-and-sherpa-onnx.md) | microWakeWord for wake, sherpa-onnx for VAD, speaker ID, STT and TTS | Accepted | [VOICE_PIPELINE.md](../VOICE_PIPELINE.md) |
| [0010](0010-llm-provider-protocol-bedrock-default.md) | Our own `LLMProvider` protocol; Bedrock ConverseStream with Claude Haiku 5.5 as default | Accepted | [LLM_PROVIDERS.md](../LLM_PROVIDERS.md) |
| [0011](0011-sqlite-single-file.md) | One SQLite file in WAL mode, single writer, envelope-encrypted secrets | Accepted | [DATA_AND_PRIVACY.md](../DATA_AND_PRIVACY.md) |
| [0012](0012-sveltekit-static-spa.md) | SvelteKit static SPA served by FastAPI; no Node in production | Accepted | [WEB_UI.md](../WEB_UI.md) |
| [0013](0013-https-tiers.md) | HTTPS in two tiers: local CA by default, real domain unlocks passkeys and external MCP | Accepted | [WEB_UI.md](../WEB_UI.md), [OPERATIONS.md](../OPERATIONS.md) |
| [0014](0014-second-factor-web-confirm.md) | The v0.1 second factor is web UI confirm without push, with an optional spoken PIN | Accepted | [IDENTITY_AND_TRUST.md](../IDENTITY_AND_TRUST.md) |
| [0015](0015-latency-contract-and-fast-path.md) | The latency contract and the deterministic fast path; no local LLM | Accepted | [VOICE_PIPELINE.md](../VOICE_PIPELINE.md), [ROADMAP.md](../ROADMAP.md) |
| [0016](0016-docker-compose-mit.md) | Four Docker Compose containers on Pi OS Lite; MIT license; models downloaded, not vendored | Accepted; licence clause superseded by 0020 | [OPERATIONS.md](../OPERATIONS.md) |
| [0017](0017-phased-roadmap.md) | A gated roadmap: M0 proves the Pi before v0.1 begins | Accepted | [ROADMAP.md](../ROADMAP.md) |
| [0018](0018-no-audio-retention.md) | No audio is ever persisted; a speaker-model change means re-enrollment | Accepted | [DATA_AND_PRIVACY.md](../DATA_AND_PRIVACY.md), [VOICE_PIPELINE.md](../VOICE_PIPELINE.md) |
| [0019](0019-shared-protocol-package-and-negotiated-embedding-dim.md) | One light protocol package shared by both services; the embedding length is negotiated in `hello` | Accepted | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| [0020](0020-gpl-3-and-espeak-phonemiser.md) | GPL-3.0-or-later; espeak-ng phonemiser in the TTS path; lessac-derived voices accepted (D1) | Accepted | [OPERATIONS.md §13](../OPERATIONS.md#13-model-and-licensing-hygiene), [VOICE_PIPELINE.md](../VOICE_PIPELINE.md) |

## Writing an ADR

This is the only ADR how-to; [CLAUDE.md §8](../../CLAUDE.md) and [CONTRIBUTING.md §6](../../CONTRIBUTING.md) link here.

1. Copy the template below to `docs/adr/NNNN-short-kebab-title.md` using the next free number. Open the PR with status `Proposed`; discussion happens on the PR, and reviewers favour the minimal and extensible option when two are otherwise equal.
2. Keep it between 150 and 400 words. If you need more, the decision is probably two decisions.
3. Statuses: `Proposed` → `Accepted` (on merge) → `Superseded by NNNN` or `Deprecated`. Never rewrite the Decision section of an Accepted ADR: add a dated `> **Amended YYYY-MM-DD.**` note for a clarification, or write a new ADR that says `Supersedes NNNN` and mark the old one `Superseded by MMMM`.
4. Ground it: cite the research report, benchmark result or incident that forced the decision. Numbers that are not measured are labelled targets.
5. Add the row to the index above, and link the ADR from the topic doc that implements it.
6. ADRs ship in the same PR as the change they justify ([CLAUDE.md](../../CLAUDE.md) hard rule 9).

### Template

```markdown
# NNNN. Title in the imperative or as a noun phrase

> Status: Proposed · YYYY-MM-DD · Applies to: <milestones>

One or two sentences saying what was decided.

Related: [docs/TOPIC.md](../TOPIC.md) · [ADR NNNN](NNNN-....md)

## Context
What forced a decision. Constraints, measurements, research findings.

## Decision
One paragraph, present tense. "We use X for Y."

## Consequences
What becomes easier, what becomes harder, what we gave up, what must be watched.

## Alternatives considered
- **Alternative A**: why not.
- **Alternative B**: why not.
```
