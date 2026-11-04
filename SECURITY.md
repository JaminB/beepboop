# Security policy

> Status: Draft spec · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

beepboop controls devices in people's homes, holds biometric voiceprints, and stores credentials for personal accounts. This document says how to report a vulnerability, where the threat model lives, what beepboop does and does not claim about voice identification, and which versions receive fixes.

Related: [docs/IDENTITY_AND_TRUST.md](docs/IDENTITY_AND_TRUST.md) · [docs/DATA_AND_PRIVACY.md](docs/DATA_AND_PRIVACY.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md) · [docs/WEB_UI.md](docs/WEB_UI.md) · [CLAUDE.md](CLAUDE.md) · [CONTRIBUTING.md](CONTRIBUTING.md)

---

## 1. Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through **GitHub Security Advisories** on this repository ("Report a vulnerability" under the Security tab). Until the repository is published, or if advisories are unavailable, email the maintainers at the address listed in the repository's GitHub profile with the subject `beepboop security`.

Include what you can: the affected component (`core`, `audio`, `ui`, `deploy`, a module id), the version or commit, steps to reproduce, and the impact you believe it has. A proof of concept is welcome; working exploit chains against live homes are not needed.

**What to expect** (proposed until the first release)

| Step | Target |
|---|---|
| Acknowledgement | within 3 days |
| Initial assessment and severity | within 7 days |
| Fix or mitigation for high/critical severity | within 30 days, sooner where possible |
| Coordinated disclosure | after a fix ships, or 90 days from report, whichever is first, unless we agree otherwise |

We will credit reporters in the advisory unless they prefer otherwise. There is no bug bounty; this is a volunteer project.

**In scope:** everything in this repository, the published container images, the `tools/train_wakeword/` pipeline, and the static OAuth bounce page when it exists (v0.2).
**Out of scope:** Home Assistant itself (report to the Open Home Foundation), sherpa-onnx, microWakeWord, Caddy and other upstreams (we will help you route the report), vulnerabilities that require physical possession of the Pi (see §3), and social engineering of household members.

## 2. Threat model

The full threat model, with assets, adversaries, attack trees and mitigations, is in [docs/IDENTITY_AND_TRUST.md](docs/IDENTITY_AND_TRUST.md). The short version:

**Assets.** Control of locks, alarms, garage doors and other `critical` devices; personal data (calendars, lists, transcripts, memory); voiceprints and the embeddings derived from them; credentials (HA long-lived token, Bedrock keys, CalDAV app passwords, OAuth tokens); the household's trust that the device is listening only when it says it is.

**Adversaries we design against**

| Adversary | Example | Primary defence |
|---|---|---|
| Someone in the room who is not the account holder | A visitor, a child, a sibling who sounds alike | Risk tiers, Guest fallback below threshold, per-profile ceilings, second factor for `critical` |
| Replayed or synthesised voice | A recording of a parent played back; TV audio; a cloned voice | Voice ID never satisfies `critical`; `personal` requires score and margin; audit flags low-score high-risk turns |
| Hostile content reaching the model | A calendar invite or email that says "unlock the door" | `untrusted_output` taints the turn: every non-`read` call afterwards needs confirmation; outbound messaging is `critical`; model output can never change the `Principal` |
| Someone on the LAN | A compromised laptop or IoT device | Inbound is **default-deny** (nftables `input` policy drop with a short allowlist, [OPERATIONS §2](docs/OPERATIONS.md)); HA bound to loopback; web UI behind HTTPS (local CA with name constraints) with password + TOTP; `__Host-` cookies; server-side sessions; CSRF checks |
| Someone on the internet | Scanning, phishing the OAuth bounce | Nothing is exposed by default; external MCP requires Tier-1 HTTPS and OAuth 2.1 with PKCE and resource indicators; the bounce page holds no secrets and only redirects to allowed local callback shapes; the Pi validates the signed, session-bound state at `/oauth/callback` |
| Theft of the SD card or SSD | Card pulled from an unattended Pi | Envelope encryption of beepboop's own secrets and voiceprints. HA's config directory (including HA integration credentials), Music Assistant data, the local CA key and transcripts are plaintext unless the optional LUKS data partition ([OPERATIONS](docs/OPERATIONS.md)) is enabled |
| A buggy or malicious module | A module that tries to call HA directly | Primary defence: code review (all modules are in-repo through v0.2). `ToolContext`, the egress allowlist and the import lint catch mistakes, not malice; a module runs with core's privileges ([IDENTITY_AND_TRUST](docs/IDENTITY_AND_TRUST.md) T9) |
| Supply chain | A compromised upstream image or package | Images pinned by digest, Python deps locked, notify-only updates with snapshot and automatic rollback; no silent updates |

