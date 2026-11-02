# Data and privacy

> Status: Draft spec · 2026-11-01 · Applies to: v0.1 (schema, encryption, retention, export/delete, backups); v0.2+ as invariants only (§6.2, §12)

beepboop keeps everything in one SQLite file on the Pi. **Audio is never stored, with no exceptions.** Secrets and biometric data are envelope-encrypted with a key derived from the Pi's one-time-programmable device key, so a stolen SSD is opaque. Every person can see what is kept about them and what left the house, export it, and delete it with one cascade. This document owns the schema, storage rules, retention, export/delete, egress and backups. **§6 is the only DDL in the spec**; other documents summarise columns and link here.

Related: [ARCHITECTURE.md](ARCHITECTURE.md) · [IDENTITY_AND_TRUST.md](IDENTITY_AND_TRUST.md) · [VOICE_PIPELINE.md](VOICE_PIPELINE.md) · [WEB_UI.md](WEB_UI.md) · [OPERATIONS.md](OPERATIONS.md) · [LLM_PROVIDERS.md](LLM_PROVIDERS.md) · [MODULES.md](MODULES.md) · [SECURITY.md](../SECURITY.md) · ADRs [0011](adr/0011-sqlite-single-file.md), [0006](adr/0006-voice-id-is-personalization.md), [0005](adr/0005-risk-tiers-and-grants.md)

---

## 1. Principles

1. **One file.** `beepboop.db` (SQLite, WAL). No second store, no per-module tables. Modules get `kv`.
2. **No audio, ever.** PCM lives in the audio service's RAM (a ~3 s ring buffer) and is gone after the turn. No enrolment clips, no false-wake clips, no debug retention. A speaker-model change therefore means re-enrolment (about two minutes per person, [VOICE_PIPELINE §9](VOICE_PIPELINE.md#9-enrollment)). An opt-in false-wake queue may be proposed for v0.2 via a new ADR.
3. **Encrypt what hurts.** Secrets, embeddings and the spoken-PIN hash are ciphertext. Transcripts are plaintext in v0.1 (§6.4).
4. **Minimise by default.** 30-day transcript retention, no adaptation from uncertain turns, PIN digits never leave the audio process.
5. **Honest egress.** Everything that leaves the Pi is one table (§10), rendered as the privacy ledger and enforced by `ctx.http`.
6. **Real delete, real export.** Both cascade through every table; "delete me" leaves nothing attributable except pseudonymised safety records.
7. **Guest stores nothing about the guest** (§8).

---

## 2. Storage layout

```
/srv/beepboop/                           # data root on the SSD (OPERATIONS §2)
  core/                                  # mounted at /data in beepboop-core only, mode 0700
    beepboop.db  beepboop.db-wal  beepboop.db-shm
    keys/device_salt                     # 32 random bytes, created at install, never rotated
    keys/kek.passphrase.wrapped          # only on non-Pi dev machines (§6.3)
    export/                              # transient per-user export zips, deleted after download or 1 h
  audio/tts-cache/                       # mounted at /data in beepboop-audio only; pre-rendered TTS clips, never captured audio
  ha/                                    # Home Assistant's own /config; beepboop never reads it
  models/                                # downloaded at install, LICENSES.md; read-only
  config/beepboop.yaml                   # the one household config file (ARCHITECTURE §9)
  backups/                               # local snapshots (§11); the real destination is elsewhere
/run/beepboop/audio.sock                 # audio↔core unix socket (text events only)
```

