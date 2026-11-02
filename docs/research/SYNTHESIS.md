# beepboop architecture synthesis (lead architect, 2026-11-01)

The seven research reports disagree on eight points. Each disagreement is settled where it comes up and listed together in §5.0. Every Pi 4 number is still unmeasured, so milestone **M0 ("prove the Pi")** must pass before v0.1 work starts.

---

## 1. Component stack (one pick per slot)

| Slot | Pick | Version | Why, and how it settles the report disagreements |
|---|---|---|---|
| Hardware | Pi 4 **8 GB recommended, 4 GB minimum**, booting from a USB 3 SSD, with an active fan or heatsink case | — | HA plus STT, TTS and speaker ID will not fit in 2 GB. One report said 4 GB minimum and another said plan for 8 GB, so the spec states both. HA's recorder wears out SD cards. |
| OS | Raspberry Pi OS Lite 64-bit (current Debian base), Docker Engine plus Compose v2 | — | Treat 32-bit as unsupported. |
| Mic / speaker | ReSpeaker XVF3800 USB running the **16 kHz USB firmware**. Use the processed ASR beam for both enrollment and identification. **All playback goes out through the array's USB output**, because that is the AEC reference. | Pinned firmware image | AEC and barge-in only work if TTS and chimes leave through the array. |
| Audio I/O | Raw ALSA `/dev/snd` in the audio container. No PipeWire in that path. | — | Avoids device-busy conflicts. |
| Wake word | **microWakeWord** via `pymicro-wakeword` **2.5.0** (aarch64 wheels). Ship a custom "beepboop" model built by `tools/train_wakeword/` (Piper synthetic clips plus negatives, fine-tuned SEED-style on household clips). **openWakeWord** is an optional plugin. | 2.5.0 | The STT report preferred openWakeWord. It loses: its pretrained models are CC BY-NC-SA, it has had no release since 2024, and HA/OHF has standardized on microWakeWord. |
| VAD / endpointing | Silero VAD (ONNX) through sherpa-onnx | sherpa-onnx **2.0.1** | One runtime for VAD, speaker ID, STT and TTS. |
| Speaker ID | **CAM++ (WeSpeaker VoxCeleb, 192-d)** via sherpa-onnx, fp32 first. int8 only after a household A/B test. | — | Best accuracy per FLOP: 1.72 GFLOPs and 0.73% EER. It is the only model with headroom against the 200 ms target. |
| STT (local) | **sherpa-onnx streaming zipformer transducer** (English, small) with contextual biasing (hotwords) | 2.0.1 | True streaming means only a ~100 ms tail after end-of-speech. Apache-2.0. Moonshine streaming becomes a plugin after a license check. whisper.cpp is the offline fallback plugin. |
| TTS (local) | **Piper-format VITS voice (low/medium) run by sherpa-onnx**, synthesized sentence by sentence | 2.0.1 | Avoids linking GPL piper1. Each voice's license is recorded in `models/LICENSES.md`. Kokoro is opt-in. |
| Cloud speech (plugins) | Deepgram Nova-3 (STT), Amazon Polly (TTS); Transcribe later | — | Off by default, opt-in per household, and the UI shows who hears your audio. |
| LLM | Own `LLMProvider` protocol. Default adapter: **Bedrock ConverseStream (boto3)** with `cachePoint`, model **Claude Haiku 5.5** (`global.anthropic.claude-haiku-5-5`). Sonnet 5.5 as an opt-in or escalation. Second adapter: OpenAI-compatible (Ollama, llama.cpp, vLLM, OpenRouter). The `bedrock-mantle` Messages adapter comes in v0.2. | boto3 current, pinned | The MCP report wanted InvokeModel for Anthropic server-side tool search. Not needed: beepboop has ≤15 core tools plus its own portable `find_tools`, so Converse is enough. No LiteLLM (dependency weight, unverified supply-chain incident). |
| Local LLM on the Pi | **None.** The deterministic fast path does that job. | — | Prefill at Pi 4 speeds makes it impossible within 1.5 s. |
| Integration backbone | **Home Assistant Core container**, tag pinned by digest (start at the latest vetted `2026.10.x`), talked to over **one persistent WebSocket** from the `ha` module | 2026.10.x | **HA's MCP server is not used.** It has no per-user scoping. |
| MCP | **Official `mcp` Python SDK v2** (`MCPServer`), protocol **2026-07-28**, Streamable HTTP at `/mcp` | **2.3.x** pinned | Overrides the MCP report's FastMCP 4 pick. Our own registry already does policy, namespacing and search, so FastMCP's extras duplicate it and add churn risk. The SDK is used only at the external front door. |
| Core service | Python **3.12**, FastAPI, uvicorn, pydantic 2, structlog, `uv` lockfile | — | — |
| Database | **SQLite (WAL), single writer task, one file**. `synchronous=FULL` for auth and ACL, `NORMAL` for traces. sqlite-vec for memory in v0.3. | — | No Postgres. |
| Secrets | Envelope encryption (libsodium secretbox / AES-GCM). KEK = HKDF(Pi OTP device key), falling back to a passphrase-wrapped KEK | — | Protects against theft of the SD card or SSD, not theft of the whole Pi. Documented as such. |
| Web UI | **SvelteKit 3.1 + Svelte 5, `adapter-static` 4.0 SPA** served by FastAPI. Bits UI headless primitives plus CSS tokens. TS client generated from OpenAPI. | 3.1.0 / 4.0.0 | No Node in production. One API serves the UI, the CLI and tests. |
| TLS / proxy | **Caddy 2** with a local CA by default, ACME DNS-01 when a real domain exists, ARI renewal | — | Let's Encrypt lifetimes drop to 64 days in Feb 2027, so renewal must not be hard-coded. |
| Web auth | Password (argon2id) plus optional TOTP (`pyotp`). Passkeys (`webauthn` 2.7.x) only on a real domain. `__Host-` session cookies. | — | `.local` cannot use passkeys. |
| Updates | Notify-only (Diun-style). One-click update that snapshots, pulls by digest, health-checks and rolls back automatically. **Never silent.** | — | Watchtower is archived. Silent updates are unacceptable on a device that controls locks. |

