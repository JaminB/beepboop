# 0011. One SQLite file in WAL mode, single writer, envelope-encrypted secrets

> Status: Accepted · 2026-11-01 · Applies to: v0.1, v0.2, v0.3+

All of beepboop's state lives in one SQLite database in WAL mode, written by a single writer task. Secrets and voiceprints inside it are envelope-encrypted with a key derived from the Pi's OTP device key, with a passphrase-wrapped fallback and a recovery key for backups.

Related: [docs/DATA_AND_PRIVACY.md](../DATA_AND_PRIVACY.md) · [docs/OPERATIONS.md §10](../OPERATIONS.md#10-backups-and-restore) · [ADR 0016](0016-docker-compose-mit.md)

## Context

A Pi appliance needs a store that costs nothing at idle, survives power loss, backs up as one file and restores on a different board. Postgres idles at tens of megabytes, adds a container and a migration story, and offers nothing beepboop needs. SQLite in WAL mode adds no process; `synchronous=FULL` guarantees durability for the rows that matter, `NORMAL` is fine for traces. The real threat to data at rest is theft of the SD card or SSD; the Pi has no hardware key store, but the BCM2711 has a write-once OTP private key readable through `/dev/vcio`, which Raspberry Pi's own provisioner uses to unlock LUKS without a passphrase. Memory search in v0.3 needs vectors; `sqlite-vec` provides them in the same file.

## Decision

One file, `beepboop.sqlite`, `journal_mode=WAL`, one async writer task that batches trace writes (SD-friendly), `synchronous=FULL` for auth, grants and approvals, `NORMAL` for turns. The schema is the one in [docs/DATA_AND_PRIVACY.md §6](../DATA_AND_PRIVACY.md#6-schema) and is not restated here; modules never own tables and write through `kv`. Secrets and voiceprints (centroids and per-clip embeddings) are encrypted with libsodium secretbox or AES-GCM under a data key; the data key is wrapped by a KEK derived with HKDF from the OTP device key, or on boards without one by a passphrase. A recovery key generated at onboarding wraps a second copy of the KEK so backups restore on a new Pi. Backups are nightly `VACUUM INTO` plus `age` encryption. Transcripts live 30 days; audio is never stored; delete and export cascade through every table. Embeddings never leave the Pi and are never exposed over MCP.

## Consequences

- Backup is a file copy; restore is tested in CI across releases.
- Encryption protects against theft of the storage medium, not of the whole Pi; the docs say so.
- Lose the recovery key and existing backups cannot be restored elsewhere; the UI insists the phrase is written down.
- Single writer means modules write through `kv` via the writer, never with their own connections or tables.

> **Amended 2026-11-03 (cipher and recovery wording).** "libsodium secretbox or AES-GCM" above is loose: the cipher is **XChaCha20-Poly1305**, libsodium's AEAD (`crypto_aead_xchacha20poly1305_ietf` via PyNaCl), with the row-binding AAD in [DATA_AND_PRIVACY §6.3](../DATA_AND_PRIVACY.md#63-envelope-encryption); the Pi 4 has no AES instructions. "A recovery key … wraps a second copy of the KEK" is also superseded by DATA_AND_PRIVACY §11: the backup bundle carries the DEKs, encrypted to the `age` recovery key. The M0 implementation is `core/beepboop/secrets/`.

> **Amended 2026-11-03 (owner decision D3).** The KEK order is reversed for v0.1: the default on every board, Pi included, is the argon2id passphrase-wrapped KEK, with the passphrase in a root-only host file mounted into core for unattended boot. The installer does not program the OTP key; the OTP-derived KEK is an optional later upgrade. Because the passphrase file lives on the same SSD, SSD theft is protected only with LUKS on; the threat model is in [DATA_AND_PRIVACY §6.3](../DATA_AND_PRIVACY.md#63-envelope-encryption).

> **Amended 2026-11-01.** The Decision originally listed the tables inline (including `timers`, `lists` and "retained enrollment audio"). The review moved timers and lists into `kv` and [ADR 0018](0018-no-audio-retention.md) removed every audio-retention path; DATA_AND_PRIVACY §6 is the only table list.

## Alternatives considered

- **Postgres**: RAM, a container, no benefit at this scale.
- **Full-disk LUKS**: Adiantum is fast enough on a Pi 4 but complicates unattended boot and imaging; offered as an optional extra, not the default.
- **Plaintext secrets in a root-only file**: fails the stolen-card threat.
- **Separate databases per concern**: more files to back up and keep consistent.
