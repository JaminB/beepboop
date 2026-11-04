# beepboop.ha: the Home Assistant client

The only package that talks to Home Assistant or holds its token
([HOME_ASSISTANT.md](../../../docs/HOME_ASSISTANT.md)). Modules never import it
(hard rule 2); they get the policy-wrapped `ctx.ha`.

| Module | What |
|---|---|
| `onboarding.py` | Headless onboarding ([§3](../../../docs/HOME_ASSISTANT.md#3-headless-onboarding)). `HAOnboarding(session, store, config).run()` goes through `/api/onboarding` → `users` → `/auth/token` → `core_config` → `analytics` → `integration` → WS `auth/long_lived_access_token` → `/auth/token` `action=revoke` (the onboarding refresh token), then checks every step is done. The owner password and the long-lived token go into the secrets envelope. It is idempotent: a stored token means "already onboarded". An HA that someone else onboarded returns `needs_login`. |
| `ws.py` | The minimal authenticated WebSocket session onboarding uses, and the shared error types. |
| `client.py` | `HAConnection` ([§4](../../../docs/HOME_ASSISTANT.md#4-the-websocket-client)): one persistent socket, one reader task, `id`-matched replies, coalesced event frames, `on_connect` hooks (a reconnect is a full resync), 30 s ping with two-miss drop, jittered backoff 1 s → 30 s, `auth_invalid` terminal (`reauth`). |
| `cache.py` | `StateCache` ([§5](../../../docs/HOME_ASSISTANT.md#5-state-cache)): `get_states` then `state_changed`; `stale` while disconnected. |
| `index.py` | `RegistryIndex` (pure build from the four registries + `get_entries` + states) and `HAEntityIndex`, the resolver's `EntityIndex`: active, enabled, visible, granted ([§6](../../../docs/HOME_ASSISTANT.md#6-registries-and-the-resolver)). |
| `inbox.py` | `sync_entities`: mirror into `ha_entities`; new UUIDs land `new` (default deny), indirect services `declared_risk = critical`, personal config entries skip the inbox. |
| `plan.py` | The tool → service tables and `HATargetExtractor`, which turns a `home.*`/`media.control` call into policy `Target`s before `authorize` ([§9](../../../docs/HOME_ASSISTANT.md#9-service-allowlist-and-risk-tiers)). |
| `wrapper.py` | `PolicyHAClient`, the module-facing `HAClient` ([§8](../../../docs/HOME_ASSISTANT.md#8-the-policy-wrapped-client-ctxha)): granted entities only; `call_service` re-runs `authorize` on the real service. |
| `service.py` | `HAService`: owns all of the above; `beepboop.service.CoreService` starts it once onboarding has stored the token. |

Tokens travel as `pydantic.SecretStr`. Nothing here logs a token, auth code or password,
and the tests assert that against real structlog output.

**Run the tests:** `uv run pytest core/tests/ha`. They use an aiohttp fake HA for
onboarding and a fake HA WebSocket (`beepboop/testing/fake_ha.py`, re-exported as `core/tests/ha/fake_ws.py`) whose messages mirror HA
2026.10.3's `websocket_api`, plus the fixture household in `core/tests/ha/fixtures/`. No
Docker is needed.

**Pending on hardware / the pinned HA container:** V2 (onboarding against the pinned
image, [tools/probes/ha_onboarding/verify_v2.md](../../../tools/probes/ha_onboarding/verify_v2.md)),
the bridge contract tests of [§13](../../../docs/HOME_ASSISTANT.md#13-upgrade-strategy-and-bridge-contract-tests)
(which pin the real `get_entries`, event and `call_service` shapes the fake assumes), and
the §16 timings (startup < 2 s on 200 entities, `call_service` p50, resolve + authorize).
None of these is claimed.
