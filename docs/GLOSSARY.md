# Glossary

> Status: Draft spec · 2026-11-01 · Applies to: all milestones

Definitions only; behaviour and numbers live in the linked topic doc, which wins on conflict. Code spellings (`risk_ceiling`, `speaker.final`) are given where they exist.

Related: [Architecture](ARCHITECTURE.md) · [Voice pipeline](VOICE_PIPELINE.md) · [Identity and trust](IDENTITY_AND_TRUST.md) · [Modules](MODULES.md) · [MCP](MCP.md) · [Web UI](WEB_UI.md) · [Data and privacy](DATA_AND_PRIVACY.md) · [ADR index](adr/README.md)

---

## A

**Act** — The risk tier for ordinary device control (lights, media, timers). [IDENTITY §4](IDENTITY_AND_TRUST.md)

**Activity timeline** — The "why did you do that?" view, one row per turn; who sees whose turns is in [IDENTITY §10](IDENTITY_AND_TRUST.md). [WEB_UI §5.8](WEB_UI.md)

**Admin** — The preset with full grants and the `critical` ceiling. [IDENTITY §9](IDENTITY_AND_TRUST.md)

**AEC** — Echo cancellation on the ReSpeaker array, which is why all playback goes through it. [VOICE §2](VOICE_PIPELINE.md)

**Agent loop** — The core routine that runs one turn from prompt to spoken reply. [ARCHITECTURE §5](ARCHITECTURE.md)

**AGENT.md** — The mandatory model-facing guide every module ships, served by `read_guide`. [MODULES §10](MODULES.md)

**Announce (`ctx.announce`)** — The only way a module speaks outside a turn. [MODULES §5.3](MODULES.md)

**Approval** — A pending second-factor request bound to one profile, exact arguments and a single-use nonce. [IDENTITY §8.2](IDENTITY_AND_TRUST.md)

**Audio service** — The `beepboop-audio` container that owns all PCM. [ARCHITECTURE §3](ARCHITECTURE.md)

## B

