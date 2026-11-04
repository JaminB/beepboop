# beepboop: plan for writing the spec

## Context
We are creating `beepboop`, a new public GitHub repo for a home voice assistant on a Raspberry Pi 4. It wakes on "Hey beepboop", identifies the speaker by voice, and acts on that person's profile. The profile decides which Home Assistant devices and personal integrations the speaker can reach. beepboop exposes those integrations as MCP tools.

This phase writes the specification only, no product code. The deliverables are README.md, CLAUDE.md and supporting docs that let engineers start building.

Design values are modularity, minimalism, easy extension, a pleasant developer experience, and detailed documentation from day one. Documentation also matters because the agent reads module docs to learn how to use each profile's integrations.

Research is complete. Workflow `wf_103f0e3b-c46` ran 7 researchers on Sonnet and Opus, then an Opus synthesis. The output is at `C:\Users\Jamin\AppData\Local\Temp\claude\C--Users-Jamin\12131f08-aff0-40a1-9888-a40675ff8b4e\tasks\wq4m62j6j.output`.

## Settled design (user decisions + research rulings)
| Area | Decision |
|---|---|
| Hardware | Pi 4: 8 GB recommended, 4 GB minimum. USB3 SSD and a fan are required. ReSpeaker XVF3800 USB array (onboard AEC). All playback goes through the array. |
| Packaging | Raspberry Pi OS Lite 64-bit, Docker Compose. 4 containers: `caddy`, `beepboop-core`, `beepboop-audio`, `homeassistant` (pinned by digest). MIT license. |
| Process split | The audio service owns PCM end to end: wake word, VAD, speaker ID, STT, TTS, earcons, and spoken-PIN capture. Only text events cross to core over a unix socket. Wyoming is used only at the edges (external STT/TTS, future satellites). |
| Wake word | microWakeWord via `pymicro-wakeword`. Custom "Hey beepboop" model built by a documented pipeline in `tools/train_wakeword/`. openWakeWord is a plugin. Porcupine is excluded: its free tier ended 2026-06. |
| Speaker ID | CAM++ via sherpa-onnx. A provisional ID comes from the wake-word pre-roll, which is text-dependent. The final ID is ready at end-of-speech, computed in parallel with STT. Multi-centroid scoring with a top-1 threshold plus a margin. Below threshold, the Guest profile is used. Voice ID is treated as **personalization, not authentication**. |
| STT / TTS | Local by default: sherpa-onnx streaming zipformer for STT, Piper-format voices run by sherpa-onnx for TTS. Cloud plugins (Deepgram, Polly) are opt-in. English in v0.1, with a locale carried end to end so other languages can be added. |
| LLM | Our own minimal `LLMProvider` protocol. Default is Bedrock ConverseStream with Claude Haiku 5.5, prompt caching, and Sonnet 5.5 as opt-in. An OpenAI-compatible adapter covers BYOM. No LiteLLM. No local LLM on the Pi; a deterministic local fast path handles common device commands instead. |
| Backbone | Home Assistant Core over one WebSocket, behind a policy-wrapped client. HA's own MCP server is not used because it has no per-user scoping. HA is bound to loopback. HA is the first choice for integrations; native modules only where HA can't model per-person accounts. |
| MCP | beepboop is an MCP server: one tool registry with two front doors. The voice agent calls it in-process; external clients use `/mcp` over Streamable HTTP with the official `mcp` Python SDK. About 12 always-loaded intent tools with a server-side entity resolver, plus `find_tools` and `read_guide` for deferred module tools. External access requires Tier-1 HTTPS (own domain or Tailscale). |
| Trust | Risk tiers: `read` / `act` / `personal` / `critical`. Enforcement is deterministic middleware, never the prompt. One grant mechanism (subject → resource → max_risk → schedule). HA resources are keyed by entity-registry UUID. New entities are denied by default and land in an inbox. Indirect HA services (scripts, scenes, automations) default to `critical`. "Taint the turn" applies after untrusted tool output. Each profile has a risk ceiling. |
| Roles | Admin / Member / Child / Guest. These are grant templates plus a ceiling plus schedules, with no special-case code. Parents can see children's history. |
| Second factor | **Web UI confirm with no push**: beepboop says "approve on your phone" and the user approves in the web UI. A spoken PIN fallback is optional per profile; it is captured by the audio service and never reaches the LLM or transcripts. Push and passkeys come later with Tier-1 HTTPS. |
| Latency contract | Fast path ≈0.5 s, works offline. Audible feedback always within 1 s. Spoken answer within 1.5 s for turns without a tool, within 2 s for turns with a tool. Milestone M0 must prove this on a real Pi. |
| Web UI | SvelteKit (Svelte 5), adapter-static SPA served by FastAPI, LAN-only. Caddy with a local CA (Tier 0). Password + TOTP. First run uses a claim code the device speaks. Module settings forms are auto-rendered from manifest JSON Schema. |
| Data | One SQLite file (WAL). Envelope-encrypted secrets and voiceprints. No audio kept. Transcripts kept 30 days, viewable and deletable per user. Delete and export cascade through all data. |
| Modules | Each module is a folder: `manifest.yaml`, `module.py`, mandatory `AGENT.md`, `SETUP.md`, `evals.yaml`, `tests/`. Modules never import MCP, HA or LLM SDKs; they get a `ToolContext`. CLI: `beepboop module new` / `module check`. CI lints docs, schemas and description token budgets. |
| Persona | A household default persona with per-profile overrides (voice, speech rate, verbosity). Personas are modules. |
| OAuth | v0.2 adds a static bounce page on GitHub Pages that holds no secrets. HA flows use an `/auth/external/callback` route through Caddy. |
| Roadmap | **M0** proves the Pi (bench and go/no-go gates). **v0.1** covers the core loop and modules `ha`, `timers` (alarms and reminders), `lists`, `weather`, CalDAV calendar, Music Assistant via HA, plus persona, the web UI and the activity timeline. **v0.2** adds OAuth modules (Google, Spotify), external MCP, the OpenAI-compatible adapter, cloud speech and presence priors. **v0.3+** adds memory, routines and proactive announcements, barge-in, full parental controls, satellites, and a third-party MCP proxy. |
| Predicted extras in the spec | "Why did you do that?" activity timeline with "that wasn't me"; text chat to test as a given user; latency waterfall plus `beepboop bench`; new-device inbox; voice trust page; cost budgets; babysitter / time-boxed guest; backups and safe updates with rollback; privacy ledger. |

