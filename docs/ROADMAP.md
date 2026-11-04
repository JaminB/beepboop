# Roadmap

> Status: Draft spec · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

Four milestones. **M0** proves that a Raspberry Pi 4 can meet the latency contract at all; nothing else starts until it passes or the design is changed by ADR. **v0.1** is one house that knows your voice. **v0.2** connects personal accounts and external clients. **v0.3+** adds memory, routines, barge-in, satellites and a third-party MCP proxy. Every milestone lists scope, testable acceptance criteria and what is explicitly out of scope, and the parking lot at the end maps every predicted feature to a milestone.

Related: [README.md](../README.md) · [CLAUDE.md](../CLAUDE.md) · [docs/ARCHITECTURE.md](ARCHITECTURE.md) · [docs/VOICE_PIPELINE.md](VOICE_PIPELINE.md) · [docs/IDENTITY_AND_TRUST.md](IDENTITY_AND_TRUST.md) · [docs/MODULES.md](MODULES.md) · [docs/OPERATIONS.md](OPERATIONS.md) · [ADR 0015](adr/0015-latency-contract-and-fast-path.md) · [ADR 0017](adr/0017-phased-roadmap.md)

---

## The latency contract (applies to every milestone)

The contract table (fast path, no-tool and tool turns; first audible feedback and first spoken word) is owned by [ADR 0015](adr/0015-latency-contract-and-fast-path.md) and is not restated here. How it is measured:

- **Clock zero** is the physical end of the user's speech (the last VAD-positive frame). The VAD hangover is an `endpoint` stage *inside* the budget, not before it.
- **Statistic:** p50 meets the number; p90 ≤ 1.3× the number; p95 is reported, not gated. The one exception is the 1 s audible-feedback earcon, which is a **p95 promise**: you always hear something within a second.
- **Conditions:** a Pi 4 with Home Assistant running under `beepboop bench e2e` load, both RAM variants. These are targets until M0 measures them; the measured numbers are cited from `tools/bench/results/` with dates.

The gate thresholds below (G1–G7) are the **only** place gate numbers live. Other docs link here.

---

## M0: Prove the Pi

**Goal.** Measure every unmeasured number in the design on real hardware, concurrently, with HA running, and decide go/no-go for v0.1 as specified. Duration target: 2–3 weeks. Owner: whoever has the Pi. Session-doable and hardware-gated steps are split out in [CLAUDE.md §12](../CLAUDE.md); a session stops and asks the owner at a hardware-gated step and never fakes results.

**Scope.**

