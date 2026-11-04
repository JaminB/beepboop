-- 0001_init: the v0.1 schema. The DDL is owned by docs/DATA_AND_PRIVACY.md §6.1; this file
-- mirrors it exactly. Conventions: ULID TEXT ids, INTEGER Unix-ms UTC *_at, INTEGER 0/1
-- booleans, *_json TEXT, *_enc BLOB envelopes, CHECK enumerations, ON DELETE CASCADE for
-- the per-person delete (§9). Forward-only; tracked with PRAGMA user_version.

-- Household-wide settings. One row per key. Secrets are NOT here (see secrets).
CREATE TABLE household (
  key         TEXT PRIMARY KEY,
  value_json  TEXT NOT NULL,
  updated_at  INTEGER NOT NULL
);

-- People. kind drives web-role visibility only; voice policy uses grants + risk_ceiling.
CREATE TABLE profiles (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL UNIQUE,
  kind                TEXT NOT NULL CHECK (kind IN ('admin','member','child','guest')),
  risk_ceiling        TEXT NOT NULL CHECK (risk_ceiling IN ('read','act','personal','critical')),
  locale              TEXT NOT NULL DEFAULT 'en-US',
  tts_voice           TEXT,
  speech_rate         REAL NOT NULL DEFAULT 1.0,
  verbosity           TEXT NOT NULL DEFAULT 'normal' CHECK (verbosity IN ('brief','normal','chatty')),
  persona_override_json TEXT,
  pin_hash_enc        BLOB,
  pin_failures        INTEGER NOT NULL DEFAULT 0,
  pin_locked_until    INTEGER,
  transcript_retention_days INTEGER,
  disabled_at         INTEGER,
  created_at          INTEGER NOT NULL
);

-- Web login credentials (admin/member, optionally child). One per profile.
CREATE TABLE logins (
  profile_id       TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
  username         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash          TEXT NOT NULL,
  totp_secret_enc  BLOB,
  totp_last_step   INTEGER,
  recovery_hashes_json TEXT,
  failed_attempts  INTEGER NOT NULL DEFAULT 0,
  locked_until     INTEGER,
  created_at       INTEGER NOT NULL
);

CREATE TABLE sessions (
  id_hash     TEXT PRIMARY KEY,
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

-- THE permission primitive. subject -> resource -> max_risk -> schedule. Semantics: IDENTITY_AND_TRUST §5.
CREATE TABLE grants (
  id             TEXT PRIMARY KEY,
  subject_type   TEXT NOT NULL CHECK (subject_type IN ('profile','group')),
  subject_id     TEXT NOT NULL,
  resource_type  TEXT NOT NULL CHECK (resource_type IN ('tool_glob','ha_entity','ha_area','ha_config_entry')),
  resource_id    TEXT NOT NULL,
  max_risk       TEXT NOT NULL CHECK (max_risk IN ('read','act','personal','critical')),
  schedule_json  TEXT,
  source         TEXT NOT NULL,
  created_by     TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  created_at     INTEGER NOT NULL,
  UNIQUE (subject_type, subject_id, resource_type, resource_id)
);
CREATE INDEX grants_subject  ON grants(subject_type, subject_id);
CREATE INDEX grants_resource ON grants(resource_type, resource_id);

-- Mirror of HA's entity registry, keyed by the stable registry UUID. status 'new' is the new-device inbox.
CREATE TABLE ha_entities (
  uuid            TEXT PRIMARY KEY,
  entity_id       TEXT NOT NULL,
  domain          TEXT NOT NULL,
  device_class    TEXT,
  area_id         TEXT,
  device_id       TEXT,
  config_entry_id TEXT,
  friendly_name   TEXT,
  aliases_json    TEXT,
  status          TEXT NOT NULL CHECK (status IN ('new','active','hidden')),
  declared_risk   TEXT CHECK (declared_risk IN ('read','act','personal','critical')),
  first_seen_at   INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL,
  removed_at      INTEGER
);
CREATE INDEX ha_entities_status ON ha_entities(status);
CREATE INDEX ha_entities_area   ON ha_entities(area_id);

-- Speaker-ID model per person. Centroids are ciphertext. One row per (profile, model). No audio anywhere.
CREATE TABLE voiceprints (
  profile_id     TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  model_id       TEXT NOT NULL,
  model_sha      TEXT NOT NULL,
  dim            INTEGER NOT NULL,
  centroids_enc  BLOB NOT NULL,
  k              INTEGER NOT NULL,
  n_clips        INTEGER NOT NULL,
  calib_json     TEXT NOT NULL,
  consent_at     INTEGER NOT NULL,
  consent_version TEXT NOT NULL,
  consented_by   TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  updated_at     INTEGER NOT NULL,
  PRIMARY KEY (profile_id, model_id)
);

-- Individual embeddings: enrolment anchors, the bounded adaptation buffer, and negatives from "that wasn't me".
CREATE TABLE voice_samples (
  id          TEXT PRIMARY KEY,
  profile_id  TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  model_id    TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('enroll','adapt','negative')),
  emb_enc     BLOB NOT NULL,
  quality_json TEXT,
  source_turn TEXT,
  created_at  INTEGER NOT NULL
);
CREATE INDEX voice_samples_profile ON voice_samples(profile_id, kind, created_at);