## Execution steps
1. **Scaffold.** Create `C:\Users\Jamin\beepboop` and run `git init`. Copy the 7 research reports and the synthesis into `docs/research/` for provenance.
2. **Writing workflow** (Fable, `model: 'fable'`, about 8 agents in a pipeline). Each writer gets the settled-design table above, the synthesis, and the relevant raw reports, and writes its files directly. Assignments:
   - `README.md`, `CLAUDE.md` (build conventions, layout, hard rules, how to add a module or provider), `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE` (MIT)
   - `docs/ARCHITECTURE.md` (containers, event protocol, provider seams, turn lifecycle) and `docs/GLOSSARY.md`
   - `docs/VOICE_PIPELINE.md` (wake word, speaker ID, enrollment, STT/TTS, latency budget)
   - `docs/IDENTITY_AND_TRUST.md` (principal, tiers, grants, roles, second factor, threat model)
   - `docs/MODULES.md`, the `docs/module-template/` skeleton (manifest, AGENT.md, SETUP.md and evals templates), and `docs/MCP.md`
   - `docs/HOME_ASSISTANT.md`, `docs/LLM_PROVIDERS.md`
   - `docs/WEB_UI.md` (principles, information architecture, ASCII wireframes, onboarding, HTTPS tiers), `docs/DATA_AND_PRIVACY.md`
   - `docs/OPERATIONS.md`, `docs/ROADMAP.md` (acceptance criteria per milestone), `docs/adr/0001-…` (one short ADR per decision in the table)
3. **Review workflow** (6 reviewers, one per angle, then a judge). Angles: minimalism/YAGNI, security and threat model, module-author experience and extensibility, Pi performance and ops, UX and UI joy, cross-doc consistency and contradictions. An Opus judge rules on the findings, favoring the minimal and extensible option. A Fable revision pass applies the accepted changes. Rejected findings go into `docs/research/REVIEW_LOG.md`.
4. **Final pass (me).** Read every doc and check it against the decision table. Check links. Any new contradiction goes back to the user.
5. **Publish.** Check `gh auth status`, run `gh repo create beepboop --public`, make the initial commit with Co-Authored-By, and push.

## Verification
- Every row of the decision table appears in at least one doc, and none of the docs contradict it. I will check this with a grep-based checklist.
- Every relative link in the docs resolves.
- The module template is complete enough that a new engineer could scaffold a module from the docs alone.
- CLAUDE.md lets a fresh Claude session start M0 without needing this conversation.
- The repo is public on GitHub and the README renders correctly.

## Post-review rulings (user, 2026-11-01)
| Area | Decision |
|---|---|
| Audio retention | **Absolute in v0.1**: no audio is ever persisted — no enrollment clips (`keep_audio`/`audio_ref` removed), no false-wake queue, no debug retention. A speaker-model change means re-enrollment (~2 min/person). An opt-in false-wake queue may be proposed for v0.2 via a new ADR (encrypted, 7 days, <=50 clips). |
| Doc size rule | (1) Each fact has one owning doc; others link. (2) Topic docs fully specify only M0/v0.1; v0.2+ appears as <=10-line invariants + ROADMAP link. (3) Soft budget ~5k words per topic doc; MODULES.md, docs/module-template/ and AGENT.md guidance are exempt (they teach the agent). |

## Owner rulings 2026-11-03 (after M0)

These override earlier rows in this file where they conflict.

- **License:** GPL-3.0-or-later replaces MIT ([ADR 0020](../adr/0020-gpl-3-and-espeak-phonemiser.md)). `piper-phonemize`/espeak-ng are product dependencies of the Piper TTS path.
- **D1:** lessac-derived Piper voices (research-only provenance) are accepted for wake-word generation and the default TTS voice.
- **D3:** OTP key programming is deferred; the argon2id passphrase KEK is the v0.1 default and OTP is an optional upgrade.
- **D4:** all M0 spec findings are resolved, see ROADMAP M0. CAM++ embeddings are 512-d, negotiated per connection; the event protocol is its own package `beepboop-protocol`, version 2 ([ADR 0019](../adr/0019-shared-protocol-package-and-negotiated-embedding-dim.md)).
- **Sequencing:** v0.1 software work may proceed in parallel with the M0 hardware gates. A failing gate still triggers its ROADMAP fallback ADR, and v0.1 code adapts.
