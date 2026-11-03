# core/tests/ha

Unit tests for `beepboop.ha` and for the V2 recorder (`tools/probes/ha_onboarding/record_v2.py`,
loaded by path). `conftest.py` holds `FakeHA`, an aiohttp server that mirrors HA 2026.10.3's
onboarding, `/auth/token` and WebSocket auth behaviour. Run with `uv run pytest core/tests/ha`.
The real HA surface is covered by V2 on the Pi and, from v0.1, by the bridge contract tests
(HOME_ASSISTANT §13).