---

## 2. Architecture

### 2.1 Processes and containers

There are 4 containers and only 2 of them are ours.

```
┌───────────────────────── Pi 4 (host network unless noted) ─────────────────────────┐
│ caddy        :443 → core:8080 ; /auth/external/callback → HA (only that path)       │
│ beepboop-core  (FastAPI · agent loop · policy · registry · modules · MCP · UI · DB)  │
│      ▲  unix socket: event stream (JSON-lines, Wyoming-style framing), TEXT ONLY    │
│ beepboop-audio (ALSA · wake · VAD · speaker-ID · STT · TTS · earcons · AEC playback)│
│      cpuset 2-3, nice -5, mem_limit 768M, /dev/snd                                   │
│ homeassistant  pinned digest; http bound to 127.0.0.1 (+ nftables drop :8123 LAN)   │
│ optional compose profiles: mosquitto, zwave-js-ui, matter-server, music-assistant,  │
│                            wyoming-* (external STT/TTS servers)                      │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

**Rules:**
- **Audio never crosses a process boundary.** The audio service owns PCM end to end. Between audio and core only events flow: `wake`, `speaker.provisional`, `speaker.final`, `stt.partial`, `stt.final`, `speak{text, voice, turn_id}`, `cancel`, `capture_pin`. The latency-critical path therefore has no audio hops.
- **Wyoming stays at the edges.** A Wyoming client adapter consumes external STT/TTS servers. A Wyoming satellite server lets ESP32 or second-Pi satellites stream audio to the audio service later. Wyoming is not the internal bus.
- **HA networking.** HA needs host networking for discovery. Its HTTP is bound to loopback, and nftables drops LAN traffic to :8123 as defense in depth. Admins reach HA through a Caddy route gated by a beepboop admin session. (`server_host` behavior varies across HA versions, so M0 must verify the binding; nftables is the backstop.)

### 2.2 Core interfaces (provider seams)

All providers are async, streaming, cancellable and registered by entry point. Every provider declares `id`, `capabilities` and `license`.

```python
class WakeProvider(Protocol):
    def models(self) -> list[str]: ...
    def process(self, frame: PCM16) -> WakeHit | None: ...        # 10–30 ms frames, sync, pinned core

