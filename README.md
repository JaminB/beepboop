# beepboop

> Status: Draft spec · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

**`[ spec phase ]`** — no product code yet. The documents in this repo are the specification engineers and Claude Code sessions build from. The first code milestone is [M0: prove the Pi](docs/ROADMAP.md).

beepboop is an open-source (GPL-3.0-or-later) home voice assistant for a Raspberry Pi 4. It wakes on **"Hey beepboop"**, works out *who* is speaking, and acts on that person's profile: their Home Assistant devices, their calendar, their music, their lists. It is local-first, Home Assistant-powered, and exposes every integration as an MCP tool so the same capabilities reach the voice agent and any MCP client you choose.

Related: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/ROADMAP.md](docs/ROADMAP.md) · [CLAUDE.md](CLAUDE.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [docs/GLOSSARY.md](docs/GLOSSARY.md)

---

## Why another voice assistant?

Every open-source assistant we found treats the household as one anonymous user. Every commercial one that knows your voice sends it to someone else's cloud. beepboop is for households that want both: an assistant that knows the difference between a parent, a kid and a visitor, and that keeps every byte of audio inside the house.

**Who it's for.** People who already run (or are willing to run) Home Assistant, own a Pi 4, and are comfortable with `docker compose up`. Later milestones make it friendlier; v0.1 is for tinkerers who like a good spec.

**What makes it different**

| | beepboop | Typical open-source assistant | Alexa / Google |
|---|---|---|---|
| Knows who is speaking | Yes: on-device speaker ID, per-person profiles | No | Yes, in their cloud |
| Per-person integrations | Yes: your CalDAV, your Music Assistant player, your HA devices | Shared | Yes, in their cloud |
| Audio leaves the house | Never. Only text transcripts go to the LLM, and the UI says so | Varies | Yes |
| Capability model | One MCP tool registry, two front doors (voice agent in-process, `/mcp` for your clients) | Bespoke skills API | Closed |
| Safety for locks, alarms, purchases | Deterministic risk tiers enforced in middleware; a second factor for `critical` | Usually none | Voice PIN |
| Home backbone | Home Assistant Core, pinned by digest, behind a policy-wrapped client | Varies | Proprietary |
| Offline | Fast path handles common device commands with no internet | Varies | No |
| Licence | GPL-3.0-or-later | Mostly Apache/MIT | Proprietary |

Voice ID in beepboop is **personalization, not authentication**. It picks the right profile and, above a strict threshold, unlocks that person's own data; it never on its own unlocks anything `critical`. See [docs/IDENTITY_AND_TRUST.md](docs/IDENTITY_AND_TRUST.md) and [SECURITY.md](SECURITY.md).

---

## How a turn works

Sam walks into the kitchen and says: *"Hey beepboop, what's on my calendar tomorrow?"*

1. **Wake (t = 0).** The audio service's microWakeWord model fires on "Hey beepboop". An earcon plays, the LED turns on, and the ~1 s of pre-roll audio is already being embedded by the speaker-ID model. A `wake` event and then `speaker.provisional{profile: sam, score: 0.81}` cross the unix socket to core. Core pre-warms the Bedrock connection.
2. **Listen.** Streaming STT emits `stt.partial` events as Sam talks. In parallel, the speaker embedder fuses the wake-word audio with the first two seconds of the command and emits `speaker.final{profile: sam, score: 0.91, margin: 0.22}` before Sam has finished the sentence.
3. **End of speech (+250 ms).** VAD closes the utterance; the STT tail finalises ~100 ms later: `stt.final{"what's on my calendar tomorrow"}`.
4. **Fast path miss (+20 ms).** The deterministic matcher handles "turn off the kitchen lights" and "set a timer for ten minutes" offline; this is not one of those, so the turn goes to the agent.
5. **Policy, then LLM.** Core builds a `Principal{profile_id: sam, via: voice, confidence: 0.91, margin: 0.22}` and a cached prompt prefix (persona, core tools, Sam's module cards). Claude Haiku on Bedrock calls `find_tools("calendar")`, gets `caldav.events` back, and calls it. Policy middleware checks: `personal` tier, score above `τ_strict`, margin above `m`, Sam has a grant. Allowed, logged.
6. **Audible feedback (<1.0 s).** Because a tool is running, a short acknowledgement earcon plays before the answer is ready.
7. **Answer (<2.0 s).** The first sentence streams to TTS while the rest of the reply is still arriving: *"Two things tomorrow: dentist at nine, and dinner with Priya at seven."* The LED fades. The 90-second conversation window opens, so "Hey beepboop, move the dentist to ten" continues the same conversation.

If Sam had said *"unlock the front door"*, step 5 would have stopped at `critical`: beepboop says "approve on your phone", a pending approval appears in the web UI, and the unlock only runs after Sam taps **Approve** within 60 seconds. If the speaker had scored below threshold, the turn would have run as **Guest**, which by default can only ask about the house, the weather and timers, and never touches anyone's calendar.

The full lifecycle and event schemas are in [docs/VOICE_PIPELINE.md](docs/VOICE_PIPELINE.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the latency contract those timings come from is [ADR 0015](docs/adr/0015-latency-contract-and-fast-path.md).

---

## Hardware

| Part | Requirement | Notes |
|---|---|---|
| Raspberry Pi 4 | **8 GB recommended, 4 GB minimum** | 2 GB will not fit Home Assistant plus local speech. 32-bit OS is unsupported. |
| Storage | **USB 3 SSD, required** | HA's recorder and beepboop's traces wear out SD cards. |
| Cooling | **Active fan or fan case, required** | Sustained STT + speaker ID + HA throttles a passive Pi. |
| Microphone / speaker | **ReSpeaker XVF3800 USB 4-mic array** | Onboard AEC and beamforming. All playback goes out through the array so echo cancellation has a reference signal. |
| Network | Ethernet or Wi-Fi | Internet is needed for the LLM; the fast path and timers work offline. |
| OS | Raspberry Pi OS Lite 64-bit + Docker Compose v2 | See [docs/OPERATIONS.md](docs/OPERATIONS.md). |

Every Pi 4 number in this spec is a **target to be validated in M0** against the M0 gates (G1–G7, [docs/ROADMAP.md](docs/ROADMAP.md)).

---

## Architecture

Four containers; two of them are ours.

```
┌─────────────────────────────── Raspberry Pi 4 ────────────────────────────────┐
│                                                                                 │
│  ┌──────────┐  :443  ┌──────────────────────────────────────────────────────┐   │
│  │  caddy   │──────▶ │  beepboop-core        (python: beepboop)             │   │
│  │ local CA │        │  ┌─────────┐ ┌────────┐ ┌──────────┐ ┌───────────┐   │   │
│  └──────────┘        │  │ agent   │ │ policy │ │ registry │ │ mcp (/mcp)│   │   │
│   /auth/external/    │  │  loop   │ │ tiers  │ │ modules  │ │ external  │   │   │
│   callback ──▶ HA    │  │ fastpath│ │ grants │ │ resolver │ │ clients   │   │   │
│                      │  └────┬────┘ └───┬────┘ └────┬─────┘ └───────────┘   │   │
│                      │  ┌────┴──────────┴───────────┴─────┐ ┌────────────┐  │   │
│                      │  │ providers/llm (Bedrock, OpenAI-c)│ │ api + ui   │  │   │
│                      │  └──────────────────────────────────┘ │ (SvelteKit │  │   │
│                      │  ┌──────────────────────────────────┐ │  static)   │  │   │
│                      │  │ db: one SQLite file (WAL)         │ └────────────┘  │   │
│                      │  └──────────────────────────────────┘                 │   │
│                      └───────────▲──────────────────────────┬────────────────┘   │
│          unix socket, JSON-lines │ TEXT EVENTS ONLY         │ one WebSocket      │
│   the event protocol in ARCHITECTURE §4.2; never PCM       │ policy-wrapped     │
│                      ┌───────────┴──────────────────────┐   │                    │
│                      │  beepboop-audio (python:          │   ▼                    │
│                      │  beepboop_audio) owns ALL PCM     │ ┌──────────────────┐   │
│                      │  wake(microWakeWord) · VAD · speaker│ │  homeassistant   │   │
│                      │  ID (CAM++) · STT (zipformer) ·   │ │  pinned digest   │   │
│                      │  TTS (Piper via sherpa-onnx) ·    │ │  127.0.0.1:8123  │   │
│                      │  earcons · PIN capture            │ │  host network    │   │
│                      └───────────┬──────────────────────┘ └──────────────────┘   │
│                            /dev/snd (ALSA)                                       │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 ▼
                     ReSpeaker XVF3800 (mic array + speaker out, AEC)

External MCP clients ──HTTPS (Tier 1 only)──▶ caddy ──▶ core /mcp
Phone / laptop ───────HTTPS (local CA)───────▶ caddy ──▶ core web UI
```

Rules that hold the shape together:

- **Audio never leaves the audio service.** Only text events cross to core.
- **Policy is middleware, not prompt.** Every tool call, from voice or from `/mcp`, passes `policy.authorize(principal, tool, args)` and is audited.
- **One registry, two front doors.** The voice agent dispatches in-process; external clients speak Streamable HTTP at `/mcp`.
- **HA first.** Native modules exist only where HA cannot model per-person accounts.
- **Modules never import MCP, HA or LLM SDKs.** They get a `ToolContext`.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), decisions: [docs/adr/README.md](docs/adr/README.md).

---

## Features by milestone

- **M0** proves the Pi: `beepboop bench` on real hardware against gates G1–G7, before any feature code.
- **v0.1** is one house that knows your voice: wake word, speaker ID, local speech, Bedrock agent with an offline fast path, modules `ha`, `timers`, `lists`, `weather`, `caldav`, `music`, `persona`, risk tiers with web-UI confirm, and the web UI for people, grants, approvals and activity.
- **v0.2** connects your accounts and devices (OAuth modules, external `/mcp`, phone confirm, cloud speech, budgets); **v0.3+** adds memory, routines, barge-in, satellites and a third-party MCP proxy.

Scope, acceptance criteria and the parking lot of every predicted feature live in [docs/ROADMAP.md](docs/ROADMAP.md).

---

## Documentation map

| Document | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Containers, event protocol, provider seams, turn lifecycle |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | Every identifier used across the docs, defined once |
| [docs/VOICE_PIPELINE.md](docs/VOICE_PIPELINE.md) | Wake word, speaker ID, enrollment, STT/TTS, latency budget |
| [docs/IDENTITY_AND_TRUST.md](docs/IDENTITY_AND_TRUST.md) | Principal, risk tiers, grants, roles, second factor, threat model |
| [docs/MODULES.md](docs/MODULES.md) | The module contract, `ToolContext`, evals, dev kit |
| [docs/module-template/](docs/module-template/README.md) | Copy-me skeleton: `manifest.yaml`, `module.py`, `AGENT.md`, `SETUP.md`, `evals.yaml` |
| [docs/MCP.md](docs/MCP.md) | Tool registry, core tools, `find_tools`/`read_guide`, external `/mcp` door |
| [docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md) | HA container, WebSocket client, resolver, UUID grants, onboarding |
| [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md) | `LLMProvider` protocol, Bedrock default, OpenAI-compatible, conformance suite |
| [docs/WEB_UI.md](docs/WEB_UI.md) | Principles, information architecture, wireframes, onboarding, HTTPS tiers |
| [docs/DATA_AND_PRIVACY.md](docs/DATA_AND_PRIVACY.md) | SQLite schema, encryption, retention, export and delete |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Install, compose layout, updates, backups, observability |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestones with acceptance criteria |
| [docs/adr/README.md](docs/adr/README.md) | Architecture decision records (18 so far) |
| [CLAUDE.md](CLAUDE.md) | Build conventions and hard rules for Claude Code sessions and humans |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute code, modules, docs and ADRs |
| [SECURITY.md](SECURITY.md) | Reporting vulnerabilities, threat-model pointer, support policy |

---

## Quickstart (planned — not yet runnable)

This is the intended install path for v0.1. Nothing below exists yet; it is here so the docs and the code converge on it.

```bash
# On a Pi 4 running Raspberry Pi OS Lite 64-bit with Docker Compose v2
git clone https://github.com/JaminB/beepboop.git && cd beepboop
less deploy/install.sh && sh deploy/install.sh   # the only installer; prints every step first
# install.sh writes /srv/beepboop/deploy/{compose.yaml,.env} (set BB_HOST; default beepboop.local)
# and starts caddy, beepboop-core, beepboop-audio, homeassistant (+ the optional `music` profile)

# beepboop speaks and blinks a 6-digit claim code.
# Open https://beepboop.local, install the local CA when prompted,
# enter the claim code, create the admin account, enrol your voice.
```

Developer loop (planned):

```bash
uv sync                         # core + audio + tools, pinned lockfile
uv run beepboop chat --as sam   # text chat through the real policy path, no mic
uv run beepboop module new my_module
uv run beepboop module check modules/my_module
uv run beepboop bench all --gates   # on the Pi: latency waterfall + the M0 gates (G1–G7, ROADMAP)
```

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for the full install and [CLAUDE.md](CLAUDE.md) for the developer commands.

---

## Privacy promises

These are design constraints, not aspirations. Each is traceable to a doc and will be covered by tests.

1. **No audio is stored. No exceptions.** Audio exists only in the audio service's memory for the duration of a turn; enrollment keeps embeddings, not recordings, so a speaker-model upgrade asks you to re-enroll (about two minutes) rather than keeping your voice on disk ([ADR 0018](docs/adr/0018-no-audio-retention.md)).
2. **Audio never leaves the Pi.** The LLM receives text transcripts and tool results, never sound. The web UI shows what left the house and to whom.
3. **Voiceprints stay home.** Speaker embeddings are encrypted at rest with a key derived from the Pi's one-time-programmable device key, never sent to a cloud, and never exposed as an MCP tool or resource.
4. **Enrollment is consent.** You cannot be enrolled without saying yes. Guests are never enrolled; no voiceprint is kept, and their turns are kept 7 days as household activity.
5. **Transcripts are yours.** Kept 30 days, viewable and deletable per user. Admins see children's and guests' history and what adults did (actions, not transcripts).
6. **Spoken PINs never reach the model.** They are captured by the audio service, hashed, and appear in no transcript or log.
7. **Delete means delete.** "Delete me" cascades through profile, voiceprint, transcripts, tokens, secrets, lists, timers and memory. Export produces one JSON archive.
8. **Nothing phones home.** No telemetry. Updates are notify-only; you click to apply.

Full detail: [docs/DATA_AND_PRIVACY.md](docs/DATA_AND_PRIVACY.md).

---

## License

Copyright (C) 2026 beepboop contributors.

beepboop is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version (SPDX `GPL-3.0-or-later`). It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full text and [ADR 0020](docs/adr/0020-gpl-3-and-espeak-phonemiser.md) for why the project moved from MIT. Speech models are downloaded at install time and carry their own licences, recorded in `models/LICENSES.md`; a module that bundles data records its licences in `modules/<id>/LICENSES.md`. Attributions required by CC BY data are in [NOTICE](NOTICE).

beepboop is an independent project. "Home Assistant" is a trademark of the Open Home Foundation; beepboop works with Home Assistant and is not affiliated with it.

## Acknowledgements

beepboop stands on a lot of excellent work:

- **[Home Assistant](https://www.home-assistant.io/)** and the Open Home Foundation: the integration backbone, the Wyoming protocol and the Voice PE work that showed local voice is viable.
- **[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)** (k2-fsa): one runtime for VAD, speaker embedding, streaming STT and TTS on ARM.
- **[microWakeWord](https://github.com/kahrendt/microWakeWord)** (Kevin Ahrendt): tiny, accurate, Apache-licensed wake words.
- **[Model Context Protocol](https://modelcontextprotocol.io/)**: the neutral standard that let us skip inventing a skills API.
- **[Piper](https://github.com/OHF-Voice/piper1-gpl)** voices, **[Silero VAD](https://github.com/snakers4/silero-vad)**, **[WeSpeaker](https://github.com/wenet-e2e/wespeaker)** CAM++, **[Caddy](https://caddyserver.com/)**, **[SvelteKit](https://svelte.dev/)** and **[Music Assistant](https://www.music-assistant.io/)**.

And to Mycroft, Rhasspy and OpenVoiceOS, whose history taught us what to keep small.
