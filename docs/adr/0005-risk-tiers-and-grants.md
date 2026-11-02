# 0005. Four risk tiers and one grant mechanism, enforced in middleware

> Status: Accepted · 2026-11-01 · Applies to: v0.1, v0.2, v0.3+

Every tool carries a risk tier: `read`, `act`, `personal` or `critical`. Permissions are expressed with a single primitive, the grant. Enforcement is deterministic code in the policy middleware, never prompt text.

Related: [docs/IDENTITY_AND_TRUST.md](../IDENTITY_AND_TRUST.md) · [docs/HOME_ASSISTANT.md](../HOME_ASSISTANT.md) · [ADR 0006](0006-voice-id-is-personalization.md) · [ADR 0014](0014-second-factor-web-confirm.md)

## Context

Voice is a weak identity signal, tool outputs (email, calendar, web, third-party MCP) can carry injected instructions, and HA reaches entities indirectly through scenes, scripts and automations. A permission system that lives in the system prompt fails against all three. Household roles (parents, children, guests, babysitters) are varied enough that special-casing each one in code would never end.

## Decision

Each tool declares one of four tiers. `read` needs any principal that holds a grant (Guest allowed). `act` needs an identified speaker or an explicit Guest grant. `personal` needs speaker score ≥ τ_strict and margin ≥ m, otherwise beepboop asks "Is this Alex?" or declines. `critical` always needs a second factor ([ADR 0014](0014-second-factor-web-confirm.md)). Two rules sit on top: the **taint rule** (after a tool with `untrusted_output` runs, every non-`read` call in that turn needs confirmation) and the **ceiling** (each profile has a `risk_ceiling`; Child and Guest cap at `act`). Permissions are grants: `grant(subject: profile|group, resource: tool_glob | ha_entity_uuid | ha_area_id | ha_config_entry_id, max_risk, schedule?)`. Effective permissions are the union of the profile's and its groups' grants, capped by the ceiling. HA resources are keyed by entity-registry UUID so renames do not break grants; new entities are default-deny and land in an inbox. Indirect HA services default to `critical`. Presets (Admin, Member, Child, Guest, Babysitter) are grant templates plus a ceiling plus schedules, nothing more. `policy.authorize(principal, tool, args)` runs before every execution through every door, and `audit` after.

> **Amended 2026-11-01.** The `personal` rule above read "otherwise beepboop asks 'Is this Alex?' or declines". It now reads: `personal` needs speaker score ≥ τ_strict and margin ≥ m, otherwise it **requires a second factor ([ADR 0014](0014-second-factor-web-confirm.md)) or declines; a spoken confirmation never changes a policy decision**. The decision is otherwise unchanged.

## Consequences

- One mechanism to test with property-based tests, one to explain in the UI (`policy explain`), one to export as YAML.
- Module authors declare tiers and never reimplement policy.
- Default-deny means a new device needs one tap in the inbox before voice can touch it; this is the intended friction.
- Scheduled grants give parental controls and babysitters without new code.

## Alternatives considered

- **Prompt-level rules**: bypassable by injection; cannot be tested deterministically.
- **Role-based code paths (`if kid:`)**: multiplies special cases; presets as templates cover it.
- **Two primitives (allowlist plus denylist)**: ambiguity when they overlap; union-of-grants capped by ceiling is simpler.
- **HA's own permission groups**: not exposed in the UI and not entity-scoped.
