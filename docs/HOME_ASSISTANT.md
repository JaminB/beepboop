# Home Assistant integration

> Status: Draft spec · 2026-11-01 · Applies to: M0, v0.1, v0.2

beepboop runs Home Assistant Core as a sidecar container and talks to it over **one persistent WebSocket** owned by core (`core/beepboop/ha/`). HA is the integration backbone (devices, areas, Music Assistant, OAuth flows), but it has no per-person permissions, so **beepboop is the only enforcement point**: every call passes through the policy wrapper, resources are keyed by entity-registry UUID, and HA itself is reachable only on loopback. Users never see HA; beepboop onboards it headlessly on first boot.

Related: [ARCHITECTURE](ARCHITECTURE.md) · [IDENTITY_AND_TRUST](IDENTITY_AND_TRUST.md) · [MODULES](MODULES.md) · [MCP](MCP.md) · [OPERATIONS](OPERATIONS.md) · [WEB_UI](WEB_UI.md) · [DATA_AND_PRIVACY](DATA_AND_PRIVACY.md) · [ADR 0001](adr/0001-home-assistant-backbone.md) · [ADR 0005](adr/0005-risk-tiers-and-grants.md) · [ADR 0013](adr/0013-https-tiers.md) · [GLOSSARY](GLOSSARY.md)

---

## 1. Why Home Assistant

| We need | HA gives us | We add |
|---|---|---|
| Thousands of device integrations, Zigbee/Z-Wave/Matter, discovery | Yes, maintained monthly by a large community | Nothing |
| Entity, device, area and floor registries with names and aliases | Yes, over WebSocket | A per-profile index and a fuzzy resolver |
| Action schemas and descriptions (`get_services`) | Yes | Risk tiers per domain/service |
| OAuth config flows for third-party accounts (Google, Spotify, Tesla …) | Yes, including a hosted bounce page | Ownership (`config entry → profile`) |
| Per-person permissions | **No.** HA is admin vs non-admin; non-admins see and control everything | The whole trust model: [IDENTITY_AND_TRUST](IDENTITY_AND_TRUST.md) |
| A voice-first LLM tool surface | Assist and an MCP server, both household-global | Our own registry; see §10 |

**Rule: HA first.** A native beepboop module is allowed only when HA cannot model per-person accounts cleanly (CalDAV with app passwords in v0.1, Spotify and Google in v0.2). Both kinds expose the identical module contract ([MODULES](MODULES.md)). HA Core is Apache-2.0; talking to it over its API creates no derivative work, so HA's licence does not constrain beepboop's own GPL-3.0-or-later ([LICENSE](../LICENSE)), and vice versa.

**Deployment choice.** HA Container, not HAOS: HAOS owns the whole OS and beepboop must own the Pi. We lose the Supervisor's one-click add-ons; run add-on containers (MQTT broker, Z-Wave JS, Matter server) as for any HA install, bound to loopback. The only optional beepboop compose profile in v0.1 is `music` (§12).

---

## 2. Container setup