class SpeakerEmbedder(Protocol):
    model_id: str; model_sha: str; dim: int
    def embed(self, audio: PCM16) -> np.ndarray: ...              # L2-normalised

class SpeakerIdentifier:  # core logic, not a plugin
    def identify(self, emb, candidates) -> Identity  # {profile_id|GUEST, score, margin, stage: provisional|final}

class SttProvider(Protocol):
    async def stream(self, audio: AsyncIterator[PCM16], ctx: SttContext) -> AsyncIterator[Partial | Final]
    # SttContext.hotwords = profile vocab (contacts, device/area names)

class TtsProvider(Protocol):
    async def synth(self, text: str, voice: VoiceRef) -> AsyncIterator[PCM16]

class LLMProvider(Protocol):
    id: str; caps: Caps   # tools, cache, streaming_tool_args, max_ctx
    async def stream(self, req: Request) -> AsyncIterator[TextDelta | ToolCallStart | ToolCallArgsDelta | ToolCallEnd | Usage | Done]
```

`Request` contains `system` (stable blocks with cache hints, then a volatile block), `messages`, `tools` (JSON Schema, sorted deterministically), `max_tokens`, `effort`, and `metadata.profile_id`. The core loop owns tool execution, sentence chunking into TTS, retries, the fallback ladder and barge-in cancellation. Providers only translate. Every adapter must pass a conformance suite covering text streaming, tool-argument streaming, parallel tools, mid-stream cancellation and usage reporting.

### 2.3 Module contract

```
modules/<id>/
  manifest.yaml     # the contract (linted in CI)
  module.py         # entrypoint: class Module(beepboop.Module)
  AGENT.md          # mandatory model-facing guide (served on demand)
  SETUP.md          # human setup (OAuth app creation etc.)
  evals.yaml        # utterance → expected tool + args (+ profile, + expected denial)
  tests/
```

```yaml
id: spotify
api: 1
version: 0.1.0
summary: "Play, pause, queue music on the user's own Spotify."   # → module card (≤20 words)
kind: native            # native | ha | mcp_proxy
scope: profile          # household | profile  (who owns the connection)
category: media
settings:  {json-schema}          # → auto-rendered UI form, per scope
secrets:   [client_id, client_secret, oauth_token]
oauth:     {provider: spotify, scopes: [...], flow: bounce}   # optional
events:    {emits: [media.started], consumes: [speaker.final]}
tools:
  - name: spotify.play
    description: "Play a song, artist, album or playlist on the user's Spotify..."  # ≤80 words, linted
    risk: act            # read | act | personal | critical
    min_confidence: personal   # optional override of the tier default
    untrusted_output: false    # true for email/web/calendar text → taints turn
    deferred: true             # false = always loaded (core only)
    args:   {json-schema}
    output: {json-schema}      # structuredContent
    examples: [{args: {...}}]
