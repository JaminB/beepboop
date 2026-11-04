# 0016. Four Docker Compose containers on Pi OS Lite; MIT license; models downloaded, not vendored

> Status: Accepted; licence clause superseded by [0020](0020-gpl-3-and-espeak-phonemiser.md) · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

beepboop ships as a Docker Compose stack of four containers, `caddy`, `beepboop-core`, `beepboop-audio` and `homeassistant`, on Raspberry Pi OS Lite 64-bit, pinned by digest, updated only when an admin says so. The code is MIT; model weights are downloaded at install with every license recorded.

Related: [docs/OPERATIONS.md](../OPERATIONS.md) · [LICENSE](../../LICENSE) · [ADR 0001](0001-home-assistant-backbone.md) · [ADR 0008](0008-audio-core-process-split.md) · [ADR 0009](0009-microwakeword-and-sherpa-onnx.md)

## Context

The appliance must be installable by one person in minutes, reproducible, and recoverable. HA Core is officially supported as a Docker container; HAOS would own the OS. Pi OS Lite 64-bit is what the hardware vendor supports and ships no audio daemon to fight over `/dev/snd`. Watchtower, the usual auto-updater, was archived in December 2025, and silent updates are unacceptable on a device that controls locks. Several speech artifacts carry licenses incompatible with MIT redistribution (openWakeWord models are CC BY-NC-SA, `piper1` is GPL-3, Moonshine and Parakeet are unverified) while being fine to download and run. HA is a trademark of the Open Home Foundation.

## Decision

`deploy/compose.yaml` defines exactly four services with digests, `mem_limit`s, `cpuset`s, healthchecks and host networking where needed; audio has no network. Optional services (Music Assistant, Mosquitto, Z-Wave JS UI, Matter server, Wyoming STT/TTS) are Compose profiles. `deploy/install.sh` is idempotent, prints its steps, and refuses to run on an SD-card root or a 32-bit OS. Updates are notify-only; applying is an admin action that snapshots, pulls by digest, health-checks and rolls back automatically; there is no auto-apply setting. HA is updated only as part of a beepboop release that passed the `ha` contract tests. The repository is MIT with a `NOTICE` file. Weights live in `models/manifest.yaml` with `url`, `sha256`, `license` and `attribution`, are downloaded at install, and `models/LICENSES.md` is generated and linted in CI; NC and GPL-linked artifacts are denied. Wording is "works with Home Assistant", and the HA image is pulled unmodified from its own registry.

> **Superseded in part 2026-11-03 by [ADR 0020](0020-gpl-3-and-espeak-phonemiser.md).** The licence clause no longer holds: the project is GPL-3.0-or-later, not MIT, and GPL-compatible artifacts (including the espeak-ng phonemiser) are allowed; NC and unknown licences stay denied. Everything else in this ADR stands. The `NOTICE` file now exists at the repo root.

> **Amended 2026-11-01.** Optional Compose profiles in v0.1 are `music` only (Music Assistant, service name `music`). Wyoming STT/TTS profiles are v0.2. MQTT brokers, Z-Wave JS UI and the Matter server are **user-managed Home Assistant companions**: beepboop neither ships, pins nor updates them, and their ports are declared in `deploy/ports.yaml` by the user. The compose file is `deploy/compose.yaml` and the installer is `deploy/install.sh`; see [docs/OPERATIONS.md §3](../OPERATIONS.md).

## Consequences

- Install, backup, update and rollback are the same four images everywhere; support can reason about one layout.
- No HAOS add-on store; the important ones are profiles, the rest is documentation.
- A release is a tuple of four digests plus a models manifest; CI tests that tuple on a real Pi.
- Contributors cannot add a model without a license entry; the lint fails.
- Users who already run HA elsewhere are not served in v0.1 (see open question in [docs/ROADMAP.md](../ROADMAP.md)).

## Alternatives considered

- **HAOS with beepboop as an add-on**: HA owns the OS and the audio device; the add-on sandbox fights with real-time audio.
- **Bare-metal systemd services**: no digests, no rollback, dependency drift on the host.
- **Kubernetes (k3s)**: far too heavy for a Pi 4 beside HA.
- **Vendoring model weights**: faster install, incompatible licenses, bloated repo.
- **Apache-2.0 or GPL for the project**: MIT was chosen for maximum reuse; models are kept license-clean separately.
