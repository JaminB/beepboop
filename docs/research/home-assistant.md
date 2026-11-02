# Home Assistant as beepboop's integration backbone: research report (2026-11-01)

## 1. Options compared

### 1a. How beepboop talks to HA

| Option | What it gives | Per-user scoping | Latency on Pi 4 (local, est.) | Maturity | Verdict |
|---|---|---|---|---|---|
| **WebSocket API** (`/api/websocket`) | `auth`, `get_states`, `get_services` (action schemas and descriptions), `get_config`, `call_service` with `return_response`, `subscribe_events`, `subscribe_trigger` (admin), `ping`, `supported_features` → `coalesce_messages`. Registry commands: `config/entity_registry/list` and `list_for_display`, device, area and floor registries, `homeassistant/expose_entity[/list]` (admin), config-entry flows, application credentials. The HA frontend uses this API itself. | None from HA. beepboop has to enforce its own ACL. | Tens of ms per call is typical over loopback. I found no published benchmark, so measure it. | Very high. It is the frontend's own API and stable. | **Primary transport** |
| **REST API** (`/api/...`) | `GET /api/states`, `POST /api/services/<d>/<s>?return_response`, `/api/template` (admin), `/api/conversation/process`, `/api/events` (admin). HA docs say REST is for one-off requests and new code should use WebSocket. | None | Similar per call, plus HTTP overhead. No push. | High | Fallback, health checks only |
| **HA "Model Context Protocol Server"** (`/api/mcp`, since 2025.2; 2026.10 added the Settings > System > AI card and per-API endpoints such as `/api/mcp/assist`) | Exposes the tools of the selected HA LLM APIs. The Assist API offers intent tools (`HassTurnOn/Off`, `HassLightSet`, climate, media, timers, `GetLiveContext`, `GetDateTime`) plus any integration `llm.py` tools. Prompts are supported. The resource `homeassistant://assist/context-snapshot` exists for Assist only. No sampling or notifications. Transport is Streamable HTTP with OAuth 2.0 PKCE. `require_admin` and `all_llm_apis` both default to on. | **None.** Only entities exposed to the global "conversation" assistant are visible, and that exposure is one household-wide list. | Extra MCP and LLM-API indirection on every call | Medium. Reportedly still pinned to MCP Python SDK 1.28.1 (protocol 2025-11-25). | Do not wrap it as the backbone |
| **HA "MCP" client integration** | HA consumes external MCP servers (SSE only, tools only) and gives them to HA's conversation agents | n/a | n/a | Medium | Not relevant. beepboop is the agent, not HA. |
| **Conversation/Assist API** (`conversation/process`) | Text goes in. The response carries `response_type`, `speech`, success/failed targets, `continue_conversation` and `conversation_id`. Accepts `device_id` and `satellite_id`. Error codes are `no_intent_match`, `no_valid_targets`, `failed_to_handle` and `unknown`. | None (global exposure) | Built-in intent matching is fast, roughly tens to low hundreds of ms | High | Optional fast path, see section 4 |
| **Community `ha-mcp`** (HACS, 95+ tools) | Admin-level management of HA | None | n/a | Fast-moving, with breaking changes (v7.3.0) | Reference only |
| **Custom HA integration "beepboop_bridge"** inside HA (`llm.API` / `llm.py`) | Runs inside HA. `llm_context` carries `context.user`, `device_id` and `assistant`. | Possible to build (per-user filter) | In-process | Ties beepboop's release cadence to HA's internal Python APIs | Defer, possible v2 |

### 1b. Deployment of HA on the Pi 4

| | HA Container (docker compose sidecar) | HAOS |
|---|---|---|
| Fits "beepboop owns the Pi" | Yes | No. HAOS owns the whole OS. |
| Supervisor / Apps (formerly add-ons) | **Lost.** No one-click Mosquitto, Z-Wave JS UI, Matter Server, OTBR, Music Assistant, Whisper/Piper, or managed backups/updates. | Yes |
| Install-type support | Officially supported, Docker only | Officially supported |
| Hardware floor | HA's official floor is 2 GB RAM and a 32 GB A2 SD card. Use **4 GB minimum and 8 GB recommended** alongside wake word, speaker ID and STT. Boot from SSD (SD wear from the recorder database). | Same |
| Arch | Use 64-bit Pi OS (aarch64). Treat 32-bit as unsupported. | aarch64 |