```

**Contract rules:**
- Modules never import MCP, HA or LLM SDKs directly. They receive a `ToolContext` with `principal`, `grants`, `settings`, `secrets` (decrypted lazily, scoped to that module and profile), `ha` (the **policy-wrapped** client), `http`, `kv`, `emit`, `llm` (for modules that need completions; sampling is deprecated) and `log`.
- `beepboop module new` scaffolds all files.
- `beepboop module check` runs the manifest lint, the description token lint and evals in dry-run mode, where tools log intended side effects instead of executing them.

### 2.4 Trust and ACL model

**Principal** (attached to every call, whichever door it came in through):
`{profile_id, via: voice|web|oauth|pat, confidence, margin, device, scopes, tainted: bool, turn_id}`

**Risk tiers.** Enforcement is deterministic and lives in the policy middleware, never in the prompt.

| Tier | Examples | Voice requirement | External-client requirement |
|---|---|---|---|
| `read` | home state, weather, time | any (Guest allowed if granted) | `home.read` |
| `act` | lights, media, timers | identified **or** Guest grant | `home.control` |
| `personal` | my calendar, my email, memory, my Spotify | `score ≥ τ_strict` and `margin ≥ m`. Otherwise "Is this Alex?" or a denial | `personal` |
| `critical` | unlock, disarm, purchase, garage open, messaging new recipients | **always a second factor** | `critical` plus URL-mode elicitation confirm |

Two rules sit on top of the tiers:
- **Taint rule.** Once a tool with `untrusted_output` has run in a turn, every non-`read` call for the rest of that turn needs confirmation.
- **Ceiling.** Each profile has a `risk_ceiling`. Kid and Guest presets cap at `act`.

**Grants** (one mechanism for everything):
`grant(subject: profile|group, resource: tool_glob | ha_entity_uuid | ha_area_id | ha_config_entry_id, max_risk, schedule?)`

- Effective permissions = the union of the profile's grants and its groups' grants (`household`, `adults`, `kids`), capped by the ceiling.
- **HA resources are keyed by entity-registry UUID**, not `entity_id`, so renames don't break grants.
- New entities are **default-deny** and land in a UI inbox ("3 new devices: share with household?", one tap).
- Personal HA accounts work like this: config entry → `profile_id`, and its derived entities are auto-granted to that profile.
- **Indirect HA services** (`script.*`, `scene.*`, `automation.trigger`, `homeassistant.*`, `rest_command`, `shell_command`, `notify.*`) are denied unless the admin explicitly grants them as entities with a declared risk, defaulting to `critical`.
- Presets (Admin, Adult, Kid, Guest, Babysitter) are **just grant templates plus a ceiling plus schedules**. There is no special-case code.

### 2.5 MCP surface

There is **one registry with two front doors**. The voice agent dispatches in-process with no MCP serialization. External clients use `https://<host>/mcp`. Both go through the same `policy.authorize(principal, tool, args)` then `audit` path.

**Always-loaded core tools (12):**
- `home.turn_on`, `home.turn_off`, `home.set`, `home.state`. These take names or areas, which a **server-side resolver** fuzzy-matches against the profile's granted entity index.
- `media.control`
- `timer.set`, `timer.list`, `timer.cancel`
- `list.add`, `list.read`
- `find_tools(query)`: BM25 over names, descriptions and AGENT.md keywords. Returns full definitions, which the loop injects on the next step.
- `read_guide(module)`

Planned additions: `memory.*` in v0.3, plus `whoami`.

**Other surfaces:**
- **Resources:** `home://context` (a profile-scoped, compact state snapshot) and `beepboop://modules/{id}/guide`.
- **Prompts:** user-invoked routines only, from v0.3.
- **External clients:** `tools/list` is computed per token (spec-legal) with `cacheScope: private` and deterministic order. beepboop is its own OAuth 2.1 authorization server. The web login is the consent page. It implements RFC 9728, RFC 8707 and RFC 9207, PKCE, Client ID Metadata Documents, and DCR as a fallback. Personal access tokens are available for simple clients. There is no token passthrough.
- **Second factor over MCP:** MRTR `input_required`, then URL elicitation to `https://<host>/confirm/<id>`. That page requires a web session whose `sub` matches the caller. The `requestState` is AEAD-sealed over `{sub, tool, args_digest, exp ≤ 60 s, nonce}` and the nonce is single-use.

**Model-visible docs use three tiers:**
1. Tool description, at most ~80 words.
2. A per-profile module card list in the system prompt, at most ~1.5k tokens total.
3. Full AGENT.md, fetched only on demand.

**Prompt layout for caching:** persona/voice contract → core tools → module cards → **`cachePoint`** → volatile block (time, speaker name, short context).

### 2.6 Data model (SQLite)

