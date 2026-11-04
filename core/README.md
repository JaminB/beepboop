# core: the `beepboop` package

The core service (container `beepboop-core`): agent loop, policy middleware, tool
registry, Home Assistant client, LLM providers, API and SQLite. It talks to the audio
service through the event protocol in [`protocol/`](../protocol/README.md)
(`beepboop-protocol`), not through the audio package. The subpackage layout is in
[CLAUDE.md §3](../CLAUDE.md#3-repository-layout); the design is
[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md). This file is the **v0.1 contract map**:
each fact once, with a link to the doc that owns it. Builders working in parallel code
against these seams and nothing else.

## Run

```bash
uv sync --all-packages
uv run beepboop version
uv run pytest core docs/module-template/tests
uv run beepboop serve --data-dir /tmp/bb --modules-dir modules   # dev loop on x86
uv run beepboop module check docs/module-template --show-card --rubric
```

## How core starts

`beepboop.service.CoreService.start(CoreConfig)` is the one wiring function
([ARCHITECTURE §2, §11](../docs/ARCHITECTURE.md)); `beepboop serve` calls it:

```
CoreConfig.from_env()                    BB_DATA_DIR, BB_MODULES_DIR, BB_PUBLIC_URL, BB_TZ, BB_KEK_PASSPHRASE_FILE,
                                          BB_AUDIO_SOCKET, BB_HA_URL, BB_HOST_JSON, BB_CA_CERT …
  → Database.open(data_dir/beepboop.db)  migrate forward (pre-migrate snapshot), start the writer, 4 readers
  → open_secrets(db, config)             KEK via resolve_kek, then SecretStore.attach(db, kek) on the single writer;
                                          optional at boot: core still serves the fast path and the UI without it
  → PolicyState                           db.repo.policy_view.load_policy_state(); rebuilt by `refresh_policy()` after any
                                          profiles/grants/group_members/ha_entities/logins/voiceprints write
  → Registry                              + find_tools / read_guide built-ins, audit sink → tool_calls rows
  → discover(modules_dir)                 manifest → import module.py → Module() → setup(ctx) (5 s) → register; failures land
                                          in `modules_errors` and the rest keeps running (MODULES §3)
  → ToolIndex.build(...)                  BM25 for find_tools, rebuilt when modules load
  → HAService.start(token, url=BB_HA_URL) only once onboarding stored the token
  → build_turn_loop(...)                  agent/wiring.py: ladder from household.llm + secrets (NoLLM("unconfigured")
                                          without credentials), DbConversation (+ registry.taint), DbTurnLog, DbDirectory,
                                          DbPinStore
  → AudioBridge.attach(loop)              the loop's Speaker + VoiceControl; also the API's EnrollmentChannel, the
                                          ClaimManager.on_code speaker, ctx.announce, the health probe; the hub's
                                          VoiceprintSink is DbVoiceprintSink; EventBus approval_listeners → notify_approval
  → create_app(ApiDeps)                   FastAPI, /api/openapi.json, problem+json, CSRF, static SPA mount
```

`serve()` then runs uvicorn, the nightly scheduler (`CoreService.scheduler`: 03:00 purge, 03:30
backup, Sunday 04:00 restore check) and, when `BB_AUDIO_SOCKET` is set, the audio connection:
`beepboop_protocol.CoreEventClient(address, AudioBridge.handle)` reconnects with backoff, and each
connection gets `profiles.sync` first and a fresh `agent.voice.VoiceSession` that feeds
`TurnLoop.handle`. The system tests in [`tests/system/`](../tests/system/test_e2e.py) boot exactly
this against a scripted audio peer, the real `beepboop-audio` (fake models), a fake HA and a
scripted LLM.

## Contract map

| Seam | Where | Owning doc |
|---|---|---|
| Risk tiers, `Principal`, `Target`, `Approval`, `Decision`, deny-reason vocabulary | `beepboop.policy.types` | [IDENTITY_AND_TRUST §3, §4, §7.1](../docs/IDENTITY_AND_TRUST.md) |
| `authorize(state, principal, tool, args, targets, now, approval=None)`: pure, the only gate | `beepboop.policy.authorize` (`PolicyState` = profiles + `GrantIndex` + entities + `ServiceTable` + rate limiter + tz) | [IDENTITY_AND_TRUST §7.2](../docs/IDENTITY_AND_TRUST.md#72-algorithm), CLAUDE.md hard rule 1 |
| Grants, schedules, union-only index | `beepboop.policy.grants` (`Grant`, `Schedule`, `GrantIndex`) | [IDENTITY_AND_TRUST §5](../docs/IDENTITY_AND_TRUST.md#5-grants) |
| HA service classes (direct / indirect / blocked) and tiers | `beepboop/policy/ha_services.yaml` via `load_service_table()` | [HOME_ASSISTANT §9](../docs/HOME_ASSISTANT.md#9-service-allowlist-and-risk-tiers) |
| Presets as grant templates | `beepboop/policy/presets/*.yaml`, `apply_preset(name, profile_id, new_id) -> PresetRows` | [IDENTITY_AND_TRUST §9](../docs/IDENTITY_AND_TRUST.md#9-presets) |
| Approvals: create / decide once / execute once / extend once | `beepboop.policy.approvals` (`ApprovalService` over an `ApprovalStore`; `MemoryApprovalStore`, `db.repo.approvals.DbApprovalStore`) | [IDENTITY_AND_TRUST §8.2](../docs/IDENTITY_AND_TRUST.md#82-the-approval-record) |
| Spoken PIN: hashing and lockout state machine | `beepboop.policy.pin` (`hash_pin`, `record_outcome`) | [IDENTITY_AND_TRUST §8.4](../docs/IDENTITY_AND_TRUST.md#84-spoken-pin-fallback-optional-per-profile) |
| The one rate limiter | `beepboop.policy.ratelimit.RateLimiter` | [IDENTITY_AND_TRUST §7.2](../docs/IDENTITY_AND_TRUST.md#72-algorithm) |
| `PolicyDenied(reason, risk)`, `NeedsConfirmation(approval, factors, nonce)` | `beepboop.policy.errors` | [MODULES §5.2](../docs/MODULES.md), [VOICE_PIPELINE §11.6](../docs/VOICE_PIPELINE.md) for the phrases |
| Schema (`0001_init.sql`), PRAGMAs, migrations with `user_version` | `beepboop.db.migrate`, `beepboop.db.connect` | [DATA_AND_PRIVACY §3, §5, §6.1](../docs/DATA_AND_PRIVACY.md) |
| Single writer + read pool: `db.write(fn, durable=)`, `db.read(fn)`, `run_exclusive` | `beepboop.db.database.Database`, `beepboop.db.writer.Writer` | [DATA_AND_PRIVACY §4](../docs/DATA_AND_PRIVACY.md#4-single-writer-pattern) |
| Repositories: plain `fn(conn)` per table family | `beepboop.db.repo.{household, profiles, logins, sessions, grants, ha_entities, voiceprints, integrations, turns, approvals, conversation, kv, audit, policy_view}` | [DATA_AND_PRIVACY §6.1](../docs/DATA_AND_PRIVACY.md#61-v01-tables-0001_initsql) |
| Retention, "Delete me", "Delete this turn", nightly offsets | `beepboop.db.retention` | [DATA_AND_PRIVACY §7, §9.3, §11](../docs/DATA_AND_PRIVACY.md) |
| Module SDK: `Module`, `@tool`, `ToolContext`, `ToolError`, `ToolResult`, `Health`, `PromptBlock`, Protocols `KVStore`, `SecretStore`, `HAClient`, `HttpClient`, `GrantView` | `beepboop.module` | [MODULES §5](../docs/MODULES.md#5-the-module-api-beepboopmodule) |
| `manifest.yaml` model and `manifest.schema.json` | `beepboop.registry.manifest` (`Manifest`, `ToolSpec`, `load_manifest`, `write_schema`) | [MODULES §4](../docs/MODULES.md#4-manifestyaml-reference) |
| Lint L1–L18, token budgets, rubric | `beepboop.registry.lint`, `beepboop.registry.rubric`, `beepboop.tokens.estimate` | [MODULES §4.6, §10.4](../docs/MODULES.md) |
| Core allowlist (namespaces, the 12 always-loaded tools) | `beepboop.registry.allowlist` | [MODULES §4.7](../docs/MODULES.md#47-core-allowlist) |
| Discovery: scan `modules/<id>`, import `module.py` as `beepboop_modules.<id>.module` | `beepboop.registry.loader.discover` | [MODULES §3, §15](../docs/MODULES.md) |
| `Registry.dispatch(principal, tool, args, approval=None, dry_run=False)`: validate → targets → authorize → consume approval → handler with deadline → taint → audit | `beepboop.registry.registry` (`TargetExtractor`, `ContextFactory`, `AuditSink`, `TaintTracker` Protocols) | [ARCHITECTURE §6](../docs/ARCHITECTURE.md#6-one-registry-two-front-doors), [MCP §1](../docs/MCP.md) |
| Resolver: `resolve(principal, TargetQuery, EntityIndex) -> Resolution` (rapidfuzz scoring, areas, floors, `TargetQuery.all`); `StaticEntityIndex` for tests | `beepboop.registry.resolver` | [HOME_ASSISTANT §6.3](../docs/HOME_ASSISTANT.md#63-resolver) |
| `find_tools` / `read_guide`, BM25 index, module cards | `beepboop.registry.find_tools`, `beepboop.registry.builtins` | [MCP §6, §7](../docs/MCP.md) |
| Fake `ToolContext` and evals: `fake_context(module_dir, profile=…)`, `run_evals(module_dir)` | `beepboop.testing` | [MODULES §12](../docs/MODULES.md#12-evalsyaml-and-how-evals-run) |
| Turn loop entry point `TurnLoop.handle(Utterance) -> TurnResult`; `Speaker`, `ConversationStore` seams; deny → phrase mapping | `beepboop.agent.turn`, `beepboop.agent.types` | [ARCHITECTURE §5](../docs/ARCHITECTURE.md#5-turn-lifecycle), [VOICE_PIPELINE §11.6](../docs/VOICE_PIPELINE.md) |
| Voice turns: `VoiceSession(loop, control).on_event(event)` for every audio event (wake supersede, 300 ms `speaker.final` wait → guest, false wake, `pin.result` routing); `TurnLoop.follow_approval`, `notify_approval(id)` (call after a web decide/extend), `deliver_pin_result`; seams `VoiceControl` (`cancel`, `capture_pin`), `PinStore`, `TurnLog`, `ProfileDirectory` | `beepboop.agent.voice`, `beepboop.agent.types` | [ARCHITECTURE §5](../docs/ARCHITECTURE.md#5-turn-lifecycle), [IDENTITY_AND_TRUST §3.1, §8](../docs/IDENTITY_AND_TRUST.md) |
| DB seams of the agent: `DbConversation`, `ConversationTaint`, `DbTurnLog`, `DbDirectory`, `explain_turn` ("why did you do that") | `beepboop.agent.records` | [DATA_AND_PRIVACY §6.1, §9.3](../docs/DATA_AND_PRIVACY.md) |
| Sentence chunking, system phrases, per-turn cost | `beepboop.agent.chunking`, `beepboop.agent.phrases`, `beepboop.agent.usage` | [VOICE_PIPELINE §11.2, §11.6](../docs/VOICE_PIPELINE.md), [LLM_PROVIDERS §10](../docs/LLM_PROVIDERS.md) |
| Fallback ladder: `Ladder`, `Rung`, `build_ladder`, `DnsReachability`; scripted fake provider for tests: `beepboop.testing.llm.ScriptedProvider` | `beepboop.providers.llm.fallback`, `providers/llm/defaults.yaml` | [LLM_PROVIDERS §3.6](../docs/LLM_PROVIDERS.md#36-retry-timeouts-and-the-fallback-ladder) |
| Fast path: `FastPath.for_locale(locale).match(text) -> FastPathMatch \| None`, grammar in YAML | `beepboop.agent.fastpath`, `beepboop/agent/grammar/<locale>.yaml` | [LLM_PROVIDERS §12](../docs/LLM_PROVIDERS.md#12-the-deterministic-fast-path) |
| Prompt layout and prefix hash: `assemble_request(PromptInputs, history, utterance, turn_id=)` | `beepboop.agent.context` | [LLM_PROVIDERS §8](../docs/LLM_PROVIDERS.md#8-prompt-layout-for-caching) |
| `LLMProvider` protocol, Bedrock adapter, conformance | `beepboop.providers.llm` | [LLM_PROVIDERS §2, §6](../docs/LLM_PROVIDERS.md) |
| API: `create_app(ApiDeps, spa_dir=)`, routers in `api/routers/` registered through `ROUTERS`, `Session` / `Admin` dependencies → `Principal(via="web")`, `web_scope`, problem+json, CSRF | `beepboop.api` | [WEB_UI §6, §8](../docs/WEB_UI.md) |
| OpenAPI for the UI client: `beepboop api openapi --out ui/openapi.json` | `beepboop.api.openapi` | [WEB_UI §8](../docs/WEB_UI.md#8-api-conventions) |
| SSE fan-out with the §6.4 filter; the approval nonce reaches the browser only here. `PublishingApprovalService` is the `ApprovalService` the registry gets (publishes on `create`); the executor that re-dispatches after a decision awaits `EventBus.wait_decision(id, wait_s)`, and `EventBus.approval_listeners` (wired to `TurnLoop.notify_approval`) wakes a waiting voice turn; `expire_due(deps)` publishes expiries and `serve()` runs it every 5 s (`service.expire_approvals`) | `beepboop.api.events`, `beepboop.api.routers.approvals` | [WEB_UI §5.7, §8](../docs/WEB_UI.md#57-approvals-second-factor), [IDENTITY_AND_TRUST §8.2](../docs/IDENTITY_AND_TRUST.md#82-the-approval-record) |
| Web credentials (argon2id passwords, RFC 6238 TOTP, recovery codes), the claim code (`ClaimManager.issue/check`, `on_code` speaks it), login throttle, `ColumnSealer` for `*_enc` columns | `beepboop.api.security`, `beepboop.api.claim`, `beepboop.api.ratelimit` | [WEB_UI §5.1, §6](../docs/WEB_UI.md#6-authentication-and-sessions) |
| Enrollment: `EnrollmentChannel` (the audio bridge sends `enroll.start/stop`), `EnrollmentHub.clip(EnrollClipData)` (the bridge feeds results back → SSE `enroll`), `VoiceprintSink.save(...)` (`DbVoiceprintSink`: k ≤ 4 centroids, sealed `centroids_enc` + enroll samples, then a new `profiles.sync`) | `beepboop.api.ports`, `beepboop.api.enroll`, `beepboop.audio_bridge` | [WEB_UI §5.3](../docs/WEB_UI.md#53-people-and-voice-enrolment), [VOICE_PIPELINE §8.2, §9](../docs/VOICE_PIPELINE.md) |
| Audio bridge: `AudioBridge.handle(CoreSide, AudioHelloEvent)` (one connection: `profiles.sync`, `VoiceSession`, `enroll.clip`, `health`, `mic.state`), `speak` numbering (one `utterance_id` per reply, `seq` within it), `say` / `announce` / claim code under a core-minted `turn_id`, `build_profiles_sync` (household hotwords on the `guest` row), `DbPinStore` | `beepboop.audio_bridge` | [ARCHITECTURE §4, §5](../docs/ARCHITECTURE.md#4-audio--core-event-protocol), [VOICE_PIPELINE §9.4, §10.2](../docs/VOICE_PIPELINE.md), [MODULES §9](../docs/MODULES.md) |
| Fake HA WebSocket + fixture household (shared by `core/tests/ha` and `tests/system`) | `beepboop.testing.fake_ha` | [HOME_ASSISTANT §13](../docs/HOME_ASSISTANT.md) |
| API test harness and the fake core the Playwright suite runs against | `beepboop.api.testing`, `beepboop.api.devserver` | [WEB_UI §8](../docs/WEB_UI.md#8-api-conventions) |
| `ctx.http` over httpx limited to `egress` hosts | `beepboop.service_http.HttpxClient` | [MODULES §2, §5.3](../docs/MODULES.md), [DATA_AND_PRIVACY §10](../docs/DATA_AND_PRIVACY.md#10-what-leaves-the-device) |
| Envelope encryption, KEK, `SecretStore` (`attach(db, kek)` in core, `open(path, kek)` standalone), `export_deks` / `import_deks` | `beepboop.secrets` | [DATA_AND_PRIVACY §6.3](../docs/DATA_AND_PRIVACY.md#63-envelope-encryption) |
| Backups: `run_backup(BackupSources, now=)`, `restore_bundle(...)`, `generate_recovery_key()`, `set_recipients`, `BundleBackups` (the api's `BackupRunner`) | `beepboop.db.backup` | [DATA_AND_PRIVACY §11](../docs/DATA_AND_PRIVACY.md#11-backups) |
| Health report for `/api/health/detail` (mounted; probes: HA connection, audio device): `collect_health(db, version=, now=, host_json=, ca_cert=, probes=HealthProbes(...)) -> HealthReport` | `beepboop.health`, `api/routers/health.py` | [OPERATIONS §11](../docs/OPERATIONS.md#11-observability) |
| Nightly jobs: `Scheduler`, `nightly_jobs(purge=, backup=, restore_check=)` | `beepboop.scheduler` | [DATA_AND_PRIVACY §11](../docs/DATA_AND_PRIVACY.md#11-backups) |
| HA onboarding, WebSocket session | `beepboop.ha.onboarding`, `beepboop.ha.ws` | [HOME_ASSISTANT §3](../docs/HOME_ASSISTANT.md#3-headless-onboarding) |
| HA: `HAService(db, policy_state, refresh_policy)` owns `HAConnection` (reconnect, resync), `StateCache`, `RegistryIndex` → `ha_entities` inbox mirror; `.client_for_call(principal, tool, approval=, deadline=)` / `.client_for_hook()` give the policy-wrapped `ctx.ha`; `.extractor` is the registry's `TargetExtractor` for `home.*`/`media.control` | `beepboop.ha.{service,client,cache,index,inbox,plan,wrapper}` | [HOME_ASSISTANT §4-§9](../docs/HOME_ASSISTANT.md#4-the-websocket-client) |

## Extension conventions

- **A new table** is a new `db/migrations/NNNN_<name>.sql` (contiguous, forward-only) plus a repo module in `db/repo/` and a row in DATA_AND_PRIVACY §6; modules never get tables (they get `kv`).
- **A new router** is `api/routers/<name>.py` with `router = APIRouter(prefix="/api/<plural>", tags=["<name>"])`, explicit `operation_id`s, added to `ROUTERS`; auth is the `Session`/`Admin` dependency, never a cookie read in a handler.
- **A new tool** is a manifest entry plus a `@tool("<id>.<verb>")` handler in `modules/<id>/module.py`; core code never calls a handler, only `Registry.dispatch`.
- **A new intent** is a template in `agent/grammar/<locale>.yaml` mapping to an existing tool; the property test rejects templates that can match two intents.
- **A new deny reason, tier or factor** is a doc change in IDENTITY_AND_TRUST first, then `policy/types.py` and the CHECK constraints in the next migration.
- **Policy state** is a snapshot: after writing profiles, grants, group members, entities, logins or voiceprints, call `CoreService.refresh_policy()`; never mutate a `PolicyState`.

## Ownership boundaries (v0.1 builders)

| Builder | Owns | Must not touch |
|---|---|---|
| ha | `beepboop/ha/` (HAConnection, state cache, registry index → `ha_entities`), the policy-wrapped `HAClient` implementation, the `TargetExtractor` for `home.*`/`media.*`, resolver scoring upgrade (rapidfuzz) behind the same signatures, `modules/ha` | `policy/authorize.py`, `registry/registry.py` dispatch order |
| modules | `modules/{timers,lists,weather,caldav,music,persona}` | anything under `core/beepboop` except adding fixtures to `beepboop.testing` |
| agent | sentence chunking, reply/conversation windows against `db.repo.conversation`, fallback ladder, `beepboop chat --as`, `module check --llm`, turns-row writes | `TurnLoop.handle` signature, `registry.dispatch` |
| audio bridge (built by the integrator, `beepboop/audio_bridge.py`) | `CoreEventClient` → `TurnLoop`, `Speaker` over `speak`/`earcon` events, `capture_pin` ↔ `policy.pin`, `profiles.sync` from `voiceprints` | event models (they live in `protocol/`) |
| api / ui | routers (people, grants, inbox, approvals, activity, privacy, onboarding, SSE), login/TOTP/claim sessions, `ui/` | `api/deps.py` Principal construction (extend, do not fork) |
| ops / secrets | `SecretStore` on `Database` (done; the `ScopedSecrets` seam stays), `db.backup`, `health`, `scheduler`, `deploy/` (install.sh, images, host CLI: `doctor`, `update`, `db restore`) | schema without a migration |
| docs | catalog rows, ADRs for any changed default | — |

## Pending on hardware

G3 (`beepboop llm probe --gate` on the Pi), V2 (onboarding against the pinned HA) and V5
(OTP KEK) are owner actions; see [tools/M0_OWNER_CHECKLIST.md](../tools/M0_OWNER_CHECKLIST.md).
v0.1 items that need the Pi are listed per acceptance criterion in
[docs/ROADMAP.md](../docs/ROADMAP.md#v01-one-house-that-knows-your-voice).
