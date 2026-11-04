-- The Guest preset (core/beepboop/policy/presets/guest.yaml, IDENTITY_AND_TRUST §9) applied
-- once to the built-in 'guest' profile: read + timers. The YAML owns the list; the test
-- core/tests/db/test_migrate_and_writer.py::test_guest_preset_rows_match_the_yaml keeps
-- this file and the preset equal. An admin may delete or change these rows afterwards.
INSERT OR IGNORE INTO grants
  (id, subject_type, subject_id, resource_type, resource_id, max_risk, schedule_json, source, created_by, created_at)
VALUES
  ('guest-preset-home-state', 'profile', 'guest', 'tool_glob', 'home.state', 'read', NULL, 'preset:guest', NULL, CAST(strftime('%s','now') AS INTEGER) * 1000),
  ('guest-preset-weather',    'profile', 'guest', 'tool_glob', 'weather.*',  'read', NULL, 'preset:guest', NULL, CAST(strftime('%s','now') AS INTEGER) * 1000),
  ('guest-preset-timers',     'profile', 'guest', 'tool_glob', 'timer.*',    'act',  NULL, 'preset:guest', NULL, CAST(strftime('%s','now') AS INTEGER) * 1000);