```
profiles(id, name, kind[admin|adult|kid|guest], risk_ceiling, locale, tts_voice, speech_rate,
         pin_hash, phone_notify_target, created_at)
logins(profile_id, username, pw_hash, totp_secret_enc)      passkeys(profile_id, cred_id, pubkey, sign_count)
sessions(id_hash, profile_id, created, last_seen, ua)       api_tokens(id, profile_id, kind[pat|oauth], hash, scopes, client_id, exp)
groups(id, name)  group_members(group_id, profile_id)
grants(id, subject_type, subject_id, resource_type, resource_id, max_risk, schedule_json)
voiceprints(profile_id, model_id, model_sha, centroids_enc[k×192], n_clips, calib_json{τ, margin}, updated_at)
voice_samples(profile_id, kind[enroll|adapt], emb_enc, audio_enc?, consent, created_at)   -- adapt buffer ≤50
integrations(id, module_id, scope, profile_id?, settings_json, ha_config_entry_id?, status[ok|reauth|error])
secrets(id, owner_module, profile_id?, ciphertext, nonce, kek_version)
turns(id, profile_id, confidence, margin, device, transcript, reply, stages_ms_json, tokens, cost, tainted, created_at)
tool_calls(turn_id, tool, args_redacted, risk, decision[allow|deny|confirm], factor[pin|phone|elicit], result_status)
approvals(nonce, profile_id, tool, args_digest, exp, consumed_at)
timers(id, profile_id, device, fires_at, label, state)      lists/items (or HA todo entities)
conversation(profile_id, messages_json, expires_at)         -- 90 s follow-up window
memory(id, profile_id, text, embedding?, source_turn, created_at, expires_at)   -- v0.3, never cross-profile
kv(module_id, profile_id?, key, value)
```

**Retention defaults:**
- Audio: none.
- Transcripts and turns: 30 days, editable per user.
- "Delete me" cascades through every table above.
- Embeddings never leave the Pi and are never exposed as MCP tools or resources.

### 2.7 Speaker-ID decision logic

1. **Provisional ID** comes from the ~1 s wake-word pre-roll (text-dependent).
2. **Final ID** comes from wake word plus the first ≤2 s of the command, with score fusion. It is ready at or before end-of-speech.
3. **Scoring:** cosine similarity against k centroids per user, with AS-norm against the household plus a ~200-embedding cohort.
4. **Decision:** accept iff `top1 ≥ τ_tier` and `top1 − top2 ≥ margin`. Otherwise the Guest profile.
5. **Prior:** HA `person` presence narrows the candidate set (v0.2).
6. **Adaptation:** only from high-margin, uncorrected turns. Centroids are recomputed nightly. Enrollment clips are the anchor set.
7. **"That wasn't me"** adds a negative example and an audit entry.

---

## 3. Latency budget (end-of-speech → first audible reply)

All numbers are targets that M0 must confirm under **concurrent** load, with HA running.

| Stage | Fast path (local intent) | LLM, no tool | LLM + 1 tool |
|---|---|---|---|
| Endpoint (VAD hangover) | 250 | 250 | 250 |
| STT final tail (streaming) | 100 | 100 | 100 |
| Speaker ID final | 0 (parallel, done earlier) | 0 | 0 |
| Fast-path match + ACL resolve | 20 | 20 (miss) | 20 |
| Context assembly | — | 10 | 10 |
| LLM TTFT to first sentence (Haiku 5.5, cached prefix, low effort, warm keep-alive TLS) | — | 650 | 550 to tool call |
| Tool execution (HA WebSocket call_service) | 40 | — | 40 |
| Earcon or pre-recorded ack | — | — | **plays at ~0.95 s** |
| Second LLM call → first sentence | — | — | 500 |
| TTS first chunk | 0 (cached "Done" clip) | 200 | 200 |
| Output buffer and DAC | 50 | 50 | 50 |
| **Total** | **≈0.46 s** | **≈1.28 s** ✅ | **≈1.7 s for speech, earcon at <1 s** ⚠️ |

**Latency rules:**
- Pre-warm the Bedrock connection on wake word.
- Speak the first sentence while the rest of the reply streams.
- Speculative LLM starts on partial transcripts are off in v0.1.
- When the LLM fails, the voice says so within 1 s and the fast path keeps working offline.
- Tool turns may exceed 1.5 s for speech, but **some audible feedback must arrive in under 1 s**. The user should be told about this nuance (see risk 2).

---

## 4. Roadmap

