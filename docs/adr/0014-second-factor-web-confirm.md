# 0014. The v0.1 second factor is web UI confirm without push, with an optional spoken PIN

> Status: Accepted · 2026-11-01 · Applies to: v0.1, v0.2 (adds phone confirm), v0.3+

For `critical` actions beepboop says "approve on your phone", and the user opens the beepboop web UI where a pending-approvals banner lets the same profile approve within 60 seconds. A per-profile spoken PIN is an optional fallback captured entirely inside the audio service. Push, passkeys and HA Companion confirm arrive later with Tier 1 HTTPS.

Related: [docs/IDENTITY_AND_TRUST.md](../IDENTITY_AND_TRUST.md) · [docs/WEB_UI.md](../WEB_UI.md) · [docs/MCP.md](../MCP.md) · [ADR 0005](0005-risk-tiers-and-grants.md) · [ADR 0006](0006-voice-id-is-personalization.md) · [ADR 0013](0013-https-tiers.md)

## Context

`critical` actions (unlock, disarm, garage, purchases, messaging new recipients) must never rely on voice alone ([ADR 0006](0006-voice-id-is-personalization.md)). The research proposed a spoken PIN for v0.1, but a PIN said aloud in a shared room can be overheard or recorded, and a child can learn it. Phone push needs either a mobile app or HA Companion notifications, and both need a publicly trusted certificate (Tier 1), which the default Tier 0 install does not have. MCP 2026-07-28 already models confirmation as `input_required` plus URL elicitation to a page that requires a matching web session, so the web-confirm shape serves voice and external clients alike. The spec forbids form-mode elicitation for anything that authorizes a transaction.

## Decision

When policy requires a second factor, core creates an `approvals` row `{nonce, profile_id, tool, args_digest, exp ≤ 60 s}` and the device says "approve on your phone". Any web session belonging to that same profile shows a pending-approvals banner naming the action; approving consumes the nonce once and the turn proceeds. A different profile's session cannot approve. Expiry speaks a short "that timed out". Optionally, a profile may enable a spoken PIN: core sends `capture_pin` to the audio service, which listens, hashes, compares against `pin_hash`, and returns only a boolean; the digits never enter STT output, the LLM request, logs or transcripts. PINs are rate-limited with lockout. In v0.2, HA Companion actionable notifications and passkey-backed approvals become additional factors writing the same `approvals` record (`factor: phone | web | pin | elicit`). For external MCP clients the same record backs URL elicitation to `/confirm/<id>` with an AEAD-sealed `requestState`.

## Consequences

- Nothing hosted, nothing pushed, works at Tier 0 on day one.
- The user must open the app; for a lock this is acceptable friction and visibly stronger than a voice-only unlock.
- Pending approvals are a natural audit surface and appear in the Activity timeline.
- The PIN path requires the audio service to run a digit-recognition mode; the fallback is optional precisely because it is weaker.

## Alternatives considered

- **Spoken PIN only**: overhearable; rejected as the default.
- **Push notifications in v0.1**: need Tier 1 or a hosted relay.
- **Form-mode elicitation for PINs over MCP**: forbidden by the MCP spec.
- **No `critical` actions by voice at all**: safe but removes a core use case; the approval flow keeps it.