**Where enforcement lives.** Every tool call from every entry point (voice, `/mcp`, CLI, web chat) passes the deterministic policy middleware in `core/beepboop/policy/` and is written to the audit log. Nothing about authorization is delegated to the language model or to prompt text. This is [ADR 0005](docs/adr/0005-risk-tiers-and-grants.md).

## 3. Voice identification is personalization, not authentication

This is the single most important thing to understand about beepboop's security posture, and it is stated here so nobody has to infer it.

Speaker identification chooses *which profile* a turn runs as. It is a convenience: your calendar, your music, your name in the reply. It is **not** proof that you are present. It can be fooled by recordings, by voice cloning, by siblings, by children whose voices are changing, by a cold, and by the television.

Consequently:

- A voice score alone never unlocks a door, disarms an alarm, opens a garage, makes a purchase or messages a new recipient. Those are `critical` and **always** require a second factor: approval in the beepboop web UI (bound to the same profile, the exact arguments, a single-use nonce, and a 60-second expiry), or, if the profile has opted in, a spoken PIN captured by the audio service and never sent to the model or written to a transcript.
- `personal`-tier actions (your calendar, your lists, your memory) require confidence ≥ τ_strict *and* margin ≥ m over the next-best speaker; otherwise a second factor (web or PIN) or deny. A spoken "yes, it's me" never changes a policy decision.
- Below the identification threshold the turn runs as **Guest**, which can read household state and operate lights and media if granted, and nothing else.
- Child and Guest profiles are capped at the `act` tier by default. No voice, however confident, lifts that cap.
- Every turn's audit entry records the score and margin, and the UI flags high-risk actions taken on low-confidence identifications.

If you find a code path or a document in this repository that treats a voice score as sufficient for anything above `act`, that is a security bug. Please report it as one.

## 4. What we do not protect against

Honesty about limits is part of the policy.

- **A stolen SSD, unless you turn on LUKS.** In v0.1 the key that unlocks beepboop's encrypted secrets is wrapped by a passphrase kept in a file on the same SSD, so the device can boot unattended. Whoever takes the SSD has both. Install with `--luks` (you type its passphrase over SSH at every boot) if a stolen disk is in your threat model ([DATA_AND_PRIVACY §6.3](docs/DATA_AND_PRIVACY.md#63-envelope-encryption)). Programming the Pi's OTP key is an optional later upgrade, not done by the installer.
- **Physical possession of the whole Pi.** An attacker with the Pi and its storage together can recover secrets, with or without the OTP upgrade. Raspberry Pi hardware has no protected key store. Treat the device like a laptop: keep it somewhere you trust.
- **A compromised Home Assistant.** beepboop's grants are enforced on beepboop's side of the HA WebSocket. If HA itself is compromised, or an admin creates HA automations that bypass beepboop, beepboop cannot help. HA's own UI is reachable only through a beepboop admin session for this reason.
- **A compromised LLM provider.** Transcripts and tool results are sent to the configured LLM provider (Amazon Bedrock by default). We minimise what is sent (no audio, no PINs, no secrets, redacted arguments in logs), but the provider sees the text of what you said. The UI shows this plainly.
- **Overheard PINs.** A spoken PIN in a shared room can be overheard or recorded. It is optional, rate-limited with lockout, and the web-UI confirmation is the recommended second factor. Phone push confirmation arrives in v0.2.
- **Household members acting in bad faith with admin rights.** An admin can change any grant. Parental visibility into children's history is by design.

## 5. Supported versions

beepboop is in the spec phase; no versions have been released.

| Version | Supported |
|---|---|
| `main` (unreleased) | Security reports welcome; fixes land on `main` |
| v0.1.x (planned) | Will receive fixes until v0.2.0 ships plus 90 days |
| Earlier milestone tags (`m0-*`) | Not supported; benchmarking only |

Once releases exist: the latest minor receives all security fixes; the previous minor receives high/critical fixes for 90 days after the next minor ships. Container images are published by digest and the UI shows when an update is available. Updates are **never** applied silently.

## 6. Security-relevant conventions for contributors

Summarised here; the full list is in [CLAUDE.md §4](CLAUDE.md).

- Policy decisions live in `core/beepboop/policy/`, not in prompts, modules or the UI.
- Modules never import MCP, Home Assistant, LLM or vendor SDKs, nor `beepboop.ha`.
- Audio never leaves the audio service and is never persisted ([ADR 0018](docs/adr/0018-no-audio-retention.md)). Only text events cross to core.
- No secrets, tokens, passwords or PINs in logs, transcripts, tool-call records or LLM context. Redaction is tested.
- All dependencies pinned; images by digest.
- Any change to tiers, grants, the second factor, encryption or session handling requires an ADR and a review from a second maintainer.