**M0: Prove the Pi (2–3 weeks, gate for everything)**
- Build `beepboop bench`: a concurrent pipeline benchmark with HA running.
- Go/no-go gates:
  - Speaker ID p95 under 200 ms.
  - Streaming STT RTF under 0.6 at a 2-thread cap.
  - Haiku 5.5 TTFT p50 under 700 ms from a home connection.
  - RAM under 3.2 GB on a 4 GB Pi.
  - Wake word: ≤0.5 false accepts per 24 h on a living-room soak corpus.
- Verify the HA loopback bind, the onboarding API and long-lived token creation.
- **If a gate fails:** cloud STT becomes the default, or 8 GB becomes the minimum.

**v0.1: "One house that knows your voice"**
- Audio service: wake ("beepboop" model), VAD, speaker ID, local STT/TTS, earcons, LED/chime states, hardware mute respected.
- Core: agent loop, Bedrock ConverseStream with Haiku 5.5, fast path, voice prompt contract, 90 s follow-up.
- `ha` module: headless onboarding, WebSocket state cache, resolver, UUID grants, new-device inbox, indirect-service denylist.
- Modules: `timers`, `lists`, `weather`, `ha`.
- Risk tiers with **spoken PIN** as the second factor. The PIN is captured by the audio service and never enters the LLM or the transcript.
- Web UI:
  - First-run claim code spoken and blinked by the device
  - Admin plus member logins (password + TOTP)
  - People / Voice enrollment (quality gates, similarity matrix)
  - Grants; Bedrock credentials
  - Activity ("why did you do that", "that wasn't me")
  - Device health; text chat "test as user X"
- Backups (`VACUUM INTO` + age), module SDK plus `module new`/`check`, `models/LICENSES.md`.

**v0.2: "Your accounts, your devices"**
- Generic HA data-entry-flow renderer and per-profile config entries.
- OAuth bounce page (`oauth.beepboop.dev`) for native modules. Native Spotify and Google modules use bring-your-own credentials.
- Phone confirm through HA Companion actionable notifications.
- External MCP endpoint (OAuth 2.1, PATs, URL elicitation).
- `find_tools` deferral; evals in CI against Bedrock.
- OpenAI-compatible provider and mantle adapter.
- Cloud STT/TTS plugins; presence prior; voiceprint adaptation.
- Tier-1 HTTPS (own domain or Tailscale) with passkeys.
- One-click update with rollback; ducking hook; cost budgets.

**v0.3+**
- Per-user memory (sqlite-vec, view/edit/forget).
- Routines and proactive announcements (HA event bus plus `subscriptions/listen`), with quiet hours.
- Barge-in (AEC-verified) and full kids/parental controls (schedules, content filter, purchase block, re-enroll prompts).
- Wyoming satellites with nearest-device arbitration and intercom.
- Per-user proxy for third-party MCP servers (description hashing, default `critical`).
- Liveness plugin (AASIST-L), multilingual profiles, per-user wake aliases.
- Optional `beepboop_bridge` HA component so HA's Assist respects beepboop ACLs.

---

## 5. Top 12 risks, contradictions and hard problems

### 5.0 Where the reports disagreed, and the ruling

| Topic | Disagreement | Ruling |
|---|---|---|
| Wake engine | openWakeWord vs microWakeWord | microWakeWord |
| MCP framework | FastMCP 4 vs official SDK | Official SDK |
| Bedrock API | Converse vs InvokeModel (for tool search) | Converse plus our own `find_tools` |
| Wyoming | Internal bus vs edge | Edge only |
| HA network | Host vs internal-only | Host plus loopback bind plus nftables |
| RAM | 4 GB vs 8 GB | 4 minimum, 8 recommended |
| UI server | SvelteKit node vs static | Static |
| Default auth | Passkeys vs password | Password + TOTP; passkeys only on a real domain |

### 5.1 The twelve

1. **Pi 4 contention and RAM (all latency claims are unmeasured).**
   - The problem: HA, STT, TTS, speaker ID and wake share 4 A72 cores and thermal throttling.
   - Mitigation: the M0 gates; cgroup `mem_limit`s so the OOM killer doesn't kill audio mid-utterance; cpusets; a nice'd HA with a low recorder `purge_keep_days`; zram and log2ram; a fan as a hardware requirement; and a "cloud speech" degradation profile.

