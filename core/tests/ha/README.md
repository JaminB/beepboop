# core/tests/ha

Unit tests for `beepboop.ha` and for the V2 recorder (`tools/probes/ha_onboarding/record_v2.py`,
loaded by path). `conftest.py` holds `FakeHA`, an aiohttp server that mirrors HA 2026.10.3's
onboarding, `/auth/token` and WebSocket auth behaviour. Run with `uv run pytest core/tests/ha`.
The real HA surface is covered by V2 on the Pi and, from v0.1, by the bridge contract tests
(HOME_ASSISTANT §13).

v0.1 adds the WebSocket stack: `fake_ws.py` (a fake HA WebSocket: auth, `id`-matched
results, coalesced events, ping/pong, registries, `call_service`), `beepboop/testing/fixtures/ha_household.json`
(the fixture household: areas, floors, devices, entities incl. a template switch, a scene,
a script, hidden/disabled rows, states with a prompt-injection `media_title`), and `rig.py`
(database + policy snapshot + `HAService` + registry with the real `modules/ha` loaded).
`test_e2e.py` drives `registry.dispatch` end to end; `test_resolver_properties.py` holds
the Hypothesis properties (ROADMAP v0.1 #4: a new entity is unreachable until dispositioned).