-- A module's connection for a scope. Settings are plain JSON; anything secret goes to secrets.
CREATE TABLE integrations (
  id                 TEXT PRIMARY KEY,
  module_id          TEXT NOT NULL,
  scope              TEXT NOT NULL CHECK (scope IN ('household','profile')),
  profile_id         TEXT REFERENCES profiles(id) ON DELETE CASCADE,
  settings_json      TEXT NOT NULL DEFAULT '{}',
  ha_config_entry_id TEXT,
  status             TEXT NOT NULL DEFAULT 'off' CHECK (status IN ('ok','reauth','error','off')),
  last_error         TEXT,
  updated_at         INTEGER NOT NULL,
  CHECK ((scope = 'household') = (profile_id IS NULL))
);
CREATE UNIQUE INDEX integrations_unique ON integrations(module_id, COALESCE(profile_id, ''));

-- Data-encryption keys, each wrapped by the current KEK (§6.3). Created before secrets (FK).
CREATE TABLE keyring (
  id            TEXT PRIMARY KEY,
  wrapped_dek   BLOB NOT NULL,
  kek_kind      TEXT NOT NULL CHECK (kek_kind IN ('otp','passphrase')),
  kek_version   INTEGER NOT NULL,
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    INTEGER NOT NULL
);

-- Envelope-encrypted secrets. Read only through ToolContext.secrets (module- and profile-scoped) or core.
CREATE TABLE secrets (
  id            TEXT PRIMARY KEY,
  owner_module  TEXT NOT NULL,
  profile_id    TEXT REFERENCES profiles(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  ciphertext    BLOB NOT NULL,
  nonce         BLOB NOT NULL,
  dek_id        TEXT NOT NULL REFERENCES keyring(id),
  updated_at    INTEGER NOT NULL
);
CREATE UNIQUE INDEX secrets_unique ON secrets(owner_module, COALESCE(profile_id, ''), name);

-- One row per turn (voice, chat, or a module announcement). Written once at end of turn. Purged by retention.
CREATE TABLE turns (
  id            TEXT PRIMARY KEY,
  profile_id    TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  actor_profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  via           TEXT NOT NULL CHECK (via IN ('voice','web','oauth','pat','system')),
  device        TEXT NOT NULL DEFAULT 'main',
  confidence    REAL,
  margin        REAL,
  id_stage      TEXT CHECK (id_stage IN ('provisional','final')),
  locale        TEXT NOT NULL,
  transcript    TEXT,
  reply         TEXT,
  path          TEXT NOT NULL CHECK (path IN ('fast','llm','offline','error')),
  stages_ms_json TEXT NOT NULL,
  tools_offered INTEGER NOT NULL DEFAULT 0,
  tokens_in     INTEGER NOT NULL DEFAULT 0,
  tokens_out    INTEGER NOT NULL DEFAULT 0,
  tokens_cached INTEGER NOT NULL DEFAULT 0,
  cost_micro_usd INTEGER NOT NULL DEFAULT 0,
  llm_provider  TEXT,
  stt_provider  TEXT NOT NULL,
  tts_provider  TEXT NOT NULL,
  tainted       INTEGER NOT NULL DEFAULT 0,
  corrected_profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  flagged       INTEGER NOT NULL DEFAULT 0,
  false_wake    INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  created_at    INTEGER NOT NULL
);
CREATE INDEX turns_profile_time ON turns(profile_id, created_at DESC);
CREATE INDEX turns_time ON turns(created_at DESC);

CREATE TABLE tool_calls (
  id             TEXT PRIMARY KEY,
  turn_id        TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
  profile_id     TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  seq            INTEGER NOT NULL,
  tool           TEXT NOT NULL,
  module_id      TEXT NOT NULL,
  args_redacted_json TEXT NOT NULL,
  targets_json   TEXT,
  effective_risk TEXT NOT NULL CHECK (effective_risk IN ('read','act','personal','critical')),
  decision       TEXT NOT NULL CHECK (decision IN ('allow','deny','confirm')),
  deny_reason    TEXT CHECK (deny_reason IN ('ceiling','no_grant','schedule','scope','service_blocked','disabled',
                                             'rate_limited','identity','impersonation','no_factor','approval_invalid')),
  grant_id       TEXT,
  factor         TEXT CHECK (factor IN ('web','pin','phone','elicit')),
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
  nonce_hash   TEXT NOT NULL UNIQUE,
  profile_id   TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  turn_id      TEXT,
  tool         TEXT NOT NULL,
  args_digest  TEXT NOT NULL,
  summary      TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  exp          INTEGER NOT NULL,
  decision     TEXT CHECK (decision IN ('approved','denied','expired')),
  decided_at   INTEGER,
  executed_at  INTEGER,
  factor       TEXT CHECK (factor IN ('web','pin','phone','elicit')),
  extended     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX approvals_pending ON approvals(profile_id, decision, exp);

-- The 90 s conversation window. One row per (profile, device). Expired rows are deleted by the loop.
CREATE TABLE conversation (
  profile_id    TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  device        TEXT NOT NULL,
  messages_json TEXT NOT NULL,
  tainted       INTEGER NOT NULL DEFAULT 0,
  expires_at    INTEGER NOT NULL,
  PRIMARY KEY (profile_id, device)
);

-- Module scratch storage. The only persistence a module gets (timers, lists, caches).
CREATE TABLE kv (
  module_id   TEXT NOT NULL,
  profile_id  TEXT REFERENCES profiles(id) ON DELETE CASCADE,
  key         TEXT NOT NULL,
  value_json  TEXT NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX kv_pk ON kv(module_id, COALESCE(profile_id, ''), key);

-- Security and accountability log. Actors are pseudonymised on delete, rows are kept (§9).
CREATE TABLE audit (
  id          TEXT PRIMARY KEY,
  at          INTEGER NOT NULL,
  actor_id    TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  actor_name  TEXT NOT NULL,
  via         TEXT NOT NULL CHECK (via IN ('voice','web','oauth','pat','system')),
  action      TEXT NOT NULL,
  target      TEXT,
  details_json TEXT
);
CREATE INDEX audit_time ON audit(at DESC);

-- Seed rows: the three built-in groups and the singleton Guest profile (DATA_AND_PRIVACY §8).
INSERT INTO groups (id, name) VALUES ('household', 'Household'), ('adults', 'Adults'), ('children', 'Children');
INSERT INTO profiles (id, name, kind, risk_ceiling, created_at)
  VALUES ('guest', 'Guest', 'guest', 'act', CAST(strftime('%s','now') AS INTEGER) * 1000);