2. **The 1.5 s target vs tool-using turns.**
   - The problem: two LLM round trips cannot reliably fit.
   - Mitigation: the fast path covers the most common device commands, earcons and acks arrive in under 1 s, the prefix is cached, TLS is pre-warmed, effort is low. **Spec wording:** "first audible feedback under 1.0 s; first spoken answer under 1.5 s for no-tool turns." Confirm this wording with the user.

3. **Voice ID is not authentication.**
   - Threats: replay, voice cloning, TV audio. Children's EER was about 30% in one study. Siblings sound alike.
   - A **spoken PIN in a shared room is also weak**: it can be overheard or recorded.
   - Mitigation:
     - `critical` always needs a second factor, and phone confirm is preferred once v0.2 lands.
     - Spoken PINs are rate-limited with lockout. Consider a rotating PIN or challenge digits ("say digits 2 and 4 of your PIN").
     - Kid profiles are capped at `act`.
     - The margin rule applies, and the audit log flags low-score high-risk actions.
     - Docs say plainly that voice ID is personalization.

4. **HTTPS on a LAN.**
   - The problem: `.local` cannot get public certificates, Chrome rejects `.local` RP IDs for passkeys, router DNS-rebind protection breaks the plex.direct pattern, Chrome Local Network Access blocks `fetch` from public pages, and MCP clients want HTTPS.
   - Mitigation: Tier 0 is a device-generated local CA plus password + TOTP, with an "install CA" helper page per OS. Tier 1 (own domain via DNS-01, or Tailscale) unlocks passkeys and external MCP. The canonical URL is configurable from day one.

5. **OAuth redirects for a LAN-only, HA-bound-to-loopback device.**
   - The problem: HA's flows bounce through `my.home-assistant.io` to *HA's* URL, which the browser cannot reach if HA is on loopback.
   - Mitigation:
     - Set HA `internal_url` to the beepboop host and have Caddy expose **only** `/auth/external/callback`, which HA validates with signed state.
     - Native modules use our own static bounce page with a signed, single-use, session-bound JWT `state` plus PKCE.
     - Google restricted scopes (Gmail, Calendar write) mean bring-your-own client credentials, documented in `SETUP.md`. Device-code flow is not available for Calendar or Gmail.

6. **Confused deputy around an admin HA token.**
   - The problem: anyone holding the token or reaching :8123 bypasses everything, and scenes, scripts and automations reach entities indirectly.
   - Mitigation:
     - The token is held only by the `ha` module's policy wrapper and is encrypted at rest.
     - :8123 is unreachable from the LAN.
     - A service allowlist, with indirect services defaulting to `critical`.
     - HA's MCP server stays disabled.
     - Optionally, one non-admin HA user per profile, **for audit attribution only**.

7. **Tool-count explosion, token cost and TTFT.**
   - Mitigation: 12 intent tools plus the resolver instead of tools per entity; module cards plus `find_tools`; deferred module tools; a CI lint on description length and total per-profile prompt tokens; an admin UI "token budget" meter; deterministic tool ordering for cache hits.

8. **Prompt injection through tool output** (email, calendar, web, third-party MCP).
   - Mitigation: taint-the-turn; outbound messaging treated as `critical`; results wrapped in labeled untrusted blocks; model output can never change the principal; third-party tool descriptions hashed, with alerts on change.

9. **Upstream churn.**
   - The problem: HA ships monthly breaking changes (2026.10 removed integrations), and MCP has had three breaking revisions in about 18 months.
   - Mitigation: pin by digest; a bridge contract-test suite; staged upgrades with snapshot and rollback; MCP isolated behind the module API with the SDK pinned.

10. **Licensing hygiene in an MIT repo.**
    - Issues: piper1 is GPL-3, openWakeWord models are NC, Moonshine and Parakeet licenses are unverified, Piper voices have per-voice licenses, CC BY weights need attribution, HA is a trademark.
    - Mitigation: sherpa-onnx as the runtime; models downloaded at install rather than vendored where licenses are unclear; `models/LICENSES.md` checked in CI; a NOTICE file; "works with Home Assistant" wording.