The audio service never opens the database. Core pushes centroids and thresholds in `profiles.sync` at startup and whenever a voiceprint changes ([ARCHITECTURE §4.2](ARCHITECTURE.md#42-event-catalog)); enrolment embeddings travel the other way as `enroll.clip` events. Nothing under `audio/` is ever a recording.

---

## 3. SQLite settings

Set on every connection by `core/beepboop/db/connect.py`:

```sql
PRAGMA journal_mode = WAL;           -- persistent; set once at create
PRAGMA synchronous = NORMAL;         -- raised to FULL per transaction, see §4
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
PRAGMA secure_delete = ON;           -- overwrite freed pages
PRAGMA wal_autocheckpoint = 1000;    -- plus an explicit TRUNCATE checkpoint nightly before backup
PRAGMA journal_size_limit = 67108864;-- 64 MiB WAL cap after checkpoint
PRAGMA mmap_size = 134217728;        -- 128 MiB, read path only
```

Durability split: `synchronous=FULL` for security or identity state (`profiles`, `logins`, `sessions`, `grants`, `approvals`, `secrets`, `voiceprints`, `keyring`, `audit`), `NORMAL` for traces (`turns`, `tool_calls`, `conversation`, `kv`); the writer sets it per transaction (§4). With `NORMAL` in WAL mode a power loss can lose the last few trace commits but never corrupt the file. `PRAGMA integrity_check` runs weekly on the restored bundle (§11) and on every startup after an unclean shutdown.

---

## 4. Single-writer pattern

Core has **one writer task** owning the only read-write connection (`core/beepboop/db/writer.py`) and a pool of **4 read-only connections** (`file:…?mode=ro`); WAL lets readers proceed while the writer commits.

- **Repositories, not raw SQL at call sites.** `core/beepboop/db/repo/*.py` exposes `profiles.get()`, `turns.record()` etc. Each write is a plain `fn(conn)` passed to `Writer.run(fn, durable=…)`, testable without the queue.
- **Durable writes** (`durable=True`) get their own `BEGIN IMMEDIATE` transaction with `synchronous=FULL`, never batched with trace writes.
- **Trace writes are coalesced**: `turns` and `tool_calls` are written once at end of turn, batched up to 50 `NORMAL` jobs or 250 ms. This caps SSD writes at a few per second.
- **Readers never wait on the writer queue**; read-after-write within one request uses the writer's result.
- **Long operations** (`VACUUM INTO`, migrations, cascade delete) run as a single durable job so nothing interleaves.

---

## 5. Migrations

- Forward-only SQL files in `core/beepboop/db/migrations/NNNN_<name>.sql`, one transaction each, tracked with `PRAGMA user_version` (= highest applied `NNNN`).
- `beepboop db migrate` runs at core startup before the API binds. If `user_version` is ahead of the code (downgrade after rollback), core refuses to start and asks for the pre-update snapshot; `beepboop update` does this automatically ([OPERATIONS §9](OPERATIONS.md#9-updates)).
- Before any migration, core writes `VACUUM INTO backups/pre-migrate-<version>-<ts>.db`, deleted after 7 days.
- Migrations may rebuild tables (SQLite's `ALTER` is limited) with the documented 12-step recipe; `foreign_keys` is off only inside that transaction.
- **Modules never own tables.** They get `kv` (and `secrets` through `ToolContext.secrets`); timers and lists live in `kv`. A module that outgrows `kv` is a conversation about core, not a `CREATE TABLE`.
- Data migrations are idempotent, resumable Python jobs in `migrations/py/`, run after the SQL step. A speaker-model change is **not** one: embeddings cannot be recomputed without audio, so rows for the old `model_id` stay until the person re-enrols and the health page says "re-enrolment needed".

---

## 6. Schema

Conventions: ids are **ULID** `TEXT` (26 chars, time-ordered); timestamps are `INTEGER` Unix milliseconds UTC with a `_at` suffix; booleans are `INTEGER` 0/1; JSON columns end in `_json`; encrypted columns end in `_enc` and hold the envelope from §6.3. Enumerations use `CHECK`. FK `ON DELETE CASCADE` carries the per-person delete (§9).

### 6.1 v0.1 tables (`0001_init.sql`)

```sql
-- Household-wide settings. One row per key. Secrets are NOT here (see secrets).
CREATE TABLE household (
  key         TEXT PRIMARY KEY,         -- 'name','locale','timezone','canonical_url','https_tier','aec_verified',
  value_json  TEXT NOT NULL,            -- 'transcript_retention_days','guest_turn_retention_days','backup_schedule',
  updated_at  INTEGER NOT NULL          -- 'backup_destinations','llm' (LLM_PROVIDERS §2.1), ...
);

-- People. kind drives web-role visibility only; voice policy uses grants + risk_ceiling.
CREATE TABLE profiles (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL UNIQUE,
  kind                TEXT NOT NULL CHECK (kind IN ('admin','member','child','guest')),
  risk_ceiling        TEXT NOT NULL CHECK (risk_ceiling IN ('read','act','personal','critical')),
  locale              TEXT NOT NULL DEFAULT 'en-US',
  tts_voice           TEXT,                       -- NULL = household default
  speech_rate         REAL NOT NULL DEFAULT 1.0,
  verbosity           TEXT NOT NULL DEFAULT 'normal' CHECK (verbosity IN ('brief','normal','chatty')),
  persona_override_json TEXT,                     -- NULL = household persona module settings
  pin_hash_enc        BLOB,                       -- argon2id hash, DEK-wrapped; NULL = spoken-PIN fallback disabled
  pin_failures        INTEGER NOT NULL DEFAULT 0,
  pin_locked_until    INTEGER,
  transcript_retention_days INTEGER,              -- NULL = household default
  disabled_at         INTEGER,                    -- policy denies while set (IDENTITY_AND_TRUST §7.2)
  created_at          INTEGER NOT NULL
);
-- The singleton Guest profile has id 'guest', kind 'guest', risk_ceiling 'act', and is never deleted.

-- Web login credentials (admin/member, optionally child). One per profile.
CREATE TABLE logins (
  profile_id       TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
  username         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash          TEXT NOT NULL,                 -- argon2id encoded string
  totp_secret_enc  BLOB,                          -- NULL = TOTP off
  totp_last_step   INTEGER,                       -- replay guard
  recovery_hashes_json TEXT,                      -- 10 single-use codes, sha256 each
  failed_attempts  INTEGER NOT NULL DEFAULT 0,
  locked_until     INTEGER,
  created_at       INTEGER NOT NULL
);

CREATE TABLE sessions (
  id_hash     TEXT PRIMARY KEY,                   -- sha256(cookie token)
  profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL DEFAULT 'web' CHECK (kind IN ('web','claim')),
  created_at  INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  ua          TEXT
);
CREATE INDEX sessions_profile ON sessions(profile_id);

-- One-time invite links that create a login for an existing profile.
CREATE TABLE invites (
  token_hash  TEXT PRIMARY KEY,
  profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  expires_at  INTEGER NOT NULL,
  used_at     INTEGER
);

-- Exactly three built-in groups in v0.1; no admin-created groups.
CREATE TABLE groups (
  id    TEXT PRIMARY KEY CHECK (id IN ('household','adults','children')),
  name  TEXT NOT NULL UNIQUE
);
CREATE TABLE group_members (
  group_id    TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  PRIMARY KEY (group_id, profile_id)
);

-- THE permission primitive. subject → resource → max_risk → schedule. Semantics: IDENTITY_AND_TRUST §5.
CREATE TABLE grants (
  id             TEXT PRIMARY KEY,
  subject_type   TEXT NOT NULL CHECK (subject_type IN ('profile','group')),
  subject_id     TEXT NOT NULL,                   -- profiles.id or groups.id (no FK: polymorphic; cascade in §9)
  resource_type  TEXT NOT NULL CHECK (resource_type IN ('tool_glob','ha_entity','ha_area','ha_config_entry')),
  resource_id    TEXT NOT NULL,                   -- 'timer.*' | entity-registry UUID | area_id | config_entry_id
  max_risk       TEXT NOT NULL CHECK (max_risk IN ('read','act','personal','critical')),
  schedule_json  TEXT,                            -- NULL = always; else {"days":[...],"start":"HH:MM","end":"HH:MM","expires_at":"<ISO-8601>"?}
  source         TEXT NOT NULL,                   -- 'preset:<name>' | 'inbox' | 'admin' | 'auto:config_entry'
  created_by     TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  created_at     INTEGER NOT NULL,
  UNIQUE (subject_type, subject_id, resource_type, resource_id)
);
CREATE INDEX grants_subject  ON grants(subject_type, subject_id);
CREATE INDEX grants_resource ON grants(resource_type, resource_id);

-- Mirror of HA's entity registry, keyed by the stable registry UUID. status 'new' is the new-device inbox.
CREATE TABLE ha_entities (
  uuid            TEXT PRIMARY KEY,               -- entity registry id
  entity_id       TEXT NOT NULL,                  -- current, updated on entity_registry_updated
  domain          TEXT NOT NULL,
  device_class    TEXT,
  area_id         TEXT,
  device_id       TEXT,
  config_entry_id TEXT,
  friendly_name   TEXT,
  aliases_json    TEXT,
  status          TEXT NOT NULL CHECK (status IN ('new','active','hidden')),
  declared_risk   TEXT CHECK (declared_risk IN ('read','act','personal','critical')),  -- admin override for indirect services
  first_seen_at   INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL,
  removed_at      INTEGER                         -- row and its grants are pruned 30 days after this
);
CREATE INDEX ha_entities_status ON ha_entities(status);
CREATE INDEX ha_entities_area   ON ha_entities(area_id);

-- Speaker-ID model per person. Centroids are ciphertext. One row per (profile, model). No audio anywhere.
CREATE TABLE voiceprints (
  profile_id     TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  model_id       TEXT NOT NULL,                   -- e.g. 'campp-wespeaker-voxceleb-192'
  model_sha      TEXT NOT NULL,
  dim            INTEGER NOT NULL,
  centroids_enc  BLOB NOT NULL,                   -- k×dim float32, little-endian, encrypted (§6.3)
  k              INTEGER NOT NULL,
  n_clips        INTEGER NOT NULL,
  calib_json     TEXT NOT NULL,                   -- {"tau":{...},"margin":{...},"asnorm":{...}}; defaults from VOICE_PIPELINE §8.4
  consent_at     INTEGER NOT NULL,
  consent_version TEXT NOT NULL,                  -- version of the consent text shown (§9.1)
  consented_by   TEXT REFERENCES profiles(id) ON DELETE SET NULL,  -- admin who consented for a child
  updated_at     INTEGER NOT NULL,
  PRIMARY KEY (profile_id, model_id)
);

-- Individual embeddings: enrolment anchors, the bounded adaptation buffer, and negatives from "that wasn't me".
CREATE TABLE voice_samples (
  id          TEXT PRIMARY KEY,
  profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  model_id    TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('enroll','adapt','negative')),
  emb_enc     BLOB NOT NULL,                      -- dim float32, encrypted
  quality_json TEXT,                              -- {"snr":..,"clipping":..,"speech_ratio":..,"duration_ms":..,"position":"couch"}
  source_turn TEXT,                               -- turns.id for adapt/negative (no FK: turns are purged)
  created_at  INTEGER NOT NULL
);
CREATE INDEX voice_samples_profile ON voice_samples(profile_id, kind, created_at);
-- Invariant (enforced in code): ≤ 50 'adapt' rows per profile; oldest evicted. Never for profile 'guest'.

-- A module's connection for a scope. Settings are plain JSON; anything secret goes to secrets.
CREATE TABLE integrations (
  id                 TEXT PRIMARY KEY,
  module_id          TEXT NOT NULL,
  scope              TEXT NOT NULL CHECK (scope IN ('household','profile')),
  profile_id         TEXT REFERENCES profiles(id) ON DELETE CASCADE,  -- NULL iff scope='household'
  settings_json      TEXT NOT NULL DEFAULT '{}',
  ha_config_entry_id TEXT,                        -- kind: ha modules with a per-profile HA config entry
  status             TEXT NOT NULL DEFAULT 'off' CHECK (status IN ('ok','reauth','error','off')),
  last_error         TEXT,
  updated_at         INTEGER NOT NULL,
  CHECK ((scope = 'household') = (profile_id IS NULL))
);
CREATE UNIQUE INDEX integrations_unique ON integrations(module_id, COALESCE(profile_id, ''));

-- Envelope-encrypted secrets. Read only through ToolContext.secrets (module- and profile-scoped) or core.
CREATE TABLE secrets (
  id            TEXT PRIMARY KEY,
  owner_module  TEXT NOT NULL,                    -- module id, or 'core' (Bedrock keys, HA token, DNS token)
  profile_id    TEXT REFERENCES profiles(id) ON DELETE CASCADE,  -- NULL = household
  name          TEXT NOT NULL,                    -- manifest `secrets:` entry name
  ciphertext    BLOB NOT NULL,
  nonce         BLOB NOT NULL,                    -- 24 bytes
  dek_id        TEXT NOT NULL REFERENCES keyring(id),
  updated_at    INTEGER NOT NULL
);
CREATE UNIQUE INDEX secrets_unique ON secrets(owner_module, COALESCE(profile_id, ''), name);

-- Data-encryption keys, each wrapped by the current KEK (§6.3).
CREATE TABLE keyring (
  id            TEXT PRIMARY KEY,
  wrapped_dek   BLOB NOT NULL,                    -- DEK encrypted with KEK, same envelope format
  kek_kind      TEXT NOT NULL CHECK (kek_kind IN ('otp','passphrase')),
  kek_version   INTEGER NOT NULL,
  active        INTEGER NOT NULL DEFAULT 1,       -- new ciphertexts use the active DEK
  created_at    INTEGER NOT NULL
);

-- One row per turn (voice, chat, or a module announcement). Written once at end of turn. Purged by retention.
CREATE TABLE turns (
  id            TEXT PRIMARY KEY,                 -- the turn_id minted by audio (or core, for web/system)
  profile_id    TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,  -- 'guest' when unidentified
  actor_profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,       -- admin behind "test as X"; NULL otherwise
  via           TEXT NOT NULL CHECK (via IN ('voice','web','oauth','pat','system')),  -- 'system' only for ctx.announce
  device        TEXT NOT NULL DEFAULT 'main',
  confidence    REAL,                             -- calibrated speaker confidence (NULL for web)
  margin        REAL,
  id_stage      TEXT CHECK (id_stage IN ('provisional','final')),
  locale        TEXT NOT NULL,
  transcript    TEXT,                             -- NULL if the profile set retention 0 or turn is false_wake
  reply         TEXT,
  path          TEXT NOT NULL CHECK (path IN ('fast','llm','offline','error')),
  stages_ms_json TEXT NOT NULL,                   -- stage names: OPERATIONS §11
  tools_offered INTEGER NOT NULL DEFAULT 0,
  tokens_in     INTEGER NOT NULL DEFAULT 0,
  tokens_out    INTEGER NOT NULL DEFAULT 0,
  tokens_cached INTEGER NOT NULL DEFAULT 0,
  cost_micro_usd INTEGER NOT NULL DEFAULT 0,
  llm_provider  TEXT,                             -- 'bedrock:global.anthropic.claude-haiku-5-5' or NULL (fast path)
  stt_provider  TEXT NOT NULL,                    -- 'sherpa-zipformer' | 'deepgram' ...
  tts_provider  TEXT NOT NULL,
  tainted       INTEGER NOT NULL DEFAULT 0,
  corrected_profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,   -- set by "that wasn't me" (VOICE_PIPELINE §8.8)
  flagged       INTEGER NOT NULL DEFAULT 0,       -- IDENTITY_AND_TRUST §11
  false_wake    INTEGER NOT NULL DEFAULT 0,       -- a flag on the row; no clip is kept
  error         TEXT,
  created_at    INTEGER NOT NULL
);
CREATE INDEX turns_profile_time ON turns(profile_id, created_at DESC);
CREATE INDEX turns_time ON turns(created_at DESC);

CREATE TABLE tool_calls (
  id             TEXT PRIMARY KEY,
  turn_id        TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
  profile_id     TEXT REFERENCES profiles(id) ON DELETE SET NULL,  -- denormalised so safety records survive turn deletion (§9.3)
  seq            INTEGER NOT NULL,
  tool           TEXT NOT NULL,                   -- 'home.turn_on'
  module_id      TEXT NOT NULL,
  args_redacted_json TEXT NOT NULL,               -- x-sensitive fields replaced by "[redacted]"
  targets_json   TEXT,                            -- resolved entity UUIDs (+ entity_id at the time)
  effective_risk TEXT NOT NULL CHECK (effective_risk IN ('read','act','personal','critical')),
  decision       TEXT NOT NULL CHECK (decision IN ('allow','deny','confirm')),
  deny_reason    TEXT CHECK (deny_reason IN ('ceiling','no_grant','schedule','scope','service_blocked','disabled',
                                             'rate_limited','identity','impersonation','no_factor','approval_invalid')),  -- IDENTITY_AND_TRUST §7.1
  grant_id       TEXT,                            -- no FK: grants may be deleted later
  factor         TEXT CHECK (factor IN ('web','pin','phone','elicit')),   -- phone and elicit from v0.2
  approval_id    TEXT,
  untrusted_output INTEGER NOT NULL DEFAULT 0,
  result_status  TEXT NOT NULL CHECK (result_status IN ('ok','error','denied','pin_failed','timeout','dry_run','cancelled')),
  duration_ms    INTEGER,
  created_at     INTEGER NOT NULL,
  UNIQUE (turn_id, seq)
);
CREATE INDEX tool_calls_safety ON tool_calls(effective_risk, decision, created_at);

-- Second-factor approvals (IDENTITY_AND_TRUST §8.2). The id is public (URLs); the nonce is the secret.
CREATE TABLE approvals (
  id           TEXT PRIMARY KEY,
  nonce_hash   TEXT NOT NULL UNIQUE,              -- sha256 of the 128-bit nonce carried in the SSE event
  profile_id   TEXT REFERENCES profiles(id) ON DELETE SET NULL,  -- the ONLY profile that may approve
  turn_id      TEXT,                              -- no FK: turns are purged first
  tool         TEXT NOT NULL,
  args_digest  TEXT NOT NULL,                     -- sha256(tool || canonical_json(args) || sorted target uuids)
  summary      TEXT NOT NULL,                     -- "Unlock the front door"
  created_at   INTEGER NOT NULL,
  exp          INTEGER NOT NULL,                  -- created_at + 60 000, extended once to +120 000
  decision     TEXT CHECK (decision IN ('approved','denied','expired')),  -- NULL = pending; set once
  decided_at   INTEGER,
  executed_at  INTEGER,                           -- set once by the executor
  factor       TEXT CHECK (factor IN ('web','pin','phone','elicit'))
);
CREATE INDEX approvals_pending ON approvals(profile_id, decision, exp);

-- The 90 s conversation window. One row per (profile, device). Expired rows are deleted by the loop.
CREATE TABLE conversation (
  profile_id    TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  device        TEXT NOT NULL,
  messages_json TEXT NOT NULL,
  tainted       INTEGER NOT NULL DEFAULT 0,       -- taint is conversation state (IDENTITY_AND_TRUST §3.2)
  expires_at    INTEGER NOT NULL,
  PRIMARY KEY (profile_id, device)
);

-- Module scratch storage. The only persistence a module gets (timers, lists, caches).
CREATE TABLE kv (
  module_id   TEXT NOT NULL,
  profile_id  TEXT REFERENCES profiles(id) ON DELETE CASCADE,  -- NULL = household scope
  key         TEXT NOT NULL,
  value_json  TEXT NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX kv_pk ON kv(module_id, COALESCE(profile_id, ''), key);
-- Invariant (enforced in ToolContext.kv): ≤ 1 MiB per (module, profile); values are JSON ≤ 64 KiB.

-- Security and accountability log. Actors are pseudonymised on delete, rows are kept (§9).
CREATE TABLE audit (
  id          TEXT PRIMARY KEY,
  at          INTEGER NOT NULL,
  actor_id    TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  actor_name  TEXT NOT NULL,                      -- snapshot; becomes 'deleted user' on delete
  via         TEXT NOT NULL CHECK (via IN ('voice','web','oauth','pat','system')),
  action      TEXT NOT NULL,                      -- 'login.ok','grant.add','approval.approve','not_me','export',
                                                  -- 'delete_profile','update.apply','backup.ok','retention.purge', ...
  target      TEXT,                               -- profile id, grant id, tool, module id
  details_json TEXT
);
CREATE INDEX audit_time ON audit(at DESC);
```

### 6.2 Later migrations (v0.2+, invariants only)

Nothing below is in the v0.1 DDL; it is listed so v0.1 code does not foreclose it. Scope and order: [ROADMAP.md](ROADMAP.md).

- Every later table holding personal data carries `profile_id … ON DELETE CASCADE`, so §9.3 stays one cascade.
- v0.2: `profiles.phone_notify_target`, `passkeys`, `api_tokens`, `oauth_clients`/`oauth_codes`; the `factor` enums already admit `phone` and `elicit`.
- v0.3: `memory` (+ a vector virtual table, §12) and `devices` for satellites; until then `turns.device = 'main'`.
- A false-wake clip queue is **not** planned here; a proposal needs a new ADR amending principle 2.

### 6.3 Envelope encryption

```
                 ┌────────────────────────────────┐
  Pi OTP key ──▶ │ HKDF-SHA256(ikm=otp, salt=device_salt, info="beepboop/kek/v1") │ ──▶ KEK (32 B)
                 └────────────────────────────────┘
  (dev fallback:  argon2id(passphrase, device_salt) ──▶ KEK)

  KEK ── wraps ──▶ DEK (32 B, random, in keyring.wrapped_dek)
  DEK ── encrypts ──▶ every *_enc / ciphertext column:
                      XChaCha20-Poly1305 (libsodium, PyNaCl), 24-byte random nonce,
                      AAD = "<table>|<row id or pk>|<column>"  (binds ciphertext to its row)
```

| Item | Choice |
|---|---|
| Cipher | `crypto_aead_xchacha20poly1305_ietf` via PyNaCl: the Pi 4 has no AES instructions, and 192-bit nonces make random nonces safe. |
| Envelope format | `version(1) ‖ dek_id(16) ‖ nonce(24) ‖ ciphertext‖tag` as the `BLOB` (`secrets` splits the nonce into its own column). Self-describing, so rotation and restore need no side table. |
| AAD | Table, row key and column name, so a ciphertext cannot be moved between rows. |
| KEK source on a Pi | `rpi-otp-private-key` (BCM2711 customer OTP, 256 bits), written once by the installer if blank (confirmed interactively; irreversible), read through `/dev/vcio`, passed only to `beepboop-core`. Unattended boot with no key on disk. |
| KEK source elsewhere | Passphrase → argon2id; wrapped KEK in `keys/kek.passphrase.wrapped`; passphrase from `BB_KEK_PASSPHRASE_FILE` or a prompt. For x86 development and CI. |
| `device_salt` | 32 random bytes at install; makes a reinstalled Pi derive a different KEK and makes a backup bundle portable (§11). |
| Rotation | `beepboop keys rotate-dek` creates a new active DEK and re-encrypts rows in the background. KEK rotation is re-wrapping the DEKs (`kek_version++`). |
| Where decryption happens | Only in `core/beepboop/db/crypto.py`. `ToolContext.secrets.get(name)` returns a `SecretStr`; nothing caches plaintext past the request. |

**Protects against:** reading the SSD in another machine; a leaked backup bundle; a copied database file; another container reading the volume (mounted in core only; the OTP key needs `/dev/vcio`).

**Does not protect against:** theft of the whole Pi (the OTP key is in the SoC); root on the running Pi; a compromised `beepboop-core`; memory dumps. The Pi has no hardware-protected key store and the UI says so. Optional full-disk LUKS ([OPERATIONS §2](OPERATIONS.md#2-os-preparation)) covers the plaintext items in §6.4, but not a stolen Pi.

### 6.4 What is encrypted, what is not

| Encrypted (DEK-wrapped) | Plaintext on the SSD |
|---|---|
| OAuth tokens, CalDAV app passwords, Bedrock keys, HA long-lived token, DNS-01 tokens, TOTP secrets (`secrets`, `logins.totp_secret_enc`) | Transcripts and replies (`turns`), tool args (`args_redacted_json`) |
| Voiceprint centroids and every embedding (`voiceprints`, `voice_samples`) | Names, grants, settings JSON, module `kv` (timers, lists) |
| The spoken-PIN hash (`profiles.pin_hash_enc`) | Audit rows, session hashes (already hashes) |
| Backups as a whole (age, §11) | Home Assistant's `.storage` (including per-profile HA config-entry tokens), Music Assistant data, Caddy's local CA key |
| | Local pre-migration and pre-update snapshots (same volume, same protection as the live file; deleted after 7 days) |

The right-hand column is covered only by optional LUKS and the device being inside the home; [IDENTITY_AND_TRUST §12](IDENTITY_AND_TRUST.md#12-threat-model) T8 lists it as residual risk. Transcripts stay plaintext in v0.1 so that Activity search, purges and the ledger are simple SQL; the exposure is ≤ 30 days of household speech from a stolen SSD. Per-row encryption of `transcript`/`reply` is a contained change (repo layer plus Python-side search) and the first thing to do if that is judged unacceptable (§13).

---

## 7. Retention defaults

| Data | Default | Range | Who can change | Purge |
|---|---|---|---|---|
| Audio (pre-roll, command, enrolment, PIN) | RAM only, ≤ 3 s ring buffer, gone at end of turn | none | nobody | n/a |
| Transcripts, replies, ordinary tool calls (`turns`, `tool_calls`) | 30 days | 0 (timing and decisions only) to 365 | the person for own turns; admin for the household default | nightly, per profile |
| **Safety records** (§9.3) | 365 days | fixed | — | nightly; pseudonymised, never deleted early |
| Guest turns | 7 days (`household.guest_turn_retention_days`) | 0 to 30 | admin | nightly |
| Conversation window | 90 s | fixed | — | agent loop on expiry |
| Adaptation embeddings (`voice_samples.kind='adapt'`) | rolling ≤ 50 per person | fixed cap | — | eviction on insert; nightly centroid recompute |
| Enrolment and negative embeddings | until the voiceprint is deleted | — | the person / admin | cascade |
| Sessions; invites; claim codes | 30 days idle / 90 absolute ([WEB_UI §6.2](WEB_UI.md#62-sessions-and-cookies)); 24 h; 15 min | fixed | sign out / revoke | on access + nightly |
| Module `kv` (done timers, list history) | module-defined; timers 7 days after done | — | module | module's `setup()` sweep |
| Audit | 365 days | 90–730 | admin | nightly |
| Health samples (latency p50/p90 view) | 7 days, in `kv` under `module_id='core.health'` | fixed | — | ring in code |
| Container logs | [OPERATIONS §2](OPERATIONS.md#2-os-preparation) | — | admin | Docker |
| Backups | 7 daily + 4 weekly at the destination | configurable | admin | §11 |

Retention is the first nightly job (03:00 local, §11); each purge is an `audit` row `retention.purge` with counts. Retention 0 keeps `turns` rows with `transcript`/`reply` `NULL`, so Activity still explains *what* happened without *what was said*.

---

## 8. Guest

`profiles.id = 'guest'` is a singleton with `kind='guest'`, `risk_ceiling='act'`, no login, and only the grants the admin gives it ([IDENTITY_AND_TRUST §9](IDENTITY_AND_TRUST.md#9-presets)).

Guests did not consent to anything, so beepboop stores nothing *about them*: no `voiceprints`, no `voice_samples` (adaptation never runs on Guest turns; "that wasn't me" negatives go under the *wrongly matched* profile), no `conversation` window beyond the turn, no `kv`, no `secrets`. The identity result for a guest is the label `guest`, not an embedding.

Turns attributed to Guest *are* household activity, kept 7 days by default (§7): "who unlocked the door at 3 a.m." is exactly the turn the admin needs. The UI labels them "Guest (unknown voice)" and every member's ledger explains this. A babysitter is not a profile: **Guest access** inserts ordinary grants for `guest` with `schedule.expires_at`, swept nightly when they expire.

---

## 9. Consent, export and delete

### 9.1 Biometric consent

Voice embeddings are biometric data: special-category under GDPR Art. 9 (basis: explicit consent) and "voiceprints" under Illinois BIPA, which requires informed written consent, a published retention schedule, and destruction when the purpose ends or within 3 years of last interaction; Texas and Washington are similar. The household is the controller; the project ships the mechanics:

- **Consent screen** before the first enrolment clip (`core/beepboop/locale/<locale>/consent.md`, versioned into `voiceprints.consent_version`). It states what is stored (a mathematical voice model, never a recording), where (this device, encrypted), why (to tell voices apart and personalise; not to authenticate), who can see it (nobody), how long (until deleted, or after 3 years without a confident match), and how to delete it (People › `<name>` › Voice › Delete, or "Hey beepboop, forget my voice" with web confirmation).
- **Children**: an admin enrols and consents on the child's behalf (`consented_by`), satisfying GDPR Art. 8 and COPPA. Re-consent is needed only if the text version changed.
- **Withdrawal** is one tap and equals deleting the voiceprint: `DELETE FROM voiceprints`, cascade to `voice_samples`, `profiles.sync` to audio, `audit` row `voiceprint.delete`.
- **Automatic destruction**: voiceprints with no `adapt` sample and no confident turn in 3 years are deleted nightly after a 30-day warning on Home.
- **Never**: embeddings are never shown, exported (§9.2), sent to any provider, or reachable through `/mcp`. A speaker-model upgrade asks for re-enrolment; there is no audio to re-embed from.

### 9.2 Export (`POST /api/privacy/{profile_id}/export`)

Produces `beepboop-export-<name>-<date>.zip` in `export/`, downloadable once within 1 h, then deleted. Contents (JSON, UTF-8):

| File | Source |
|---|---|
| `profile.json` | `profiles` (minus `pin_hash_enc`), `logins` (username, TOTP on/off; no hashes), `group_members` |
| `grants.json` | `grants` for this profile or its groups, with resolved HA names |
| `turns.jsonl` | `turns` + nested `tool_calls` + `approvals` for this profile |
| `modules.json` | the person's `kv` rows per module, plus items they added to household lists |
| `integrations.json` | `integrations` with `scope='profile'`: settings only; secrets listed by name with `"set": true` |
| `voiceprint.json` | metadata only (model id, k, n_clips, consent record, clip quality, dates). **No embeddings**: they would let anyone with the file impersonate the person to any beepboop |
| `audit.jsonl` | audit rows where `actor_id` is this profile |
| `README.txt` | what each file is, the retention policy in force, the export timestamp |

Admins may export anyone; members export themselves. The export is audited. Later data classes (memory, v0.3) join the zip with their migration.

### 9.3 Delete ("Delete me") and "Delete this turn"

`DELETE /api/privacy/{profile_id}` after typing the profile name. Refused for the last admin and for `guest`. Runs as one durable writer job:

```
1. sessions, invites                     → deleted (cascade); user is signed out everywhere
2. approvals pending                     → decision='denied'; then pseudonymised (step 9)
3. turns                                 → deleted (cascade); safety-record rule below
4. conversation, kv (profile rows)       → deleted (cascade); household kv items keep their text, lose the author
5. integrations scope=profile + secrets  → deleted (cascade) after the module's `on_disconnect` hook (revoke upstream if possible)
6. voice_samples, voiceprints            → deleted (cascade); profiles.sync tells audio to drop centroids
7. grants subject=profile, group_members → deleted
8. audit                                 → kept; actor_id → NULL, actor_name → 'deleted user'
9. safety records                        → kept and pseudonymised (rule below)
10. profiles row                         → deleted; secure_delete zeroes freed pages
11. audit row 'delete_profile' by the acting admin (target = the deleted id, no name)
```

Later tables (§6.2) join the cascade through their `profile_id` FK.

**Safety-record rule** (the same rule as [IDENTITY_AND_TRUST §11](IDENTITY_AND_TRUST.md#11-audit-log)): `tool_calls` rows with `effective_risk = critical` or `decision = confirm`, and every `approvals` row, are pseudonymised (`profile_id → NULL`, `actor_name = "deleted user"`, `args_redacted_json` kept) rather than deleted, by both "Delete this turn" and "Delete me", and are purged at 365 days. "Delete this turn" (Activity, or the `forget_last` fast-path intent) writes `transcript`/`reply` as `NULL` and deletes the ordinary `tool_calls` rows; a critical or confirmed call it contained stays on record without a name.

Household-scope data the person touched but does not own stays. Earlier backups still contain the data; the UI says so and recommends rotating the backup set if the deletion must be complete.

---

## 10. What leaves the device

Nothing leaves without a row in this table. Each module's `manifest.yaml` declares `egress:` entries (`destination`, `data`, `purpose`, `when`), required by `beepboop module check` for any module using `ctx.http`; core declares its own. **`ctx.http` refuses undeclared hosts before any socket opens**, so the ledger ([WEB_UI §5.10](WEB_UI.md#510-privacy-ledger)) is true by construction; it is this table joined with per-person counts from `turns` and `integrations`.

| Destination (v0.1) | Data sent | Never sent | When | Counted from |
|---|---|---|---|---|
| **Amazon Bedrock** (admin-chosen region; a `global.` profile may route within the geography) | System prompt (persona, tool schemas, module cards), the speaker's **first name**, the transcript, the 90 s conversation window, tool results (calendar text, HA entity names and states, list contents) | Audio, embeddings, PIN, secrets, entity-registry UUIDs, other profiles' data | Every `path='llm'` turn | `turns.llm_provider` |
| **Weather provider** (Met.no via HA by default) | Configured location | Who asked | `weather.*` calls | `tool_calls` where `module_id='weather'` |
| **The person's own CalDAV server** | Credentials over TLS, calendar reads/writes | — | `caldav.*` calls | `tool_calls` |
| **Home Assistant** (loopback) | Service calls and state subscriptions | — | Always | not egress; HA's own integrations may call out and are outside this table |
| **Container registry** (GHCR) | Image digest requests (reveal the Pi's public IP and version) | — | Daily update check | `audit` `update.check` |
| **NTP** | Time requests | — | Continuous | not counted |

v0.2+ invariants ([ROADMAP.md](ROADMAP.md)): cloud STT/TTS (Deepgram, Polly) are opt-in, ship as a Compose override that gives audio a network, send only the turn's command audio or reply text (never the pre-roll or speaker identity), and get rows here before they ship. Tier 1 HTTPS adds the ACME/DNS provider (hostname in Certificate Transparency logs) or Tailscale control traffic. The OAuth bounce page has no backend and never contacts the Pi.

Rules that keep the table honest:

- **Redaction before the model.** The volatile prompt block carries `speaker: Alex` (first name only), local time and device area. Entity references in tool results are friendly names, never UUIDs. `capture_pin` runs entirely in the audio service; the LLM only sees "the user confirmed".
- **Redaction before the log.** Tool args marked `x-sensitive: true` are stored as `"[redacted]"`; structlog redacts `*secret*`, `*token*`, `*password*`, `*pin*` keys.
- **Provider data policies** are summarised in the ledger's *Why?* text; [LLM_PROVIDERS.md](LLM_PROVIDERS.md) owns the Bedrock citation.
- **Offline is a feature.** The fast path runs timers, lights and media with zero egress, and the ledger shows that.

---

## 11. Backups

This section owns backup mechanics; [OPERATIONS §10](OPERATIONS.md#10-backups-and-restore) is the operator's view.

**Nightly schedule**, through the core scheduler at `nice 19`: **03:00** retention purge (§7), **03:10** centroid recompute (v0.2, [VOICE_PIPELINE §8.7](VOICE_PIPELINE.md#87-adaptation)), **03:30** backup. Kept at the destination: **7 daily + 4 weekly**. Destinations in v0.1: the local SSD (always, as the first copy) plus an optional USB path or SMB share; S3-compatible remotes are v0.2. CLI: `beepboop db backup` (core) and `beepboop db restore <bundle> --identity <key>` (host).

```
1. writer: PRAGMA wal_checkpoint(TRUNCATE)
2. writer: VACUUM INTO 'backups/tmp/beepboop.db'        -- online, consistent, compacted copy
3. bundle (tar, built in memory/tmpfs):
     beepboop.db
     keys/device_salt
     dek.json          -- every keyring DEK in PLAINTEXT, exported from memory for this bundle only
     ha/               -- HA's .storage and configuration.yaml (recorder history is disposable and excluded)
     manifest.json     -- schema user_version, core version, HA version, created_at, host
4. age -r <recovery public key> [-r <second recipient>]  →  beepboop-<host>-<ts>.tar.age
5. copy to each destination; verify size + sha256
6. prune: keep 7 daily + 4 weekly at each destination
7. audit 'backup.ok' | 'backup.fail' (shown on Home as "Needs attention")
```

- **Recovery key.** An `age` X25519 identity generated at onboarding. The public key is stored in `household`; the secret identity is shown **once** (QR, text, download, printable card) and never stored on the Pi. Losing it means backups are unreadable; the UI says so twice. A passphrase recipient (`age -p`) can be added.
- **Why the DEK is inside the bundle.** The live DEK is wrapped by a KEK derived from *this* Pi's OTP key; a replacement Pi has a different one, so the restore must bring the DEK along, protected by the recovery key. On restore, core imports `dek.json` and re-wraps each DEK under the new Pi's KEK.
- **Pre-update snapshot** runs steps 1–2 plus a copy of `ha/`. It never includes `dek.json`: it is restored on the same Pi, whose keyring already holds the wrapped DEKs. It lives in `backups/pre-update-<ts>/`, feeds the auto-rollback, and is deleted after the next successful update or 7 days.
- **CI security test:** run a backup and a pre-update snapshot on a fixture, then grep every file under the data root for the fixture DEK bytes; none may match.
- **Restore** (`beepboop db restore` or the onboarding "Restore from backup" step): verify the manifest, refuse if `user_version` is newer than the code, stop the agent, replace `beepboop.db` and `ha/`, import DEKs, migrate forward, push voiceprints to audio, restart.
- **Weekly restore check.** The scheduler restores the latest bundle into a temp dir using a second, Pi-held test recipient, runs `PRAGMA integrity_check`, deletes it, and reports on the health page; failure is `backup.fail`.
- **Local-only backups are not backups.** If the only destination is the same SSD, Home shows a standing warning until a USB, SMB or (v0.2) remote destination is configured.

---

## 12. Memory (v0.3, invariants only)

Per-person memory lands in v0.3 ([ROADMAP.md](ROADMAP.md)); its DDL comes with that migration. Invariants fixed now:

1. **Strictly per profile.** Retrieval filters by `profile_id` before similarity search; no household memory, no cross-profile read, none for Guest. Admins see children's memory under the one parental rule ([IDENTITY_AND_TRUST §10](IDENTITY_AND_TRUST.md#10-parental-view-of-child-history)).
2. **`personal` tier.** `memory.*` tools need the `personal` threshold and margin, or the second factor.
3. **Explicit by default.** Only "remember that…" creates rows; inferred memory is a per-profile opt-in with review before use.
4. **Local embeddings or FTS5**; memory text is never sent to an embedding API. When recalled it goes to the LLM like any tool result and the ledger counts it.
5. **Viewable, editable, forgettable** in the UI; included in export and delete (§9).

---

## 13. Open questions

1. **[v0.1 ruling] Plaintext transcripts at rest.** §6.4 argues for simplicity plus short retention and optional LUKS; the alternative is per-row encryption from day one, contained to the `turns` repo and Activity search.
2. **[v0.1 ruling] Voiceprint export excludes embeddings** (§9.2). Strict data-portability readings may want them included; this doc treats the impersonation risk as decisive.
3. **[v0.1 ruling] Bedrock data-handling claim** in §10 must be cited and re-verified against current AWS documentation before publication; LLM_PROVIDERS owns the citation.