**Babysitter** — Not a preset: the "Guest access" UI action that grants the built-in `guest` profile ordinary grants with an expiry. [IDENTITY §9](IDENTITY_AND_TRUST.md#9-presets)

**Barge-in** — Interrupting beepboop while it speaks (v0.3). [VOICE §15.2](VOICE_PIPELINE.md)

**Bounce page** — The v0.2 static page that redirects OAuth providers back to the Pi, which validates the state itself. [MCP §10](MCP.md)

## C

**Canonical events** — The turn events that carry transcript, identity or speech across the socket. [ARCHITECTURE §4.2](ARCHITECTURE.md#42-event-catalog)

**`capture_pin`** — Core → audio request for a spoken PIN, verified inside the audio service. [ARCHITECTURE §4.2](ARCHITECTURE.md#42-event-catalog)

**Ceiling (`risk_ceiling`)** — The highest risk tier a profile can ever reach. [IDENTITY §4](IDENTITY_AND_TRUST.md)

**Child** — The preset for children: restricted grants, `act` ceiling, history visible to guardians. [IDENTITY §9](IDENTITY_AND_TRUST.md)

**Claim code** — The spoken first-boot code that creates the first admin. [WEB_UI §5.1](WEB_UI.md)

**Confidence (`confidence`)** — The Principal field carrying the speaker-ID score; never proof of presence. [IDENTITY §3](IDENTITY_AND_TRUST.md)

**Confirm** — The policy outcome that proceeds only after a second factor. [IDENTITY §7](IDENTITY_AND_TRUST.md)

**Conformance suite** — The shared test every provider adapter must pass. [LLM_PROVIDERS §6](LLM_PROVIDERS.md), [VOICE §17](VOICE_PIPELINE.md)

**Conversation window** — The 90 s after a reply in which "Hey beepboop, …" continues the same conversation (wake word required). [VOICE §15.1](VOICE_PIPELINE.md)

**Core** — The `beepboop-core` container: agent loop, policy, registry, modules, web, SQLite; never PCM. [ARCHITECTURE §3](ARCHITECTURE.md)

**Core tools** — The always-loaded tools in every prompt. [MCP §4](MCP.md)

**Critical** — The highest risk tier; always needs a second factor. [IDENTITY §4](IDENTITY_AND_TRUST.md)

## D

**Deferred tool** — A tool the model discovers via `find_tools` instead of seeing in every prompt. [MCP §6](MCP.md)

**Dry run** — Module mode in which `ctx.http`, `ctx.ha` and `ctx.announce` record intended effects instead of acting. [MODULES §12.3](MODULES.md)

## E

**Earcon** — A short non-speech sound with a fixed meaning. [VOICE §13](VOICE_PIPELINE.md)

**Endpointing** — Deciding the user has stopped speaking; the hangover is its own latency stage. [VOICE §7.2](VOICE_PIPELINE.md)

**Enrollment** — Speaking consented phrases into the Pi's mic to build a voiceprint; only embeddings are kept, never the audio. [VOICE §9](VOICE_PIPELINE.md), [ADR 0018](adr/0018-no-audio-retention.md)

**Entity inbox** — Where new HA entities wait, default-deny, until granted. [IDENTITY §6.2](IDENTITY_AND_TRUST.md)

**Entity-registry UUID** — HA's stable per-entity id; grants key HA resources by it. [IDENTITY §6.1](IDENTITY_AND_TRUST.md)

**Envelope encryption** — Secrets and voiceprints encrypted with a data key wrapped by the KEK. [DATA §6.3](DATA_AND_PRIVACY.md)

**`evals.yaml`** — A module's utterance → expected tool, args and decision cases. [MODULES §12](MODULES.md)

**Event protocol** — The JSON-lines stream over `unix:///run/beepboop/audio.sock`; text only. [ARCHITECTURE §4](ARCHITECTURE.md#4-audio--core-event-protocol)

## F

**Fallback ladder** — The ordered LLM configurations tried on error, ending in fast-path-only. [LLM_PROVIDERS §3.6](LLM_PROVIDERS.md)

**Fast path** — The deterministic offline matcher for common commands; same tool call, same policy. [LLM_PROVIDERS §12](LLM_PROVIDERS.md)

**Final ID** — The speaker-ID decision carried by `speaker.final` and used to build the Principal. [VOICE §8.2](VOICE_PIPELINE.md)

**`find_tools`** — Core tool that searches the registry for deferred tools. [MCP §6](MCP.md)

**Front door** — One of the two entries into the single registry: in-process, or `/mcp`. [MCP §1](MCP.md)

## G

**Grant** — The only permission primitive: subject → resource → max risk → optional schedule. [IDENTITY §5](IDENTITY_AND_TRUST.md)

**Group** — A grant subject naming several profiles; built-ins `household`, `adults`, `children`, no others in v0.1. [IDENTITY §5](IDENTITY_AND_TRUST.md)

**Guest** — The profile used below the speaker-ID threshold, and the lowest preset. [IDENTITY §9](IDENTITY_AND_TRUST.md), [DATA §8](DATA_AND_PRIVACY.md)

## H

**`ha` module** — The module that maps HA entities and services to tools; the HA client lives in `core/beepboop/ha/`. [HOME_ASSISTANT](HOME_ASSISTANT.md)

**Household config file** — `/config/beepboop.yaml`, read-only in core and audio: hardware, locale, providers. [ARCHITECTURE §9](ARCHITECTURE.md)

## I

**Indirect HA service** — Scripts, scenes, automations and the like; default `critical`. [IDENTITY §6.3](IDENTITY_AND_TRUST.md)

## K

**KEK** — The key derived from the Pi's OTP device key that wraps the data key. [DATA §6.3](DATA_AND_PRIVACY.md)

**Kind (module)** — `native`, `ha` or `mcp_proxy` (v0.3). [MODULES §4.2](MODULES.md)

**Kind (profile)** — `admin | member | child | guest`; voice policy never branches on it. [IDENTITY §2](IDENTITY_AND_TRUST.md)

## L

**Latency contract** — The per-turn promise for first feedback and first word, owned by [ADR 0015](adr/0015-latency-contract-and-fast-path.md); gates in [ROADMAP](ROADMAP.md).

**`LLMProvider`** — The minimal protocol every LLM adapter implements; it translates, never loops. [LLM_PROVIDERS §2](LLM_PROVIDERS.md)

## M

**Manifest (`manifest.yaml`)** — A module's contract; its JSON Schema is the only schema for tool args. [MODULES §4](MODULES.md)

**Margin (`margin`)** — Top-1 minus top-2 speaker score; gates `personal` together with confidence. [VOICE §8.4](VOICE_PIPELINE.md)

**MCP** — Model Context Protocol; beepboop is a server at `/mcp` (v0.2). [MCP](MCP.md)

**Member** — The default adult preset. [IDENTITY §9](IDENTITY_AND_TRUST.md)

**microWakeWord** — The default wake engine; openWakeWord is a plugin. [VOICE §6.1](VOICE_PIPELINE.md)

**Module** — A folder under `modules/<id>/` that gets a ToolContext and imports no SDKs. [MODULES §2](MODULES.md)

**Module card** — The short per-module summary in the system prompt. [MCP §7](MCP.md)

## P

**Persona** — The household voice contract with per-profile overrides; personas are modules. [MODULES §5.6](MODULES.md)

**Personal** — The risk tier for a person's own data; requires confidence ≥ τ_strict and margin ≥ m, otherwise a second factor (web or PIN) or deny. [IDENTITY §4](IDENTITY_AND_TRUST.md)

**`pin.result`** — Audio → core event carrying only the outcome of `capture_pin`. [ARCHITECTURE §4.2](ARCHITECTURE.md#42-event-catalog)

**Policy middleware** — `policy.authorize(principal, tool, args)`; every front door passes it, no prompt implements it. [IDENTITY §7](IDENTITY_AND_TRUST.md)

**Preset** — A grant template plus ceiling plus schedules (Admin, Member, Child, Guest). [IDENTITY §9](IDENTITY_AND_TRUST.md#9-presets)

**Principal** — The identity attached to every call; model output can never change it. [IDENTITY §3](IDENTITY_AND_TRUST.md)

**Profile** — A person known to the household. [IDENTITY §2](IDENTITY_AND_TRUST.md)

**Provider** — A pluggable stage behind a Protocol (`WakeProvider`, `SpeakerProvider`, `SttProvider`, `TtsProvider`, `LLMProvider`), registered by entry point. [VOICE §17](VOICE_PIPELINE.md), [LLM_PROVIDERS §2](LLM_PROVIDERS.md)

**Provisional ID** — The early speaker ID from the wake-word audio, used only to warm caches. [VOICE §8.2](VOICE_PIPELINE.md)

## R

**Read** — The lowest risk tier: state, weather, time. [IDENTITY §4](IDENTITY_AND_TRUST.md)

**`read_guide`** — Core tool returning a module's AGENT.md. [MCP §6](MCP.md)

**Registry** — The single tool catalogue; `registry.dispatch` is the one path to execution. [MCP §1](MCP.md)

**Reply window** — The 8 s after beepboop asks a question, in which the answer needs no wake word. [VOICE §15.1](VOICE_PIPELINE.md)

**Resolver** — Matches spoken names to the granted entity UUIDs. [MCP §5](MCP.md)

**Risk tier (`risk`)** — `read | act | personal | critical`, declared per tool, enforced by policy. [IDENTITY §4](IDENTITY_AND_TRUST.md)

## S

**Schedule** — An optional time window on a grant. [IDENTITY §5.1](IDENTITY_AND_TRUST.md)

**Scope (module)** — `household` (shared) or `profile` (one connection per person). [MODULES §4.3](MODULES.md)

**Second factor** — Proof beyond voice: web confirm by default, spoken PIN as opt-in fallback. [IDENTITY §8](IDENTITY_AND_TRUST.md)

**SETUP.md** — A module's human-facing setup guide. [MODULES §11](MODULES.md)

**sherpa-onnx** — The single runtime for VAD, speaker ID, STT and TTS. [VOICE §1](VOICE_PIPELINE.md)

**Speaker ID** — Who spoke, from a CAM++ embedding; personalization, not authentication. [VOICE §8](VOICE_PIPELINE.md), [ADR 0006](adr/0006-voice-id-is-personalization.md)

**Spawn (`ctx.spawn`)** — How a module runs supervised background work. [MODULES §5.3](MODULES.md)

**Spoken PIN** — The optional second factor captured and verified inside the audio service. [VOICE §12](VOICE_PIPELINE.md)

## T

**Taint (`tainted`)** — After an `untrusted_output` tool runs, every later non-`read` call needs confirmation. [IDENTITY §3.2](IDENTITY_AND_TRUST.md)

**Tau (`tau`)** — The speaker-ID accept threshold; `tau_strict` for `personal`. [VOICE §8.4](VOICE_PIPELINE.md)

**"That wasn't me"** — One-tap correction that adds a negative example and an audit entry. [VOICE §8.8](VOICE_PIPELINE.md)

**Tier-0 / Tier-1 HTTPS** — Local CA by default; a real hostname unlocks passkeys, external MCP, phone confirm. [ADR 0013](adr/0013-https-tiers.md)

**Timer** — Timers, alarms and reminders in the `timers` module. [MODULES §16](MODULES.md)

**`ToolContext`** — What a handler receives instead of SDKs: `ha`, `http`, `kv`, `secrets`, `announce`, `spawn`, `dry_run`, `principal`. [MODULES §5.3](MODULES.md)

**Turn** — One interaction from `wake` to the end of the reply, identified by `turn_id`. [ARCHITECTURE §5](ARCHITECTURE.md)

## U

**`untrusted_output`** — Manifest flag for tools returning third-party text; running one taints the turn. [MCP §11](MCP.md)

## V

**VAD** — Silero voice activity detection; drives endpointing. [VOICE §7.1](VOICE_PIPELINE.md)

**`via`** — Principal field naming the front door: `voice`, `web`, `oauth`, `pat`. [IDENTITY §3.1](IDENTITY_AND_TRUST.md)

**Voiceprint** — A profile's speaker centroids plus calibration, encrypted at rest, never leaving the Pi. [DATA §6.4](DATA_AND_PRIVACY.md)

## W

**Wake word** — "Hey beepboop", detected by microWakeWord; emits `wake`. [VOICE §6](VOICE_PIPELINE.md)

**Waterfall** — Per-turn stage timings shown in Activity and produced by `beepboop bench`. [OPERATIONS §11](OPERATIONS.md#11-observability)

**Wyoming** — The Open Home Foundation's LAN speech protocol, used only at the edges. [ARCHITECTURE §7](ARCHITECTURE.md)