11. **Biometric privacy and secrets.**
    - The problem: voiceprints fall under GDPR Art. 9 and BIPA, and an SD card can be stolen.
    - Mitigation: explicit consent at enrollment; embeddings and audio encrypted with the OTP-derived KEK; never sent to the cloud or exposed over MCP; real cascading delete and export; Guest stores nothing; a privacy ledger in the UI; Bedrock receives transcripts only, and the UI says so.

12. **The design forks between HA and native adapters, and HA config entries have no owner.**
    - The problem: two integration models could double the work.
    - Rule: **HA first.** A native module is allowed only when HA cannot represent per-person accounts cleanly. Both kinds expose the identical manifest and tool contract. Ownership is beepboop's mapping `integration → profile`. Spotify's quirks (Premium dev-app requirement, user caps, one stream per account) are documented in its AGENT.md and SETUP.md.

**Also tracked:**
- XVF3800 AEC reference routing is undocumented, which blocks barge-in until verified.
- SD wear (SSD recommended; write traces in batches).
- False wakes from TV (opt-in review queue of false-wake clips).

---

## 6. Features the user didn't ask for (ranked by value to cost)

1. **Per-user timers, alarms and reminders** that persist across restarts. They are the most-used voice feature everywhere.
2. **Deterministic local fast path plus offline degradation.** This carries the latency target and resilience.
3. **"Why did you do that?" activity timeline.** It shows transcript, ID score, tools offered and called, risk decision and second factor, with a one-tap "that wasn't me" that feeds speaker-ID training. It doubles as the module debugger.
4. **Text chat / CLI "test as user X".** It uses the same policy path and makes development a joy.
5. **Shared and personal lists** (shopping, to-do).
6. **Per-turn latency waterfall plus `beepboop bench`**, shown in the UI and run in CI on a real Pi.
7. **Module dev kit:** `module new`/`check`, dry-run, `evals.yaml` utterance-to-tool tests in CI, provider conformance suite.
8. **Privacy affordances:** mute state, LED and chime, "forget the last thing I said", audio retention off, export and delete per user.
9. **Music with per-user accounts plus ducking on wake.**
10. **Presence-aware identity prior** (HA `person` entities) and addressing ("tell Sam when the dryer's done").
11. **Per-user STT vocabulary** (contacts, rooms, device nicknames) and a per-user TTS voice and speech rate.
12. **Voice-ID trust page:** household similarity matrix and a live "sounds like you: 94%" meter.
13. **Backup/restore at onboarding plus safe one-click updates with rollback.**
14. **Cost and usage budgets** per household and per user, with a spoken warning.
15. **Babysitter / time-boxed guest profiles** (a grant template with expiry).
16. **Announcements and intercom/broadcast**, which are the foundation for satellites.
17. **Accessibility:** text replies to phone or screen, a slower speech rate.
18. **Per-user wake aliases** as an identity prior. microWakeWord runs multiple models cheaply.
19. **Multilingual per profile.**
20. **Exportable YAML ACL policy**, reviewable and diffable.

---

## 7. Open questions for the user before the documentation pass

**Q1. Latency definition.** Is "first audible feedback (earcon or ack) under 1 s, spoken answer under 1.5 s for non-tool turns" acceptable for tool-using requests?

**Q2. Second factor in v0.1.** Spoken PIN only, given that it can be overheard, or wait for phone confirm in v0.2?

**Q3. Minimum hardware.** Is "4 GB minimum, 8 GB recommended, SSD and fan required" acceptable?

**Q4. External MCP clients and local CA.** Is "external MCP requires a real domain or Tailscale (Tier 1)" acceptable? The alternative is supporting local-CA HTTPS for MCP clients, which many clients reject.

**Q5. Project bounce page.** Is a project-hosted static page (`oauth.beepboop.dev`) acceptable, or must the project run zero hosted infrastructure, which would leave only bring-your-own credentials with manual code paste?

**Q6. Contradiction to resolve.** Speaker ID was specified as "fast and local," but it is also the sole router for personal data. The design treats it as personalization only, and `personal`-tier data needs high confidence or else a prompt. Confirm that is the intent.