You can rebuild the missing Apps by hand as extra compose services (`eclipse-mosquitto`, `zwave-js-ui`, `python-matter-server`, `music-assistant`). beepboop should ship these as optional compose profiles.

**Licensing.** HA Core is Apache-2.0. Talking to it over WebSocket or REST creates no derivative-work issue, so beepboop stays MIT. If the compose file pulls the official image unmodified, redistribution is only an issue if you rebundle images. Keep a NOTICE file anyway. Apache §6 grants **no trademark rights**: say "works with Home Assistant" and never brand beepboop as HA. A future in-HA custom component is still your own code and can be MIT-licensed. Apache does not force relicensing.

## 2. Recommendation

**Run HA Container as a sidecar on the private compose network. beepboop talks to it through a single `ha_bridge` module over one persistent WebSocket connection, authenticated with a dedicated non-human "beepboop-system" admin user's long-lived access token (10-year validity). beepboop enforces all per-profile ACLs itself. Do not wrap HA's MCP server.**

Why:
1. **Per-user scoping is beepboop's core value, and HA can't provide it.** HA's model is admin vs non-admin. Permission groups exist in code but there is no entity-level policy UI, and non-admins see and can control every entity. Community RBAC add-ons (ha-rbac, user-rbac) confirm the gap, and there is no official 2026 roadmap item to close it. HA's MCP server exposes one global exposure list and defaults to admin-only, so it can't express "Alice's Spotify, not Bob's."
2. **Latency and control.** One persistent socket gives you `get_states` once, then incremental `state_changed` via `subscribe_events`, held in an in-memory state cache. Tool calls become a single `call_service` round trip. The MCP path adds a protocol hop and HA's own LLM-API prompt logic, which beepboop does not want, because beepboop's own AGENT.md should be the documentation the model sees.
3. **Better tool metadata.** `get_services` returns action field schemas, and the entity, device, area and floor registries give names, aliases and areas. beepboop can generate typed MCP tools per profile (for example `lights.set(area, brightness)` limited to that profile's allowed entities) instead of HA's generic intent tools.
4. **Modularity.** `ha_bridge` is just another beepboop module with a `manifest.yaml`, `AGENT.md`, and risk levels per domain/service (`lock.unlock` and `alarm_control_panel.alarm_disarm` are high risk). If someone wants openHAB later, it is a sibling module.

**Mapping accounts to people.** One integration can have multiple config entries for many integrations. Spotify officially supports several accounts on one developer app. Google Calendar and others create one entry per account. Config entries have **no owner field**, so beepboop stores `profile_id → [config_entry_id]`. It resolves entities through the entity registry's `config_entry_id` and grants that profile the derived `entity_id`s. Shared house devices (lights, thermostat) go to a household ACL group. Personal ones (Spotify `media_player`, calendar entities) go to the person.

**Hiding HA from users (headless onboarding).** Onboarding endpoints are open until each step is done:
1. `POST /api/onboarding/users` (name, username, password, client_id, language) returns `auth_code`. Exchange it at `/auth/token`.
2. `POST /api/onboarding/core_config`, then `/analytics` (send opt-out), then `/integration`.
3. Over WebSocket, create the long-lived token with `auth/long_lived_access_token`, called on a connection authenticated with the onboarding refresh-token session, then store it encrypted.

The beepboop installer does this on first boot with a random owner password kept in beepboop's secret store. The owner remains as break-glass access. Users never see HA's UI. Admins can get an "Open Home Assistant" escape hatch.

**OAuth for integrations from the beepboop UI.** Drive config flows over WebSocket/REST (`config_entries/flow`, `application_credentials/*`). OAuth integrations return an `external` step URL. The browser goes to the provider, then to `https://my.home-assistant.io/redirect/oauth`, which bounces to the instance URL encoded in `state`. beepboop's UI then polls or subscribes for flow progress. This works on LAN-only setups because the redirect happens in the user's browser, which is on the LAN.

## 3. Gotchas and risks specific to beepboop

- **One HA identity means no HA-side attribution.** All actions show as "beepboop-system" in HA's logbook. Mitigation: optionally create one non-admin HA user per profile (WebSocket `config/auth/create` plus `config/auth_provider/homeassistant/create`, both admin) and issue calls with that user's token, which gives correct `context.user_id` in logbook and history. Treat this purely as audit. It is **not** security, because HA won't block anything.
- **ACL bypass.** Anyone holding the system token or reaching HA's port 8123 bypasses every beepboop ACL. Keep HA on an internal-only Docker network, or bind it to 127.0.0.1 with a reverse proxy that only admins reach. Disable or ignore HA's MCP server.
- **New entities and renames.** New integrations and entities appear constantly, and entity IDs get renamed. Store ACLs by **entity registry `id` (UUID)**, not `entity_id`. Default-deny new entities until an admin assigns them. Subscribe to `entity_registry_updated` and `config_entry` changes.
- **Spotify gotchas.** Since Feb 2026 a Premium account is required for the developer portal. Each extra account must be added to the app's User Management (dev-mode user caps apply). A second account often fails unless the user logs out of Spotify or uses a private window, so say this in the UI. Spotify allows one stream per account.
- **Config-flow coverage.** Some integrations still use YAML or discovery-only setup, and some flows have multi-step forms. The beepboop UI needs a **generic data_entry_flow renderer** (HA returns a voluptuous-serialised schema), not hand-built pages per integration.
- **Token and reauth expiry.** OAuth integrations raise reauth flows. Surface `config_entry` state `setup_error` and `reauth` per profile ("Alice, reconnect Google"). The voice reply should be "your calendar needs reconnecting in the app."
- **Cloud OAuth credentials.** Home Assistant Cloud supplies default OAuth credentials for some integrations, and it is being renamed "Home Assistant Link" in December 2026. Without it, users must create their own developer apps (Google, Spotify), which is the biggest onboarding friction. Document each one in its module's AGENT.md and SETUP.md.
- **Upgrade breakage.** HA ships monthly with backward-incompatible changes (2026.10 removed 8 integrations, and users reported start failures). Pin the HA image tag in compose, run a contract test suite against the bridge, and give the admin UI a "staged upgrade with backup" step.
- **Resource contention.** HA Core plus its recorder on a Pi 4 shares CPU with STT and speaker ID. Set the recorder `purge_keep_days` low, exclude noisy entities, and CPU-pin or `nice` HA below the audio pipeline. Boot from SSD.
- **Admin-only APIs.** `subscribe_trigger`, `render_template`, firing events and exposure editing need admin, which is a reason the bridge token is admin. Contain that token carefully, encrypted at rest.

## 4. Unrequested features and design ideas

1. **Two-tier command path.** Try HA's local intent matcher (`conversation/process` with `agent_id=conversation.home_assistant`) first for simple device commands such as "kitchen lights off". Only after a beepboop-side ACL check on the returned targets, fall back to the LLM. This cuts latency well under 1.5 s and saves Bedrock cost. HA's sentence engine is multilingual and tested. One caveat: the matcher acts immediately, so either restrict it to low-risk domains or keep it in a narrow mode.
2. **Auto-generated AGENT.md for HA.** Generate per-profile tool docs from `get_services` descriptions plus registry areas and aliases, then cache them. This fits the "detailed docs for the MCP" goal without hand-writing hundreds of services.
3. **Live context resource.** Expose a beepboop MCP resource `home://context/{profile}` (a filtered state snapshot), similar to HA's `GetLiveContext` but scoped per profile. This is cheap grounding for the model.
4. **Event-driven proactive announcements.** `subscribe_events` / `subscribe_trigger` is the foundation for routines ("doorbell → announce to whoever is home"). Design the bridge's event bus now.
5. **Presence-aware profiles.** HA `person` entities give "who is home". beepboop can use them as a speaker-ID prior (narrow the candidate set) and to route announcements.
6. **HA as an output too.** Use HA `notify.mobile_app_*` for the high-risk "phone confirm" second factor. The HA Companion app already gives actionable notifications, so you don't need a beepboop mobile app in v1.
7. **Satellite path.** HA's Assist pipeline and Wyoming protocol devices (ESPHome voice satellites) could later feed audio to beepboop. Keep the audio front-end abstracted.
8. **Exportable ACL policy file** (YAML) per profile, for backup/restore and for code review of household permissions.
9. **Optional "beepboop_bridge" HA custom component (v2).** It would register an `llm.API` that reads beepboop's ACLs, so HA's own Assist/voice satellites respect beepboop profiles too.

## 5. Sources

- HA MCP Server integration: https://www.home-assistant.io/integrations/mcp_server/
- HA MCP client integration: https://www.home-assistant.io/integrations/mcp/
- WebSocket API: https://contributors.home-assistant.io/core/internals/websocket-api/
- REST API: https://contributors.home-assistant.io/core/internals/rest-api/
- Conversation API: https://contributors.home-assistant.io/core/internals/conversation-api/
- LLM API (`llm.API`, `llm.py`): https://contributors.home-assistant.io/core/internals/llm-api/
- Onboarding views source: https://github.com/home-assistant/core/blob/dev/homeassistant/components/onboarding/views.py
- Onboarding integration: https://www.home-assistant.io/components/onboarding
- Auth docs: https://www.home-assistant.io/docs/authentication/ ; Auth API: https://developers.home-assistant.io/docs/auth_api ; Permissions: https://developers.home-assistant.io/docs/en/auth_permissions.html
- Application credentials: https://www.home-assistant.io/integrations/application_credentials/ ; OAuth2 helpers: https://developers.home-assistant.io/docs/oauth2 ; redirect change: https://github.com/home-assistant/core/issues/74000
- Pi install / requirements: https://www.home-assistant.io/installation/raspberrypi/ ; https://shop.zimaspace.com/de/pages/home-assistant-hardware-requirements
- Spotify multi-account: https://www.home-assistant.io/integrations/spotify ; https://community.home-assistant.io/t/spotify-for-2-user/399269 ; https://github.com/music-assistant/support/discussions/2163
- Per-user permissions gap: https://github.com/orgs/home-assistant/discussions/1171 ; https://community.home-assistant.io/t/access-control-real-per-user-permissions-as-a-custom-integration/1022515 ; https://github.com/SamAthanas/user-rbac ; https://community.home-assistant.io/t/create-users-via-api/757875
- 2026.10 MCP card and changes (secondary sources): https://jacar.es/?p=7657 ; https://www.techzeitgeist.de/home-assistant-mit-chatgpt-oder-claude-verbinden-was-sie-freigeben/ ; changelog https://www.home-assistant.io/changelogs/core-2026.10/ ; https://www.bbox-mag.fr/divers/home-assistant-2026-10-nouveautes/47768917 ; https://community.simon42.com/t/probleme-mit-home-assistant-update-ab-version-2026-10-aktuell-2026-10-0-video-von-simon42/93738
- Community ha-mcp: https://github.com/homeassistant-ai/ha-mcp

## Not verified, check before building on these

- **From memory (not checked against a source):**
  - Long-lived token validity of 10 years.
  - The WebSocket user-creation commands (`config/auth/create`, `config/auth_provider/homeassistant/create`).
  - Whether `auth/long_lived_access_token` works on a connection authenticated with a long-lived token. I believe it needs a login-session refresh token.
  - Whether `/api/mcp` accepts long-lived bearer tokens in 2026.
- **Estimates, not measured:**
  - HA Core RAM on a Pi 4. My working figure is about 300–600 MB with a modest integration set.
  - Per-call WebSocket latency.

  Benchmark both on real hardware.
- **Secondary sources only:**
  - The 2026.10 MCP details: the Settings > System > AI card and the per-API endpoints.
  - The Home Assistant Cloud to "Home Assistant Link" rename.

  I did not find the official release blog.