- `tools/bench/`: `beepboop bench` with the subcommands in [docs/OPERATIONS.md §11](OPERATIONS.md#11-observability) and a fixture corpus recorded with `tools/bench/record_fixture.py` through an XVF3800 (≥ 6 speakers, ≥ 200 utterances, a 24 h living-room soak recording for wake-word false accepts).
- `tools/train_wakeword/`: a reproducible pipeline producing the stock "Hey beepboop" microWakeWord model, with the training data licenses recorded.
- A minimal Bedrock ConverseStream `LLMProvider` with `cachePoint`, enough to measure TTFT and pass the conformance-suite skeleton.
- The event protocol in [docs/ARCHITECTURE.md §4](ARCHITECTURE.md#4-audio--core-event-protocol) as pydantic models with an in-process fake.
- `deploy/compose.yaml` skeleton with the four services, and verification of the HA items below.
- Results committed to `tools/bench/results/` and summarized with dates in [docs/VOICE_PIPELINE.md](VOICE_PIPELINE.md) and here.

**Go/no-go gates.** All measured on a **4 GB** Pi 4 with fan and SSD, HA running with the default recorder config, during `bench e2e`. The 8 GB run is recorded alongside.

| # | Gate | Threshold | If it fails |
|---|---|---|---|
| G1 | Speaker ID (CAM++ fp32, 2 threads) latency | p95 < 200 ms for a 2 s clip, concurrent with STT | Try int8 after a household EER A/B (must not raise EER > 0.5 pt absolute). No int8 CAM++ export is published, so this first needs a self-made one: onnxruntime dynamic quantization of the pinned fp32 model, added to `models/manifest.yaml` with its own sha256 ([VOICE_PIPELINE §8.1](VOICE_PIPELINE.md#81-model)). If still failing: final ID moves to "provisional only until end-of-speech + 150 ms", and `personal` turns accept a 150 ms added wait. ADR required. |
| G2 | Streaming STT (zipformer small, 2 threads) | p50 RTF < 0.6 and p50 final tail ≤ 100 ms, p90 ≤ 1.3× each. Final tail here is `bench stt`'s per-clip number: end of input fed to the recognizer (`t_eos`, stream flushed) → final result. It is not the e2e `stt_tail` stage, which starts at the last VAD-positive frame and so includes the endpoint hangover ([OPERATIONS §11](OPERATIONS.md#11-observability)) | Cloud STT (Deepgram Nova-3) becomes the **default** with local as fallback, and the privacy ledger and README say so prominently. Second option: 8 GB minimum with a 4-thread cap. ADR required. |
| G3 | Bedrock Haiku 5.5 TTFT, pre-warmed TLS, home connection | **Warm:** first-call TTFT p50 ≤ 600 ms and second (cached, tool-result) call p50 ≤ 500 ms; the arbiter is tool-turn `first_speech` p50 ≤ 2.0 s. **Cold cache (mandatory scenario):** gap ≥ 6 min since the last turn, per-profile prefix; reported separately next to the warm number, not gated. M0 decides between `llm.cache_ttl: 1h` and a smaller prefix from these two numbers. | Measure a regional inference profile; test Haiku 4.5; shrink the always-loaded tool set. If still failing, the no-tool contract loosens to 1.8 s by ADR and the fast path grammar expands to cover more turns. |
| G4 | RAM | Peak of (`MemTotal` − `MemAvailable`) sampled at 1 Hz during `bench e2e` < 3.2 GiB (3,435,973,836 bytes) on a 4 GB (nominal) Pi; zram used < 25 %; **zero OOM kills of any container (incl. HA) over 24 h**, with the default profile and again with `music` on | **8 GB becomes the minimum.** ADR required; BOM and README update. |
| G5 | Wake word, stock "Hey beepboop" model | ≤ 0.5 false accepts per 24 h on the soak corpus, ≤ 5 % false rejects at 3 m | Retrain with more negatives; add a Silero VAD gate; raise threshold and accept more false rejects; last resort a second-stage verifier. Does not block v0.1 but blocks the v0.1 **release**. |
| G6 | End-to-end latency contract ([ADR 0015](adr/0015-latency-contract-and-fast-path.md)) | p50: fast path ≤ 0.5 s offline, including a **1 s command** case ("Hey beepboop, lights off"); first audio < 1.0 s (p95 for the earcon); no-tool < 1.5 s; tool < 2.0 s. p90 ≤ 1.3× each. Stage check: TTS first chunk p50 ≤ 200 ms. | Decompose by waterfall; apply G1–G3 fallbacks to the stage that overran. If no combination fits, the contract is renegotiated with the user by ADR; the spec does not quietly relax. |
| G7 | Thermal | No `0x4` throttle bit during a 20-minute `bench e2e` with the reference fan case | Document the specific cases that pass; make a stronger case the BOM default. |

**Verification items** (pass/fail, not performance):

- V1 The pinned HA version honours `http: server_host: 127.0.0.1` in a container, and the nftables default-deny input chain drops LAN → :8123. Record which (or both) is load-bearing.
- V2 HA headless onboarding (`/api/onboarding/*`) and `auth/long_lived_access_token` over WebSocket work from core using the onboarding session; record the exact sequence.
- V3 XVF3800 16 kHz USB firmware exposes the processed ASR beam; playback through the array's output is the AEC reference (TTS at 70 dB SPL from 1 m does not trigger the wake word and does not corrupt STT of a simultaneous speaker). If AEC reference routing cannot be confirmed, barge-in stays in v0.3+ and the audio service mutes the wake path during playback.
- V4 Raw ALSA in the container with `network_mode: none` runs for 24 h without device-busy errors or xruns > 1/h. **Variant:** the same 24 h with concurrent SSD write load and a `docker pull` in progress, xruns ≤ 1/h.
- V5 The passphrase-wrapped KEK (the v0.1 default, owner decision D3 of 2026-11-03) works on a Pi without an OTP key: create, reload across a reboot, reject a wrong passphrase, round-trip a secret. Optional, not required: the OTP device key is readable from the core container via `/dev/vcio` and produces a stable KEK across reboots.

**Acceptance criteria.**

1. `beepboop bench all --gates` exits 0 on the 4 GB Pi, or every failing gate has an accepted ADR choosing its fallback.
2. `tools/bench/results/` contains JSON for 4 GB and 8 GB runs, and the docs cite them with dates.
3. The "Hey beepboop" model is regenerable from `tools/train_wakeword/README.md` by someone who was not there.
4. V1–V5 are recorded in [docs/HOME_ASSISTANT.md](HOME_ASSISTANT.md), [docs/VOICE_PIPELINE.md](VOICE_PIPELINE.md) and [docs/DATA_AND_PRIVACY.md](DATA_AND_PRIVACY.md) with the HA version, firmware version and date.
5. The Bedrock probe passes the conformance tests for text streaming and cancellation.

**Out of scope.** Any user-facing feature, the web UI, modules, policy enforcement, enrollment UX. M0 code is allowed to be throwaway except `tools/`, the event models and the Bedrock provider.

**Status (2026-11-03).** Every session-doable step is code-done and green on x86 (`ruff`, `pyright` strict, `pytest`, link check). **No gate (G1–G7) and no verification item (V1–V5) has a result yet**: each needs the Pi, the XVF3800 or a human, and is pending the owner. The ordered owner procedure is [tools/M0_OWNER_CHECKLIST.md](../tools/M0_OWNER_CHECKLIST.md).

| Step ([CLAUDE.md §12](../CLAUDE.md#12-what-to-do-first-m0)) | Status | Code | Owner action / README |
|---|---|---|---|
| 1 Scaffold | done | `pyproject.toml`, `core/`, `audio/`, `tools/ci/`, `.github/workflows/ci.yml` | — |
| 2 Event protocol | done (protocol v2) | [`protocol/beepboop_protocol/`](../protocol/README.md) | — (on the Pi: check the `/run/beepboop` socket is shared between core and audio) |
| 3 Compose skeleton | code-done, pending owner (V1) | [`deploy/`](../deploy/README.md) | [`tools/probes/ha_bind/`](../tools/probes/ha_bind/README.md) |
| 4 Wake-word pipeline | code-done, pending owner (training run) | [`tools/train_wakeword/`](../tools/train_wakeword/README.md) | train, publish `hey_beepboop_v1`, pin it in `models/manifest.yaml` |
| 5 Bench harness | done (harness); gate runs are step 10 | [`tools/bench/`](../tools/bench/README.md) | — |
| 6 Synthetic CI bench | done | [`tools/bench/synthetic/`](../tools/bench/synthetic/README.md), `bench-synthetic` CI job (nightly) | — |
| 7 Bedrock TTFT probe | code-done; conformance (text streaming, cancellation) passes; **G3 pending owner** | [`core/beepboop/providers/llm/`](../core/beepboop/providers/llm/README.md) | `beepboop llm probe --gate` on the Pi ([bedrock README](../core/beepboop/providers/llm/bedrock/README.md)) |
| 8 HA headless onboarding | code-done, pending owner (V2) | [`core/beepboop/ha/`](../core/beepboop/ha/README.md), [`core/beepboop/secrets/`](../core/beepboop/secrets/README.md) | [`tools/probes/ha_onboarding/verify_v2.md`](../tools/probes/ha_onboarding/verify_v2.md) |
| 9 Fixture corpus | blocked on owner (XVF3800 + people) | `tools/bench/record_fixture.py` | [`tools/bench/README.md`](../tools/bench/README.md#pending-on-hardware-owner-actions) |
| 10 Bench runs (G1–G6) | blocked on owner (Pi + corpus) | `beepboop bench all --gates` | [`tools/bench/README.md`](../tools/bench/README.md#pending-on-hardware-owner-actions) |
| 11 Hardware probes (V3, V4, V5) | code-done, pending owner | `beepboop bench aec`, `tools/probes/alsa_soak/`, `tools/probes/otp_kek/` | [aec_reference](../tools/probes/aec_reference/README.md), [alsa_soak](../tools/probes/alsa_soak/README.md), [otp_kek](../tools/probes/otp_kek/README.md) |
| 12 Thermal (G7) | code-done, pending owner | `beepboop bench thermal`, `tools/probes/thermal_g7/` | [thermal_g7](../tools/probes/thermal_g7/README.md) |

Result files so far. **Neither is a gate result**; they are cited only as they label themselves:

- [`tools/bench/results/2026-11-02-workstation-g3-preliminary.json`](../tools/bench/results/2026-11-02-workstation-g3-preliminary.json): *"preliminary, x86 workstation on the owner's home connection, NOT the Pi gate measurement"*. Haiku 5.5 via `global.` inference profile, us-west-2, 16 calls. Warm first-call TTFT p50 1125 ms (n = 5); second (cached, tool-result) call TTFT p50 1002 ms (n = 6); prompt cache read seen. There is no valid cold-cache sample (a stale pooled connection hit the read timeout) and no valid pre-warm sample (`warm()` bug, since fixed); see the file's `notes`.
- [`tools/bench/results/2026-11-03-desktop-n9qe44h-synthetic.json`](../tools/bench/results/2026-11-03-desktop-n9qe44h-synthetic.json): *"synthetic / x86 / not a gate result"* (Windows 11 x86_64, TTS-generated corpus). Gate evaluator output: G1, G2, G5, G6 `incomplete` (run preconditions unmet: not a Pi, synthetic corpus, no HA), G3, G4, G7 `skipped`; nothing `pass`.

Resolved spec findings (2026-11-03). The step builders' findings were fixed in the spec, code and tests on the owner's decisions of 2026-11-03 (D1 lessac voices accepted, D3 OTP deferred, D4 fix all spec errors, licence moved to GPL-3.0-or-later). The docs and ADRs below now own each fact:

- **512-d CAM++ embeddings; core depending on the audio package.** The protocol is its own light package, `protocol/beepboop_protocol/` (event protocol v2), and the embedding length is negotiated per connection in `hello.speaker.dim`: [ADR 0019](adr/0019-shared-protocol-package-and-negotiated-embedding-dim.md), [ARCHITECTURE §4](ARCHITECTURE.md#4-audio--core-event-protocol), [VOICE_PIPELINE §8.1](VOICE_PIPELINE.md#81-model).
- **Piper phonemiser.** The project is GPL-3.0-or-later, so piper-phonemize / espeak-ng are normal product dependencies of the audio package: [ADR 0020](adr/0020-gpl-3-and-espeak-phonemiser.md), [VOICE_PIPELINE §11.1](VOICE_PIPELINE.md).
- **Lessac-derived voices (D1).** Accepted for wake-word generation and the default TTS voice, with the research-only provenance recorded: [ADR 0020](adr/0020-gpl-3-and-espeak-phonemiser.md), [models/LICENSES.md](../models/LICENSES.md), [OPERATIONS §13](OPERATIONS.md).
- **KEK (D3).** The passphrase-wrapped KEK is the v0.1 default; the OTP key is an optional later upgrade (V5 above): [ADR 0011](adr/0011-sqlite-single-file.md), [DATA_AND_PRIVACY §6.3](DATA_AND_PRIVACY.md).
- **Protocol, HA, LLM-provider and ops spec errors (D4).** `turn_id` rule, `health.aec_verified`, `speak` clip keys and framing limits ([ARCHITECTURE §4](ARCHITECTURE.md#4-audio--core-event-protocol)); shared log redaction ([CLAUDE.md](../CLAUDE.md) rule 5); HA onboarding token revoke and canonical URLs ([HOME_ASSISTANT §2–§3](HOME_ASSISTANT.md)); Bedrock bearer auth, retry and warm-up ([LLM_PROVIDERS §3](LLM_PROVIDERS.md)); firewall, `.env` and `ports.yaml` ([OPERATIONS §2–§3](OPERATIONS.md)).

Still open:

- **D2 (AWS profile)**: owner decision pending.
- **Zipformer `bpe.model`**: the pinned zipformer ships no `bpe.model`, so per-profile hotwords need a different export or a separately pinned `bpe.model` in v0.1; the hotword WER delta is unmeasured ([VOICE_PIPELINE §10.2](VOICE_PIPELINE.md)).
- **int8 CAM++**: needs a self-made quantization before the G1 A/B (G1 above).
- **`speak` unknown-key rule**: an unknown clip key with no `text` plays the `error` earcon and reports `speak.done` reason `error` ([ARCHITECTURE §4.2](ARCHITECTURE.md#4-audio--core-event-protocol)); a builder's choice awaiting owner confirmation.

---

## v0.1: One house that knows your voice

**Goal.** A household installs beepboop from `deploy/install.sh`, enrolls voices, grants devices, and uses it daily for lights, media, timers, lists, weather, calendar and music, with `critical` actions protected by web confirm. Everything local except the LLM.

**Scope.**

| Area | Deliverable |
|---|---|
| Audio service | "Hey beepboop" wake (microWakeWord), Silero VAD, CAM++ speaker ID with provisional + final stages, streaming zipformer STT with per-profile hotwords, Piper-format TTS via sherpa-onnx, earcons and LED states, hardware mute respected, `capture_pin` mode. Locale carried end to end; English only shipped. |
| Core | Agent loop (sentence chunking, 90 s conversation window and 8 s reply window, cancellation), deterministic fast path, Bedrock ConverseStream with Haiku 5.5 (Sonnet 5.5 opt-in), prompt layout with `cachePoint`, fallback ladder, voice prompt contract, `Principal` on every call, policy middleware with the four tiers, grants, ceilings, taint rule, approvals table, usage and cost tracking per turn (budgets are v0.2). |
| Registry and tools | The 12 always-loaded tools (`home.turn_on`, `home.turn_off`, `home.set`, `home.state`, `media.control`, `timer.set`, `timer.list`, `timer.cancel`, `list.add`, `list.read`, `find_tools`, `read_guide`), server-side entity resolver, module cards, deferred module tools. External `/mcp` door is **not** exposed. |
| Modules | `ha` (headless onboarding, WebSocket state cache, UUID grants, new-device inbox, indirect-service denylist), `timers` (timers, alarms, reminders; persistent in `ctx.kv`; per profile; ring on the device), `lists` (shared and personal), `weather`, `caldav` (per-profile CalDAV with app passwords; read and create events), `music` (Music Assistant via HA, per-profile default player), `persona` (household default, per-profile voice, rate, verbosity). |
| Second factor | Web UI confirm: device says "approve on your phone"; pending-approvals banner; approval bound to profile, args digest, single-use nonce, ≤ 60 s. Optional per-profile spoken PIN fallback captured in the audio service, rate-limited with lockout. |
| Web UI | Claim-code onboarding with the Recovery kit step; admin and member logins (password + TOTP); People (profiles, presets Admin/Member/Child/Guest, the Guest access action, invites); `People › <name> › Voice` (enrolment with quality gates, and a plain-sentence similarity check: "Alex and Sam sound alike; add a few more clips"); Access (grants) and the new-device inbox; Integrations (auto-rendered settings forms); Activity ("why did you do that", waterfall, "that wasn't me"); Approvals; System (Health, Updates notify, Backups, LLM, HTTPS, Logs); Chat ("test as user X"); Privacy ledger, export, delete. |
| Ops | `deploy/install.sh`, Compose with digests, Caddy Tier 0 with a name-constrained local CA, default-deny nftables, backups and restore per [docs/DATA_AND_PRIVACY.md §11](DATA_AND_PRIVACY.md) with restore at onboarding, update **notify** plus the host-side `beepboop update` CLI with snapshot/health/rollback, `models/LICENSES.md` lint, structlog, per-turn traces, `host.json` on the health page. |
| Dev kit | `beepboop module new`, `module check` (evals dry-run in CI), `chat --as`, `policy explain`, provider conformance suites, `bench all --gates` on a self-hosted Pi runner. |

**Acceptance criteria.** Each is a test or a scripted check.

1. **Install.** On a fresh Pi OS Lite 64-bit image, `deploy/install.sh` reaches a spoken claim code in ≤ 15 minutes on a 50 Mbit/s connection with no manual steps other than the Imager settings.
2. **Latency.** `bench e2e` on the v0.1 release candidate meets the [ADR 0015](adr/0015-latency-contract-and-fast-path.md) table under the statistic above, on both RAM variants; CI publishes the waterfall.
3. **Identity.** On the fixture household (6 speakers), closed-set accuracy ≥ 97 % on 2 s utterances with the Guest fallback counted as an error; impostor acceptance into a `personal` tool ≤ 1 % at the calibrated threshold; a child misidentified as an admin never completes a `critical` action in the eval suite.
4. **Policy.** Property-based tests show: no tool executes without `policy.authorize`; a `critical` tool never executes without a consumed approval nonce or verified PIN; a tainted turn requires confirmation for every non-`read` call; a profile never exceeds its ceiling; a new HA entity is unreachable by every profile until granted.
5. **Approvals.** Playwright: a `critical` voice request produces a banner on the same profile's web session within 2 s; approving from a different profile's session fails; the nonce cannot be reused; an approval after 60 s is rejected and the device says so.
6. **Offline.** With the uplink unplugged: "Hey beepboop, turn off the kitchen lights", "set a timer for ten minutes" and "add milk to the shopping list" complete via the fast path at p50 ≤ 0.5 s; "what's the weather" gets a spoken "I can't reach the internet" within 1 s.
7. **Modules.** Every v0.1 module passes `module check` with ≥ 3 eval cases per tool and ≥ 1 denial; live tool selection ≥ 90 % per module against Haiku 5.5; no module imports an MCP, HA or LLM SDK or `beepboop.ha` (CI grep).
8. **Privacy.** No audio bytes on disk after a turn (fs scan in CI on the Pi runner); transcripts expire at 30 days; "delete me" leaves zero rows for that profile across all tables; export produces a zip the restore test can read.
9. **Ops.** Restoring a backup onto a second Pi with the recovery phrase reproduces profiles, grants, voiceprints and HA config; `beepboop update` to a deliberately broken image rolls back and says so; `models/LICENSES.md` lint passes; the health endpoint reports all fields in [docs/OPERATIONS.md §11](OPERATIONS.md#11-observability); `beepboop doctor` flags an undeclared listening port.
10. **Docs.** A new engineer scaffolds and ships a toy module from [docs/MODULES.md](MODULES.md) and [docs/module-template/](module-template/README.md) alone (tested by giving the docs to a fresh Claude Code session). The template's Python is linted by ruff today and must also pass `pyright --strict` once `beepboop.module` and `beepboop.testing` exist: remove `docs` from the pyright `exclude` in `pyproject.toml` (keeping `docs/research` out) as part of this criterion.

**Out of scope for v0.1.** External `/mcp` clients, OAuth modules (Google, Spotify native), push or passkeys, HA Companion phone confirm, Tier 1 HTTPS, one-click update button in the UI, cloud STT/TTS, presence prior, voiceprint adaptation, "sounds like you" live meter, re-enrol prompts, false-wake review queue, household wake-word fine-tune, `home://context` resource, MCP "pin to core", Bedrock Guardrails, per-profile "require TOTP again for approvals", policy YAML import/diff, cost budgets, memory, routines and module event subscriptions, barge-in, satellites, third-party MCP proxy, non-English speech models.

---

## v0.2: Your accounts, your devices

**Goal.** Each person connects their own cloud accounts, their phone confirms `critical` actions, and their own Claude or other MCP client can act on the home as them.

**Scope.**

- **HTTPS Tier 1:** own domain via ACME DNS-01 or Tailscale; passkeys (`webauthn`) on Tier 1; configurable canonical URL exercised end to end.
- **External MCP door** at `/mcp`: Streamable HTTP with the official `mcp` SDK, beepboop as OAuth 2.1 authorization server (RFC 9728, 8707, 9207, PKCE, Client ID Metadata Documents, DCR fallback), personal access tokens, per-token `tools/list` with `cacheScope: private`, URL-mode elicitation to `/confirm/<id>` for `critical`. Requires Tier 1. Also: the `home://context` resource and MCP "pin to core" (a per-profile setting that keeps a deferred tool always loaded).
- **Phone confirm** via HA Companion actionable notifications as a second-factor option alongside web confirm; same approval record. Per-profile "require TOTP again for approvals".
- **OAuth bounce page and OAuth modules:** a static page on GitHub Pages (`oauth.beepboop.dev`, planned domain) that holds no secrets and only redirects to allowed local callback shapes; the Pi validates the signed, session-bound state at `/oauth/callback` (PKCE throughout). Native `google_calendar` and `spotify` modules with bring-your-own client credentials and detailed `SETUP.md`.
- **Generic HA data-entry-flow renderer** and per-profile HA config entries with reauth surfacing ("Alex, reconnect Google").
- **LLM:** OpenAI-compatible adapter (Ollama, llama.cpp, vLLM, OpenRouter), `bedrock-mantle` Messages adapter behind a smoke test; **cost budgets** per household and profile with a spoken warning (tracking exists from v0.1); Bedrock Guardrails as an opt-in content filter for child profiles.
- **Cloud speech provider configuration:** Deepgram Nova-3 STT and Amazon Polly TTS plugins selected in `/config/beepboop.yaml` and enabled by the `deploy/compose.cloud-speech.yaml` override, which gives audio a network and updates the privacy ledger; Wyoming client adapters for external STT/TTS servers as Compose profiles.
- **Identity:** HA `person` presence prior; voiceprint adaptation from high-margin uncorrected turns with nightly centroid recompute; "sounds like you" live meter; re-enrol prompts when a voice drifts. An opt-in false-wake queue (and any household wake-word fine-tune that depends on it) may be proposed via a new ADR superseding [ADR 0018](adr/0018-no-audio-retention.md); until then no audio is kept.
- **Ops:** one-click update button (same host procedure via `beepboop-update.path`), S3-compatible remote backup destinations (USB and SMB are v0.1), ducking hook on wake, policy YAML import and diff, `module check --llm` in nightly CI.

**Acceptance criteria.**

1. Claude Desktop (or any client implementing MCP 2026-07-28) connects to `https://<domain>/mcp`, authenticates through the beepboop login page, sees only that profile's tools, and a `critical` call opens `/confirm/<id>` that only that profile's web session can approve.
2. A Guest token cannot list or call anything above `act`; `tools/list` for two different profiles differ exactly by their grants (snapshot test).
3. Two household members connect two different Google accounts and two Spotify accounts through the bounce page; "what's on my calendar" and "play my Discover Weekly" resolve per speaker; a revoked token produces the reauth prompt in the UI and a correct spoken message; a forged or replayed `state` is rejected at `/oauth/callback`.
4. Phone confirm approves a `critical` action within the 60 s window and is recorded with `factor: phone`; the web banner remains available as fallback.
5. The OpenAI-compatible adapter passes the full conformance suite in [docs/LLM_PROVIDERS.md §6](LLM_PROVIDERS.md) against Ollama on a LAN machine; the eval suite runs against it and reports accuracy per module.
6. Enabling cloud STT changes the privacy ledger text and the health page provider list; disabling it returns to `network_mode: none` for audio (verified by a connectivity test from inside the container).
7. One-click update on a broken image rolls back from the UI with the same outcome as the CLI path.

**Out of scope.** Memory, routines and announcements, module event subscriptions, barge-in, satellites, third-party MCP proxy, parental schedules and content filters beyond the Guardrails opt-in, liveness, multilingual, external (existing) Home Assistant mode.

---

## v0.3+: Memory, routines, more rooms

**Goal.** beepboop remembers per person, acts proactively, can be interrupted, extends to other rooms, and brokers third-party MCP servers safely. Order within v0.3+ is set by demand after v0.2 ships.

**Scope.**

- **Per-profile memory** (`memory.*` tools, sqlite-vec) with view, edit, forget; never cross-profile; never visible to Guest.
- **Module event subscriptions** (with routines and proactive announcements), including how events acquire a principal. HA event subscriptions, MCP prompts for user-invoked routines, addressed announcements ("tell Sam when the dryer's done"), quiet hours.
- **Barge-in**, gated on V3 (AEC verified), with diarization check that the interrupter is the same speaker.
- **Full parental controls:** schedules, content filter (prompt layer plus optional Bedrock Guardrails), purchase block, a household transcript-sharing toggle for adults. All expressed as grants, ceilings and schedules.
- **Wyoming satellites** (ESP32, second Pi): nearest-device arbitration, per-room timers, intercom and broadcast.
- **Third-party MCP proxy** (`kind: mcp_proxy`, per user): description hashing with alerts on change, default `critical`, lazy connect, remote HTTP preferred over stdio for RAM. Third-party modules as Python entry points land here too; until then, copy a module into `modules/`.
- **Liveness plugin** (AASIST-L, off by default), multilingual profiles, per-user wake aliases as an identity prior, optional `beepboop_bridge` HA component so HA's own Assist respects beepboop grants, `whoami` tool.

**Acceptance criteria (per feature, when scheduled).**

1. Memory: "remember that I'm allergic to peanuts" is retrievable only by the same profile, appears in the privacy ledger, and "forget that" removes the row and its embedding.
2. Routines: a doorbell event announces to the room where the last turn happened within 2 s; quiet hours suppress it and log the suppression; the announcement runs under a documented principal.
3. Barge-in: speaking "Hey beepboop, stop" at 1 m during TTS cancels playback within 300 ms, and a different speaker cannot cancel a `personal` turn's reply window.
4. Satellites: two devices hearing the same wake produce one turn, answered on the device with the higher wake score; per-room timers ring on their room.
5. MCP proxy: a changed upstream tool description blocks the tool and alerts the admin; an upstream tool defaults to `critical` until reclassified.

**Out of scope, indefinitely.** A local LLM on the Pi 4 (the fast path covers that job), silent updates, a beepboop mobile app (HA Companion and the web UI cover it), a hosted beepboop cloud service beyond the static bounce page.

---

## Ideas parking lot

Every predicted feature from the synthesis, mapped to where it lands. "Core" means it is part of the architecture from v0.1 even if the UI for it grows later.

| # | Feature | Milestone | Notes |
|---|---|---|---|
| 1 | Per-user timers, alarms, reminders that survive restarts | v0.1 | `timers` module; state in `ctx.kv` |
| 2 | Deterministic local fast path and offline degradation | v0.1 (proved in M0) | Carries the latency contract |
| 3 | "Why did you do that?" activity timeline with "that wasn't me" | v0.1 | Doubles as the module debugger |
| 4 | Text chat / CLI "test as user X" through the real policy path | v0.1 | `beepboop chat --as`; Chat page |
| 5 | Shared and personal lists | v0.1 | `lists` module |
| 6 | Per-turn latency waterfall and `beepboop bench` | M0 (bench), v0.1 (UI) | CI on a real Pi |
| 7 | Module dev kit: `module new`/`check`, dry-run, evals, conformance | v0.1; `module check --llm` in CI v0.2 | |
| 8 | Privacy affordances: mute state, LED/chime, "forget the last thing I said", no audio retention, export, delete | v0.1 | "forget the last thing" deletes the turn row |
| 9 | Music with per-user accounts and ducking on wake | v0.1 (Music Assistant via HA); ducking hook and native Spotify v0.2 | |
| 10 | Presence-aware identity prior and addressing | v0.2 (prior); v0.3+ (addressed announcements) | HA `person` entities |
| 11 | Per-user STT vocabulary; per-user TTS voice and rate | v0.1 | `SttContext.hotwords`; `persona` module |
| 12 | Voice-ID trust page: similarity check, live "sounds like you" meter, re-enrol prompts | v0.1 (plain-sentence similarity check); v0.2 (live meter, re-enrol prompts) | |
| 13 | Backup/restore at onboarding; safe updates with rollback | v0.1 (host-side CLI update, restore); v0.2 (one-click UI) | Never silent |
| 14 | Cost and usage budgets with a spoken warning | v0.2 | Cost tracking per turn is v0.1 |
| 15 | Babysitter / time-boxed guest access | v0.1 | Ordinary `guest` grants with an expiry ([IDENTITY §9](IDENTITY_AND_TRUST.md#9-presets)); not a preset, no code path |
| 16 | Announcements, intercom, broadcast | v0.3+ | Foundation for satellites |
| 17 | Accessibility: text replies to phone or screen, slower speech | v0.1 (rate per profile, Activity shows text); v0.2 (push text via HA Companion) | |
| 18 | Per-user wake aliases as an identity prior | v0.3+ | microWakeWord runs several models cheaply |
| 19 | Multilingual per profile | v0.3+ | Locale is carried end to end from v0.1 |
| 20 | Exportable, diffable YAML ACL policy | v0.2 | Export in v0.1 backups; import and diff v0.2 |
| — | New-device inbox | v0.1 | Default-deny makes it necessary on day one |
| — | Privacy ledger | v0.1 | Grows with each provider added |
| — | False-wake review queue; household wake-word fine-tune | v0.2 at the earliest, only via a new ADR | Would store audio; [ADR 0018](adr/0018-no-audio-retention.md) forbids it in v0.1 |
| — | `home://context` resource; MCP "pin to core" | v0.2 | Ships with the external door |
| — | Bedrock Guardrails opt-in | v0.2 | Prompt-layer filter stays the v0.1 baseline |
| — | Per-profile "require TOTP again for approvals" | v0.2 | |
| — | Household transcript-sharing toggle for adults | v0.3+ (parental controls) | Default: adults see each other's actions, not transcripts |
| — | Module event subscriptions, routines, proactive announcements | v0.3+ | Needs a principal for events |
| — | External (existing) Home Assistant mode | Out of scope through v0.2 | beepboop ships and pins its own HA; revisit by ADR |
| — | Liveness / anti-spoofing plugin | v0.3+ | Off by default; never promised |
| — | `beepboop_bridge` HA component | v0.3+ | Only if demand exists |

## How this roadmap changes

A milestone's scope changes by PR to this file plus, when the change touches a decision, an ADR. Gate thresholds in M0 change only by ADR. Features move between milestones freely; features move out of "out of scope, indefinitely" only by ADR.
