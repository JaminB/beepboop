# 0001. Home Assistant Core is the integration backbone, over one WebSocket, behind our policy

> Status: Accepted · 2026-11-01 · Applies to: v0.1, v0.2, v0.3+

beepboop does not integrate with devices itself. It runs Home Assistant Core as a container, talks to it over one persistent WebSocket from the `ha` module, and enforces every per-person permission on its own side.

Related: [docs/HOME_ASSISTANT.md](../HOME_ASSISTANT.md) · [docs/IDENTITY_AND_TRUST.md](../IDENTITY_AND_TRUST.md) · [ADR 0005](0005-risk-tiers-and-grants.md) · [ADR 0016](0016-docker-compose-mit.md)

## Context

A home assistant needs thousands of device integrations; writing them is not beepboop's job. Home Assistant (HA) has them, is Apache-2.0, and runs on a Pi 4. But HA's permission model is admin vs non-admin, with no entity-level policy UI and no official plan to add one. HA's built-in MCP server (`/api/mcp`) exposes one household-wide exposure list and defaults to admin-only, so it cannot say "Alice's calendar, not Bob's". HA's REST API is for one-off calls; its WebSocket API is what the HA frontend itself uses and offers `get_states`, `subscribe_events`, `call_service`, and the entity, device and area registries.

## Decision

We run the official HA Core image as a sidecar container, pinned by digest, with host networking for discovery and HTTP bound to loopback (nftables as backstop). The `ha` module holds one persistent WebSocket, authenticated with a dedicated admin long-lived token that is envelope-encrypted and never given to any other module. It keeps an in-memory state cache from `get_states` plus `state_changed`, and exposes a **policy-wrapped client**: every target is resolved against the calling profile's grants before `call_service`. HA is the first choice for any integration; a native module is allowed only when HA cannot model per-person accounts. HA's own MCP server stays disabled. HA is onboarded headlessly on first boot; users never see HA's UI.

## Consequences

- One confused-deputy risk: the admin token bypasses everything. Mitigations are loopback binding, nftables, encryption at rest, a service allowlist with indirect services (`script.*`, `scene.*`, `automation.*`, `homeassistant.*`, `rest_command`, `shell_command`, `notify.*`) defaulting to `critical`, and an audit log.
- HA ships monthly breaking changes, so HA is updated only with a beepboop release, after the `ha` module's contract tests pass against that digest.
- HA attribution shows "beepboop-system"; optional per-profile HA users can be created for audit only.
- We lose HAOS add-ons and recreate the important ones (Mosquitto, Z-Wave JS UI, Matter, Music Assistant) as Compose profiles.

## Alternatives considered

- **Wrap HA's MCP server**: no per-user scoping, extra hop, HA's prompt logic in the way.
- **HAOS on the Pi**: owns the whole OS; beepboop could not run beside it.
- **Native integrations only**: years of work, worse than HA's.
- **openHAB or others**: smaller ecosystems; a sibling module remains possible later.
