# beepboop.ha: the Home Assistant client

The only package that talks to Home Assistant or holds its token
([HOME_ASSISTANT.md](../../../docs/HOME_ASSISTANT.md)). Modules never import it
(hard rule 2); they get the policy-wrapped `ctx.ha`.

| Module | What |
|---|---|
| `onboarding.py` | Headless onboarding ([§3](../../../docs/HOME_ASSISTANT.md#3-headless-onboarding)). `HAOnboarding(session, store, config).run()` goes through `/api/onboarding` → `users` → `/auth/token` → `core_config` → `analytics` → `integration` → WS `auth/long_lived_access_token`, then stores the owner password and the long-lived token in the secrets envelope. It is idempotent: a stored token means "already onboarded". An HA that someone else onboarded returns `needs_login`. |
| `ws.py` | A minimal authenticated WebSocket session (`auth` handshake, then `call(type, **payload)`). The full `HAConnection` with reconnects, subscriptions and the state cache (§4–§6) is v0.1. |

Tokens travel as `pydantic.SecretStr`. Nothing here logs a token, auth code or password,
and the tests assert that against real structlog output.

**Run the tests:** `uv run pytest core/tests/ha`. They use an aiohttp fake HA whose
checks mirror HA 2026.10.3's onboarding, auth and websocket source. No Docker is needed.

**Pending on hardware:** V2, the run against the pinned HA container on the Pi. The
owner procedure is in [tools/probes/ha_onboarding/verify_v2.md](../../../tools/probes/ha_onboarding/verify_v2.md).
PENDING, owner action.