The `homeassistant` service (image by digest, host networking, `/srv/beepboop/ha:/config` on the SSD, memory cap, `cpu_shares`, healthcheck) is defined once, in [OPERATIONS §3](OPERATIONS.md#3-compose-layout); [`deploy/compose.yaml`](../deploy/compose.yaml) is the literal file. This doc does not repeat it. The HA-specific points:

- **Host networking** is for discovery (mDNS, SSDP, Matter, Zeroconf). It is also why the loopback bind in §2.1 matters: with host networking, a port HA opens is a port on the Pi.
- **Time zone** comes from the read-only `/etc/localtime` mount, not a `TZ` variable.
- **The memory cap and `cpu_shares: 512`** make HA the first thing the OOM killer and the scheduler squeeze, before `beepboop-audio` (§2.2, [OPERATIONS §7](OPERATIONS.md#7-resource-budget)).

`beepboop-core` reaches HA at `http://127.0.0.1:8123` and `ws://127.0.0.1:8123/api/websocket` because core also uses host networking ([ARCHITECTURE §2](ARCHITECTURE.md#2-containers)). No other container, and nothing on the LAN, gets :8123.

### 2.1 Loopback bind plus nftables

Two layers, because `http.server_host` behaviour has varied across HA releases and **M0 must verify it** on the pinned image (V1).

**Layer 1: HA binds loopback.** `/srv/beepboop/ha/configuration.yaml` carries a block marked `# managed by beepboop` so upgrades and admins know not to hand-edit it. The template is [`deploy/homeassistant/configuration.yaml`](../deploy/homeassistant/configuration.yaml); its content is owned here:

```yaml
default_config:

http:
  server_host: 127.0.0.1          # layer 1: bind to loopback only
  use_x_forwarded_for: true
  trusted_proxies: [127.0.0.1]    # Caddy (OAuth callback, admin route) sits on loopback

homeassistant:
  internal_url: https://beepboop.local   # rendered from BB_HOST: https://$BB_HOST (see below)
  external_url: https://beepboop.local

recorder:
  purge_keep_days: 3               # history lives in beepboop's activity timeline, not HA
  commit_interval: 30              # batch writes: SSD longevity and fewer fsyncs under load
  auto_purge: true
  exclude:
    domains: [update, sun, persistent_notification]
    entity_globs: ["sensor.*_rssi", "sensor.*_signal_strength", "sensor.*_uptime", "sensor.*_last_seen"]

logger:
  default: warning
```

**The canonical URLs are set here and only here** (hard rule 10). `install.sh` renders the block from the template, replacing `https://beepboop.local` with `https://$BB_HOST` from `deploy/.env` ([OPERATIONS §5](OPERATIONS.md#5-install-script-and-the-host-cli), step 3), and re-renders it when the host or tier changes. Onboarding (§3) does not set them over the WebSocket: with the URLs in YAML, HA treats them as YAML-owned anyway.

**Layer 2: the host firewall.** The nftables ruleset is owned by [OPERATIONS §2 step 8](OPERATIONS.md#2-os-preparation) and implemented in [`deploy/nftables/beepboop.nft`](../deploy/nftables/beepboop.nft): inbound default-deny. The HA-specific part is one rule: `tcp dport 8123 drop` comes **before** the established-state and allowlist rules, right after loopback, so no allowlist entry or established flow can re-open :8123 from the LAN.

**Discovery replies are blocked by default.** Host networking lets HA *send* discovery traffic, but the default-deny input chain drops the unicast replies to SSDP/UPnP searches and inbound HomeKit (HAP) connections, so integrations that rely on them (some UPnP/DLNA devices, HomeKit Controller pairing, HomeKit Bridge) will not find or reach devices. mDNS (`5353/udp`) is allowed. A household that needs one of these declares the port under `user:` in `deploy/ports.yaml` with a reason ([OPERATIONS §2 step 8](OPERATIONS.md#2-os-preparation)). This is an M0 watch item: V1 records which integrations in the test household break.

**How it is checked.** `beepboop doctor` checks each layer on the Pi itself: the bind (`ss -ltn` shows `127.0.0.1:8123` and no wildcard or LAN address on :8123) and the ruleset (`nft list table inet beepboop` is loaded and contains the `8123` drop before the allowlist). It deliberately does **not** connect to `<lan-ip>:8123` from the Pi: a connection from the host to its own LAN address goes through `lo`, which is accepted, so a self-connect can never observe the drop. End-to-end verification needs a second machine on the LAN; that is V1 ([tools/probes/ha_bind/](../tools/probes/ha_bind/README.md)).

### 2.2 Resource tuning for a Pi 4

- Recorder: `purge_keep_days: 3` and the excludes above. HA's recorder is the main SD-card killer, which is one reason the SSD is required.
- `mem_limit: 1g` and `cpu_shares: 512` so a busy HA cannot starve `beepboop-audio` (cpuset 2–3, nice −5). Expected RSS 300–600 MB with a modest integration set (estimate, M0 measures it).
- No HA add-ons, no HA frontend use by members. The frontend's WebSocket traffic is the thing that bloats HA on small boards.

---

## 3. Headless onboarding

The user never sees HA's wizard. On first boot `beepboop-core` waits for HA to answer and runs the onboarding API itself. The resulting admin identity is a non-human user **`beepboop-system`**. Its password is random, stored encrypted, and exists only as break-glass access for admins (§14).

```mermaid
sequenceDiagram
  participant C as beepboop-core (ha client)
  participant H as Home Assistant :8123 (loopback)
  C->>H: GET /api/onboarding
  H-->>C: [{step:user,done:false},{step:core_config,…},{step:analytics,…},{step:integration,…}]
  C->>H: POST /api/onboarding/users {client_id, name:"beepboop", username:"beepboop-system", password:<random 32 bytes>, language}
  H-->>C: {auth_code}
  C->>H: POST /auth/token grant_type=authorization_code&code=…&client_id=…
  H-->>C: {access_token, refresh_token}
  C->>H: POST /api/onboarding/core_config  (Bearer access_token)
  C->>H: POST /api/onboarding/analytics     (opt-out; beepboop never enables HA analytics)
  C->>H: POST /api/onboarding/integration   {client_id, redirect_uri}  (completes onboarding; result unused)
  C->>H: WS auth {access_token}
  C->>H: WS auth/long_lived_access_token {client_name:"beepboop-core", lifespan:3650}
  H-->>C: <long-lived token>
  Note over C: store LLAT in secrets(owner_module="core") (the owner password was stored before the users step)
  C->>H: POST /auth/token action=revoke&token=<onboarding refresh token>
  C->>H: GET /api/onboarding  (every step must report done)
```

Rules:
- **The sequence is the four HA steps in HA's order** (`user`, `core_config`, `analytics`, `integration`), as read from HA 2026.10.3's source and replayed by the fake in `core/tests/ha/`. V2 records the live result against the pinned image ([verify_v2.md](../tools/probes/ha_onboarding/verify_v2.md)).
- `client_id` is beepboop's canonical URL (`https://beepboop.local/` at Tier 0; HA canonicalises it with a trailing `/`). HA requires it to be a URL. The `integration` step's `redirect_uri` must have the **same scheme and host** as `client_id` (HA's IndieAuth check); beepboop sends `<client_id>?auth_callback=1`.
- **The onboarding refresh token is revoked, not just forgotten.** HA keeps a refresh token server-side until it is revoked, so once the long-lived token is stored, core posts `action=revoke` with the onboarding refresh token to `/auth/token`; the onboarding access token dies with it. Revocation is best-effort (HA answers 200 either way); a failure is logged, never retried in a loop.
- Canonical URLs are **not** set during onboarding; they live only in the managed YAML block (§2.1).
- The long-lived token is created on a session authenticated by the onboarding login (refresh-token session). The research note that LLAT creation may not work on a connection authenticated *with* an LLAT is why we do it here, once, and never need to again. **M0 verifies** this and the 10-year lifespan.
- Both secrets are envelope-encrypted rows in `secrets` ([DATA_AND_PRIVACY](DATA_AND_PRIVACY.md)). Only the policy wrapper (§8) decrypts the token; it never appears in logs, `ToolContext`, the UI or any API response.
- If HA is already onboarded (restore from backup, or an existing `/config` volume), beepboop detects `done: true` on the `user` step and asks the admin, in the web UI, for an owner login once to mint its token. Health shows `ha: needs login` until then.
- If the token is later rejected (`auth_invalid` on WS auth), health shows `ha: reauth`, voice says the `reauth` system phrase ([VOICE_PIPELINE §11.6](VOICE_PIPELINE.md#116-system-phrases-normative)), and the admin re-runs the login step. Nothing is retried in a loop against a 401.

`deploy/install.sh` and `beepboop doctor` share the same steps so a fresh Pi and a broken one are diagnosed by the same code.

---

## 4. The WebSocket client

One client, one connection, in `core/beepboop/ha/client.py`; the state cache (§5) and the registry index (§6) live beside it in `core/beepboop/ha/`. Nothing else in the repo opens a socket to HA. Modules get `ctx.ha`, the **policy-wrapped** handle (§8), never this class.

```python
class HAConnection:
    """Single persistent connection. Reconnects with backoff; resyncs fully after reconnect."""
    async def connect(self) -> None            # auth, supported_features, initial sync, subscriptions
    async def call(self, type: str, **payload) -> Any      # request/response by id, 5 s default timeout
    async def call_service(self, domain, service, data, target, *, return_response=False, timeout=5.0) -> ServiceResult
    def on(self, event_type: str, handler)     # state_changed, *_registry_updated, config_entry_*
    @property
    def connected(self) -> bool
```

Startup sequence and the messages involved:

| # | Message | Why |
|---|---|---|
| 1 | `{"type":"auth","access_token":…}` → `auth_ok` | Token from `secrets` |
| 2 | `supported_features {"features":{"coalesce_messages":1}}` | HA batches event bursts into one frame |
| 3 | `get_config` | Version (for the contract-test matrix), unit system, location |
| 4 | `config/entity_registry/list`, `config/device_registry/list`, `config/area_registry/list`, `config/floor_registry/list` | Build the registry index (§6) and refresh `ha_entities` |
| 5 | `config_entries/get` | Which config entries exist, their domain, title and state (`loaded`, `setup_error`, `setup_retry`, needs `reauth`) |
| 6 | `get_states` | Seed the state cache (§5) |
| 7 | `subscribe_events` for `state_changed`, `entity_registry_updated`, `device_registry_updated`, `area_registry_updated` | Incremental updates |
| 8 | `config_entries/subscribe` | Entry added / removed / state changed (reauth notices, new-device inbox) |
| 9 | `get_services` once, then on `service_registered`/`service_removed` | Validate service names and build the allowlist view (§9) |
| 10 | `ping` every 30 s; reconnect if 2 misses | Liveness |

Reconnect: exponential backoff 1 s → 30 s, jittered. After `auth_ok`, steps 3–9 run again and the cache is replaced wholesale (no diffing; a few hundred entities is cheap). While disconnected, `home.state` answers from the stale cache with `stale: true` and `home.*` actions fail fast with `HA_UNAVAILABLE`, which the voice layer turns into "I can't reach the home right now" inside the audible-feedback bound ([VOICE_PIPELINE](VOICE_PIPELINE.md)).

`call_service` always sets `target` (entity ids resolved by us) and never passes model-supplied `entity_id` strings through. It uses `return_response: true` only for services whose `get_services` schema declares `response`.

---

## 5. State cache

In-memory dict `entity_id → State{state, attributes, last_changed, last_updated, context}` fed by `get_states` then `state_changed`. Not persisted; HA is the source of truth. The cache exists so that:

- `home.state` is a dictionary lookup (0 ms), not a round trip.
- The resolver can filter by current state ("turn off the lights that are on").
- `home://context` (the MCP resource, [MCP](MCP.md)) renders a profile-scoped snapshot in one pass.

Snapshot format for `home://context` and for the LLM when it asks (`home.state` with no args), grouped by area, only entities the principal is granted, capped at ~600 tokens:

```
Living room: Ceiling light on 60%; Floor lamp off; TV playing (Jellyfin); Temperature 21.5°C
Kitchen: Lights off; Dishwasher running (0:42 left)
Front door: locked
```

Attributes are whitelisted per domain (`brightness`, `color_temp_kelvin`, `temperature`, `current_temperature`, `hvac_mode`, `media_title`, `volume_level`, `battery_level`, `position`). Everything else stays in the cache but is not rendered.

---

## 6. Registries and the resolver

### 6.1 Registry index

Built from steps 4–5 above, refreshed on registry events, and mirrored into the `ha_entities` table ([DATA_AND_PRIVACY §6.1](DATA_AND_PRIVACY.md#61-v01-tables-0001_initsql)) whose `status` column is the new-device inbox. Per entity:

```python
@dataclass(frozen=True)
class EntityRef:
    uuid: str            # entity registry `id`; the key for grants and audit
    entity_id: str       # current, may change
    domain: str
    name: str            # friendly name (registry name > original_name > state attribute)
    aliases: tuple[str, ...]   # registry aliases (what Assist uses too)
    area_id: str | None  # entity area, else device area
    floor_id: str | None
    device_id: str | None
    config_entry_id: str | None
    device_class: str | None
    hidden: bool; disabled: bool
```

Plus `areas: {area_id → Area{name, aliases, floor_id}}`, `devices`, `floors`. Entities with `disabled` or `hidden`, or with `ha_entities.status != 'active'`, are indexed but never resolvable.

### 6.2 Per-profile view

`index.for(principal) -> ProfileIndex` returns the subset of `EntityRef`s the principal may *see* (any grant at `read` or above). It is computed from grants (§7) and memoized until grants or the registry change. The same list feeds:

- the resolver (below),
- STT hotwords for that profile (entity, area and alias names → `profiles.sync`, [VOICE_PIPELINE](VOICE_PIPELINE.md)),
- `home://context`,
- the web UI "what can beepboop do for me" page.

### 6.3 Resolver

`resolve(principal, query: TargetQuery) -> Resolution` is a **server-side** function in `core/beepboop/registry/resolver.py`, used by `home.turn_on`, `home.turn_off`, `home.set`, `home.state`, `media.control`, and the fast path ([LLM_PROVIDERS §12](LLM_PROVIDERS.md#12-the-deterministic-fast-path)). It is generic over an `EntityIndex`; `core/beepboop/ha/` supplies the HA-backed index (§6.1). The model never sees entity ids; it passes words.

```python
@dataclass
class TargetQuery:
    name: str | None = None      # "kitchen lights", "the lamp", "tv"
    area: str | None = None      # "kitchen"
    domain: str | None = None    # "light" (tool implies it where possible)
    device_class: str | None = None
    state_filter: str | None = None   # "on", "off", "playing"

@dataclass
class Resolution:
    status: Literal["ok", "ambiguous", "none", "denied"]
    targets: list[EntityRef]          # ok: 1..n (an area query legitimately returns many)
    candidates: list[tuple[EntityRef, float]]   # ambiguous: top 3 for the clarifying question
    denied: list[EntityRef]           # matched but not granted; counted, never named to Guests
    confidence: float
```

Algorithm (pure, deterministic, property-tested with `hypothesis` plus a fixture household in `core/beepboop/ha/tests/fixtures/household.json`):

1. **Scope.** Start from `ProfileIndex`. Apply `domain`, `device_class`, `state_filter`.
2. **Area.** If `area` is given, fuzzy-match it against area and floor names and aliases (threshold 85/100). If `name` itself contains an area word ("kitchen lights"), split it: area = kitchen, name = lights. A satellite's own area (v0.3) is the default area when none is spoken.
3. **Name.** Normalise (lower, strip articles, singularise common nouns, "lights"→"light"). Score each candidate with `max(token_set_ratio(name, entity.name), max over aliases)` using rapidfuzz. Also accept **domain nouns**: "lights" with no further name matches every `light.*` in the area; "the tv" matches `media_player` with device_class tv.
4. **Decide.** Collect candidates with score ≥ 80. If the best is ≥ 90 and either unique or all ties share the same area and domain → `ok`. If several distinct devices score within 10 points of each other and the query is singular → `ambiguous`. Nothing ≥ 80 → `none`.
5. **Deny check.** Run the same match against the full index minus the profile view. If that would have matched and the profile view did not → `denied` (so the voice can say `not_granted` instead of "I don't know that device", except for Guests, who get `none`).

Fast-path use requires `status == ok` with `confidence ≥ 0.9` (score/100); anything else goes to the LLM, which can ask the clarifying question from `candidates`.

---

## 7. Mapping HA to profiles

HA has no owner concept, so ownership is beepboop's. There is exactly one mechanism, grants ([IDENTITY_AND_TRUST](IDENTITY_AND_TRUST.md)), with four HA resource kinds:

| `resource_type` | `resource_id` | Typical use |
|---|---|---|
| `ha_entity` | entity-registry UUID | "Alex can lock the front door" |
| `ha_area` | area id | "household can control everything in the living room" |
| `ha_config_entry` | config entry id | personal accounts: "Alex's Spotify", "Sam's Google Calendar" |
| `tool_glob` | `home.*`, `media.control` … | which *verbs* the profile may use at all |

Effective entities = union over grants, capped by the profile's `risk_ceiling`. Grants are by **UUID**, so `entity_id` renames do not break anything. A deleted entity's grants are kept for 30 days (the entity may come back after an integration reload) and then purged.

### 7.1 Shared devices

Lights, climate, locks, media players in common rooms: the admin grants `ha_area` or `ha_entity` to the `household` group or to individual profiles, with `max_risk`. Presets are defined in [IDENTITY_AND_TRUST §9](IDENTITY_AND_TRUST.md#9-presets): Admin and Member have a `critical` ceiling; which critical entities a member can use is decided per entity in the inbox (default Adults); Child and Guest are capped at `act`. Nothing here is special-cased.

### 7.2 Personal accounts via HA config entries

When a profile connects a personal HA integration (v0.2 generic flow renderer; in v0.1 only Music Assistant providers are mapped, through the music module's `ma_provider` setting, §12):

1. beepboop starts the config flow **on behalf of that profile** and records `integrations(module_id="ha", scope="profile", profile_id, ha_config_entry_id)`.
2. On `config_entries/subscribe` → `added`, it creates one grant: `(profile, ha_config_entry, <entry_id>, max_risk=personal, source=auto:config_entry)`.
3. Every entity whose `config_entry_id` equals that entry is therefore visible to that profile only. New entities the integration adds later inherit the grant automatically; they do **not** land in the inbox.
4. If the entry enters `reauth` or `setup_error`, the profile sees "Reconnect Spotify" in the UI and the voice reply is the `reauth` system phrase. Only that profile and admins are told.
5. Deleting the profile cascades: the grant goes, the `integrations` row goes, and beepboop removes the HA config entry (`config_entries/delete`) so the account's tokens leave the Pi too.

Provider-specific rules (Spotify's developer-portal and account limits, for example) live in that module's `SETUP.md`, not here.

---

## 8. The policy-wrapped client (`ctx.ha`)

Modules never touch `HAConnection`. They receive `ctx.ha`, the module-facing `HAClient` Protocol defined in [MODULES §5.3](MODULES.md#53-toolcontext) (`resolve`, `state`, `states`, `call_service`), implemented by `core/beepboop/policy/ha_wrapper.py`, the **only holder of the HA token**. The `ha` module itself contains only the `home.*` and `media.*` tool handlers and goes through the same wrapper.

`call()` does, in order and deterministically:

1. **Service allowlist lookup** (§9) → tier, or `PolicyDenied("service_blocked")` for the blocked class.
2. **Every target must be in the principal's granted view at ≥ that tier**, else `PolicyDenied`.
3. **`call_service`.**

Authorization and audit are not the wrapper's job: `registry.call()` owns `policy.authorize()` and the `tool_calls` row ([MCP §1](MCP.md#1-one-registry-two-front-doors)); the wrapper is the second guard against a module building its own target list. The raw client is private to `core/beepboop/ha/`; `beepboop module check` fails any module that imports `beepboop.ha`.

Per-profile HA users for logbook attribution ("Alex" instead of "beepboop-system" in HA's history) are v0.3+ ([ROADMAP](ROADMAP.md)); they would be attribution, never security.

---

## 9. Service allowlist and risk tiers

The single source is `core/beepboop/policy/ha_services.yaml`; this section renders it. Every HA service falls into one of three classes: **direct** (entity services with a tier from `(domain, service, device_class)`), **indirect** (entity-keyed, default `critical`, dispositioned in the inbox), or **blocked** (never callable). Everything not listed is treated as blocked with `SERVICE_NOT_ALLOWED`, which is logged, so the admin can see what the model tried. [IDENTITY_AND_TRUST §6.3](IDENTITY_AND_TRUST.md#63-ha-service-classes) links here.

**Direct**

| Domain / service | Tier | Notes |
|---|---|---|
| `light.*`, `switch.*` (non-template platform), `fan.*`, `humidifier.*`, `media_player.*` `turn_on` / `turn_off` / `toggle` | `act` | |
| `light.turn_on` with `brightness_pct`, `color_temp_kelvin`, `rgb_color` | `act` | via `home.set` |
| `climate.set_temperature`, `set_hvac_mode`, `set_preset_mode` | `act` | step clamped to ±5 °C per call |
| `cover.open_cover` / `close_cover` / `set_cover_position` | `act`, **`critical` if `device_class` in {garage, gate, door}** | device_class comes from the registry, not the model |
| `media_player.*` (play, pause, stop, next, previous, volume_set, volume_mute, select_source, play_media) | `act` | `media.control`; `volume_set` clamped ≤ 0.7 for Child |
| `music_assistant.play_media`, `search`, `get_queue` | `act` / `read` | music module |
| `lock.lock` | `act` | locking is safe |
| `lock.unlock`, `lock.open` | `critical` | always second factor |
| `alarm_control_panel.alarm_arm_*` | `act` | |
| `alarm_control_panel.alarm_disarm` | `critical` | |
| `vacuum.start`, `return_to_base`, `stop` | `act` | |
| `siren.*` | `critical` | |
| `timer.*` (HA timers) | not used | beepboop's `timers` module owns timers so they are per-profile and survive restarts |

**Indirect** (entity-keyed; default `declared_risk = critical`; denied until dispositioned in the inbox)

| Domain / service | Why indirect |
|---|---|
| `script.*`, `scene.turn_on`, `automation.trigger`, `automation.turn_on/off` | A scene called "Away" can unlock a door; a script can do anything |
| `input_boolean.*`, `input_select.*`, `input_number.*`, `input_button.*` | Helpers whose only purpose is to drive automations |
| `button.press`, `select.*` | Device buttons and selects commonly trigger vendor-side actions |
| `switch.*` where the entity's platform is `template` | A template switch is an automation in disguise |

The grant is on the script/scene/helper *entity* UUID; the tier the admin chose is stored as `ha_entities.declared_risk` and the grant's `max_risk`. No model-visible tool exists for them beyond `home.turn_on` on a granted entity.

**Blocked** (never callable through any beepboop tool, for any principal; the wrapper raises `PolicyDenied("service_blocked")`)

| Domain / service | Why |
|---|---|
| `homeassistant.*` (`turn_on`, `turn_off`, `toggle`, `reload_*`, `restart`, `stop`, `update_entity`) | reaches any entity, or the process |
| `rest_command.*`, `shell_command.*`, `python_script.*`, `hassio.*` | arbitrary side effects |
| `notify.*` | not exposed to tools in v0.1; core may use it itself for push confirm in v0.2; outbound messaging tools (v0.3) will be `critical` |
| `persistent_notification.*`, `system_log.*`, `logger.*`, `recorder.*` | admin tooling |

Lists are native (`list.*` on `ctx.kv`), so there is no `todo.*` row.

**Risk chip rule.** Risk can be raised; it can be lowered only for indirect services, after the admin reads what the entity does (the inbox shows the script or scene body). A direct service's tier is never editable.

Service existence and argument shapes are validated against `get_services` at call time, so a typo in the YAML surfaces as a contract-test failure, not a runtime 400.

---

## 10. What we do not use: HA's MCP server and Assist

- **HA's `mcp_server` (`/api/mcp`)** exposes the tools of HA's LLM APIs to one household-global exposure list, defaults to `require_admin`, and has no per-user scoping. It cannot express "Alex's calendar, not Sam's", and it would put HA's own prompt text in front of the model instead of our `AGENT.md`s. It stays disabled, and `beepboop doctor` warns if the integration is loaded.
- **Assist / `conversation.process`** acts immediately on a global exposure list, so its intent matcher cannot be used as a pre-check without a parallel ACL pass on its returned targets. We get the same latency win from our own deterministic fast path, which runs *through* the resolver and policy ([LLM_PROVIDERS §12](LLM_PROVIDERS.md#12-the-deterministic-fast-path)). Assist's multilingual sentence corpus is a good reference for our grammar files.
- **HA's `mcp` client integration** (HA consuming MCP servers) is irrelevant: beepboop is the agent.

**v0.3+ candidate: `beepboop_bridge`**, an HA custom component registering an `llm.API` backed by beepboop's registry and policy, so HA's own Assist satellites respect beepboop profiles. Separate repo, because it tracks HA's internal API ([ROADMAP](ROADMAP.md)); its licence is an open choice for v0.3 (code that loads into HA's process and would go upstream may need to be Apache-2.0 rather than GPL).

---

## 11. OAuth for HA integrations through Caddy

HA-backed integrations (Google, Spotify, Tesla …) use HA's own OAuth flow. The problem: the provider redirects to `https://my.home-assistant.io/redirect/oauth`, whose page sends the browser to the `redirect_uri` carried in the signed `state`, i.e. *HA's* URL. HA is on loopback, so that URL must be beepboop's, and Caddy must forward exactly one path.

```
# deploy/caddy/Caddyfile (Tier 0 shown; Tier 1 swaps the site address and tls directive)
{$BB_HOST} {
  tls internal

  handle /auth/external/callback* {    # the ONLY HA path exposed; HA verifies the HS256 state JWT
    reverse_proxy 127.0.0.1:8123
  }
  handle {
    reverse_proxy 127.0.0.1:8080        # beepboop-core: API, SPA, /mcp
  }
}
```

In v0.1 only the Music Assistant flow runs through this (no OAuth). The generic config-flow renderer for OAuth-backed HA integrations is v0.2 ([ROADMAP](ROADMAP.md)); the invariants that bind v0.1 are:

- `internal_url` and `external_url` equal beepboop's canonical URL (§3), because HA builds `redirect_uri` from them.
- The callback path never requires a beepboop session: HA's signed `state` is the protection, and the browser may lack our cookie. Everything else behind Caddy requires a session.
- beepboop starts every flow on behalf of one profile and, on `entry added`, writes the `integrations` row and the `ha_config_entry` grant (§7.2).
- Providers without HA Cloud credentials need bring-your-own application credentials; the redirect URI registered at the provider is always `https://my.home-assistant.io/redirect/oauth`. Portal walkthroughs live in each module's `SETUP.md`.
- Native modules (Spotify, Google, v0.2) use beepboop's own bounce page instead: [MODULES §8](MODULES.md#8-oauth-v02-invariants-that-bind-v01).

---

## 12. Music Assistant path (music module, v0.1)

Music Assistant (MA) runs as the optional compose profile `music` (service `music`, defined in [OPERATIONS §3](OPERATIONS.md#3-compose-layout); host networking for player discovery: AirPlay, Chromecast, Sonos, Snapcast) and is integrated into HA with the core `music_assistant` integration. beepboop's `music` module is `kind: ha`: it has no transport of its own and uses `ctx.ha`.

Setup, driven by **Integrations › Music › Set up** in the web UI: (1) the installer enables the `music` profile; (2) core runs `config_entries/flow {handler:"music_assistant", url:"http://127.0.0.1:8095"}` over the WebSocket; (3) MA players appear as `media_player.*` entities tagged with that config entry; the admin grants them by area like any device (they are shared playback hardware). Streaming provider accounts (Spotify, Tidal, local library) are configured inside MA's UI, reached through the same gated admin route as HA (§14, port 8445 → 8095). The music module's `ma_provider` setting is `x-per-profile` ([MODULES §6.3](MODULES.md#63-per-profile-overrides)): each person picks which MA provider instance is theirs, so "play my liked songs" uses Alex's account when Alex is speaking. It is a setting, not an `integrations` row. Services used: `music_assistant.play_media` (`media_id`, `media_type`, `enqueue`), `music_assistant.search` (`return_response`), `media_player.*` for transport. Details, prompts and quirks are in `modules/music/AGENT.md`.

---

## 13. Upgrade strategy and bridge contract tests

HA ships monthly with breaking changes (2026.10 removed eight integrations). Three defences:

1. **Pin by digest.** The HA digest is pinned in one place, the deploy `.env` ([OPERATIONS §3](OPERATIONS.md#3-compose-layout)). Renovate-style PRs bump it; CI must pass before merge.
2. **Bridge contract tests** in `core/beepboop/ha/tests/contract/`, run in CI against the pinned image (amd64 build of the same tag, with HA's `demo` integration) and nightly against `latest`. They assert the exact surface we depend on:
   - onboarding endpoints and payloads (§3), including `auth/long_lived_access_token` on a login session;
   - `server_host: 127.0.0.1` actually binds loopback;
   - every WebSocket command in §4 answers with the fields in `EntityRef`, `Area`, `State`;
   - event payload shapes for `state_changed`, `entity_registry_updated` (`action: create|update|remove`, `old_entity_id` on rename), `config_entries/subscribe`;
   - `call_service` with `target` and `return_response`;
   - every service in `ha_services.yaml` exists in `get_services` with the argument names we send;
   - the config flow for `music_assistant` and a representative OAuth `external` step (mocked provider).
   A nightly failure opens an issue labelled `ha-upstream`; it does not block users because they are on the pinned digest.
3. **Staged upgrade with rollback** via `beepboop update` (v0.1); a UI button comes in v0.2 ([OPERATIONS §9](OPERATIONS.md#9-updates)). Never silent: snapshot beepboop's SQLite (`VACUUM INTO`) and `/srv/beepboop/ha/`, pull the new digest, restart HA, wait for health, run the **read-only subset** of the contract tests against the live instance (no `call_service`), compare the entity registry (UUIDs unchanged, count within ±5 %). Any failure restores the snapshot and the previous digest automatically and tells the admin what failed. HA's own `.storage` migrations are one-way, which is why the snapshot comes first.

---

## 14. Admin access to HA's UI

Admins sometimes need HA's frontend (pairing a Zigbee device, debugging an integration). HA does not run under a URL path prefix, so the gated route is a second site on the same host, in the same form as [OPERATIONS §4](OPERATIONS.md#4-caddy):

```
{$BB_HOST}:8443 {
  tls internal
  forward_auth 127.0.0.1:8080 {
    uri /api/auth/check?require=admin      # 200 if the beepboop session cookie belongs to an admin, else 401
    copy_headers X-BB-Profile
  }
  reverse_proxy 127.0.0.1:8123 {             # WebSocket upgrades pass through
    header_up -Cookie                        # beepboop's session cookie never reaches HA
  }
}
{$BB_HOST}:8445 {                            # same pattern for Music Assistant's UI when the `music` profile is enabled
  tls internal
  forward_auth 127.0.0.1:8080 {
    uri /api/auth/check?require=admin
    copy_headers X-BB-Profile
  }
  reverse_proxy 127.0.0.1:8095 {
    header_up -Cookie
  }
}
```

Reaching HA's UI is one tap: **Open Home Assistant** on Integrations › Home Assistant. **HA does ask for its own login on first use**: the gate above proves you are a beepboop admin, but HA keeps its own users, and beepboop deliberately does not create one for you. The owner password reveal lives in Integrations › Home Assistant › **Advanced** › "Reveal owner password": it re-authenticates with password + TOTP, writes an audit row, and shows the `beepboop-system` password once. Admins who want their own HA login can create one from HA's UI afterwards; beepboop does not manage it. Members and children never see this page; their only HA is beepboop.

---

## 15. Trademark and wording

"Home Assistant" is a trademark of the Open Home Foundation. Apache-2.0 §6 grants no trademark rights. The repo, README, UI copy and this doc say **"works with Home Assistant"**; beepboop is never described as a Home Assistant product, distribution or flavour, and no HA logo is bundled. The optional future `beepboop_bridge` component follows the same rule. A `NOTICE` file lists HA and every other upstream we ship or download ([OPERATIONS §13](OPERATIONS.md#13-model-and-licensing-hygiene)).

---

## 16. M0 checklist (HA part)

- [ ] Pinned image boots on Pi OS Lite 64-bit with the `homeassistant` service from [OPERATIONS §3](OPERATIONS.md#3-compose-layout); RSS and CPU recorded with the recorder settings applied.
- [ ] `server_host: 127.0.0.1` verified from a second LAN host; nftables rule verified by disabling layer 1 (V1). Record which discovery-based integrations the default-deny chain breaks (§2.1).
- [ ] Headless onboarding (§3) runs end to end on a fresh volume; LLAT minted; `GET /api/onboarding` reports all steps done.
- [ ] WebSocket startup (§4) under 2 s on a 200-entity demo household; `call_service` round trip p50/p90 measured over loopback (target: 40 ms p50).
- [ ] Resolver fixture suite passes; `beepboop bench` includes "resolve + authorize" (target: 20 ms).
- [ ] Contract tests green against the pinned digest.

## 17. Open questions

1. **[v0.1 ruling] Admin route ports.** :8443 and :8445 are arbitrary; the alternative is extra hostnames (`ha.beepboop.local`), which need mDNS aliases that Avahi does not publish by default. Confirm with [WEB_UI](WEB_UI.md).
