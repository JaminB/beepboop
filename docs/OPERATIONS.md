# Operations

> Status: Draft spec · 2026-11-01 · Applies to: M0, v0.1; v0.2 as invariants only

How to buy, install, run, update, back up, observe and fix a beepboop: a Raspberry Pi 4 running four Docker Compose containers on Raspberry Pi OS Lite 64-bit (`caddy`, `beepboop-core`, `beepboop-audio`, `homeassistant`). One person with a phone and an SSH client can keep it healthy for years, and nothing changes on the device without an admin saying yes. Numbers marked **target** are unmeasured until M0 ([ROADMAP.md](ROADMAP.md)). `beepboop.dev` is a **planned domain**; nothing is published there yet.

Related: [README.md](../README.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [VOICE_PIPELINE.md](VOICE_PIPELINE.md) · [WEB_UI.md](WEB_UI.md) · [DATA_AND_PRIVACY.md](DATA_AND_PRIVACY.md) · [HOME_ASSISTANT.md](HOME_ASSISTANT.md) · [LLM_PROVIDERS.md](LLM_PROVIDERS.md) · [ROADMAP.md](ROADMAP.md) · [SECURITY.md](../SECURITY.md) · ADRs [0008](adr/0008-audio-core-process-split.md), [0011](adr/0011-sqlite-single-file.md), [0013](adr/0013-https-tiers.md), [0016](adr/0016-docker-compose-mit.md)

---

## 1. Bill of materials

| Part | Requirement | Notes |
|---|---|---|
| Raspberry Pi 4 Model B | **8 GB recommended, 4 GB minimum.** | 4 GB runs the default install plus **at most the `music` profile** and must pass the M0 RAM gate (G4). Choose 8 GB for Matter, Zigbee or Z-Wave companions together with music. |
| Power supply | Official Raspberry Pi USB-C 5.1 V / 3 A | Under-voltage throttles the CPU (`get_throttled` bit 0) and is the most common cause of "slow replies". |
| Storage | **USB 3 SSD, required.** 64 GB or more; SATA in a UASP adapter, or an NVMe enclosure. | Boot from the SSD directly (no SD card); HA's recorder and beepboop's traces wear out SD cards. **The SSD should draw < 600 mA, or use a powered adapter**; the Pi's USB ports share 1.2 A with the mic array. |
| Cooling | **Active fan, required.** | Sustained STT plus HA on a passive case reaches the 80 °C soft throttle in minutes (§8). |
| Microphone array | **Seeed ReSpeaker XVF3800 USB 4-mic array**, **16 kHz USB firmware**. | Onboard AEC, beamforming and AGC; beepboop consumes the processed ASR beam. The firmware is pinned in `deploy/firmware.lock`; the health page warns on another version. |
| Speaker | Any powered speaker on the array's 3.5 mm line-out. | **All playback goes through the array**: it is the AEC reference (M0 item V3). The Pi's own outputs are unused. |
| Network | Ethernet preferred; Wi-Fi supported. | Bedrock TTFT on Wi-Fi is worse under contention; the health page shows Bedrock RTT. |
| Optional | Zigbee or Z-Wave USB stick; a GPIO mute switch or button. | Sticks are HA's business (user-managed companions, §3): `/dev/serial/by-id/...` on a short USB 2 extension away from the USB 3 ports, which radiate on 2.4 GHz. The button re-enters claim mode when held 10 s (§6). |

Approximate cost: USD 150–220 for Pi, PSU, SSD and case; USD 70–90 for the XVF3800; speaker from USD 20.

## 2. OS preparation

Everything below is what `install.sh` (§5) does; it is listed so an operator can audit it or do it by hand.

1. **Flash** Raspberry Pi OS **Lite, 64-bit** to the SSD with Raspberry Pi Imager (hostname `beepboop`, a user, SSH key, locale, Wi-Fi if needed). 32-bit is unsupported.
2. **Boot order.** On a Pi 4 that has never booted from USB, update the bootloader EEPROM once from an SD card (`rpi-eeprom-update -a`) and set USB-first boot.
3. **Host packages:** `docker-ce` with the Compose v2 plugin, `zram-tools`, `nftables`, `rpi-eeprom`, `avahi-daemon` (mDNS for `beepboop.local`), `chrony` (no RTC; SigV4 and TLS fail with clock skew), `fake-hwclock`.
4. **Kernel cgroups.** Append `cgroup_enable=memory cgroup_memory=1 swapaccount=1` to `/boot/firmware/cmdline.txt` and reboot once; without it Docker silently ignores `mem_limit`. `beepboop doctor` checks this.
5. **Memory and logs:** zram swap at 50 % of RAM (`zstd`), `vm.swappiness=60`, journald capped at 200 MB persistent on the SSD. Container logs use the Docker `json-file` driver, `max-size 20m, max-file 5`: about 100 MB per service, **about 10 days at normal volume** for core. This is the only log-retention rule; other docs link here.
6. **Clock and watchdog:** chrony `makestep 1 -1` (step at any offset on every sync); the BCM2711 hardware watchdog with `RuntimeWatchdogSec=30`.
7. **Audio:** Pi OS Lite starts no PipeWire or PulseAudio, so the container's raw ALSA access to `/dev/snd` is uncontested. A udev rule gives the array a stable ALSA name (`hw:XVF3800`) and `audio` group ownership.
8. **Firewall (default-deny inbound):** nftables `input` policy `drop` ([`deploy/nftables/beepboop.nft`](../deploy/nftables/beepboop.nft); it never flushes Docker's tables). In order: accept loopback; **drop `8123/tcp`** (before anything can re-open it, [HOME_ASSISTANT §2.1](HOME_ASSISTANT.md#21-loopback-bind-plus-nftables)); accept established/related, drop invalid; accept ICMP echo-request, destination-unreachable and time-exceeded (IPv4 path MTU), all ICMPv6 (neighbour discovery, router advertisements, PMTU: IPv6 breaks without them) and DHCPv6 client replies (`546/udp` from `fe80::/10`); then the ports in `deploy/ports.yaml`: `base` (`22/tcp`, `80/443/tcp`, `8443` and `8445/tcp` for the Caddy admin gates, `5353/udp`), each enabled Compose profile's entries (e.g. Music Assistant stream `8097`) and the `user` entries. HA's own `server_host: 127.0.0.1` binding is primary for :8123; the drop is the backstop (M0 item V1). Default-deny also drops SSDP/UPnP discovery replies and inbound HomeKit, so integrations relying on them need a `user` entry. `beepboop doctor` lists every listening non-loopback socket and flags any not on the allowlist.

   **[`deploy/ports.yaml`](../deploy/ports.yaml)** (its header documents the schema) lists `base`, per-profile, `user` and `loopback_only` sockets, each with a mandatory reason. `base` is literal in the nftables file; `install.sh` adds the enabled profiles' and the `user` ports to the `lan_tcp`/`lan_udp` sets. `beepboop doctor` fails on any non-loopback listener not declared there (DHCP client sockets `68/udp` and `546/udp` excepted) and on a `loopback_only` socket bound to a LAN address; `deploy/tests/test_deploy.py` keeps `base` and the nftables file in step.
9. **Unattended OS security updates** (`unattended-upgrades`, security pocket only) are on by default. They never touch containers and never reboot; the health page shows "reboot pending". This is the only automatic update on the device (§9).
10. **Data root:** `/srv/beepboop/` on the SSD; contents per directory are in [DATA_AND_PRIVACY §2](DATA_AND_PRIVACY.md#2-storage-layout), plus `deploy/` and `run/` (`host.json`, §5). **Optional LUKS data partition** (`install.sh --luks`) puts `/srv/beepboop` on an encrypted volume so HA's config and credentials, Music Assistant data, the local CA key and transcripts are protected at rest too (beepboop's own secrets and voiceprints are envelope-encrypted regardless). The passphrase must be entered over SSH at every boot, so it is off by default ([SECURITY.md](../SECURITY.md)).

## 3. Compose layout

**This section owns the deploy shape**; other docs link here rather than restating it. The literal file is [`deploy/compose.yaml`](../deploy/compose.yaml) (installed at `/srv/beepboop/deploy/compose.yaml`); it reads digests, memory caps and the enabled profiles from `.env` (table below), and [`deploy/tests/test_deploy.py`](../deploy/tests/test_deploy.py) holds it to the rules below and to the §7 budget. Four services (`caddy`, `core`, `audio`, `homeassistant`) plus the optional `music` profile, the only optional capability in v0.1. Host networking for the three that need the LAN or loopback, **no network at all** for audio. Everything is pinned by image digest; the images are built from [`deploy/docker/`](../deploy/docker/) with their base images pinned by digest too. MQTT brokers, Z-Wave and Matter servers are Home Assistant companions the user runs and manages; beepboop neither ships nor updates them ([ADR 0016](adr/0016-docker-compose-mit.md)).

| Service | Network | Mounts and devices that matter |
|---|---|---|
| `caddy` | host: `:80`, `:443`, `:8443`, `:8445` | Caddyfile (ro), `caddy/data` (holds the install-generated Tier-0 root, §4) |
| `core` | host; binds `127.0.0.1:8080` only | `/data` (`core/`: the database and `keys/`), `/backups`, `/ha`, the KEK passphrase file (ro, from `/etc/beepboop`), `/config/beepboop.yaml` (ro), `/models` (ro, manifest only), `/hostrun` (ro, `host.json`), the Tier-0 root *certificate* (ro), the `bb-run` socket volume; `/dev/vcio` only for the optional OTP KEK |
| `audio` | **none** | `/dev/snd`, `SYS_NICE`, `rtprio 50`, `/models` (ro), `/config/beepboop.yaml` (ro), `/tmp` as tmpfs for ring buffers, `bb-run`; **no writable data mount** (clips are cached in RAM, calibration arrives in `profiles.sync`) |
| `homeassistant` | host; `server_host: 127.0.0.1` | `/srv/beepboop/ha` as `/config` |
| `music` (profile) | host; UI on `127.0.0.1:8095` | `/srv/beepboop/music` |

Rules the file encodes:

- **Audio has `network_mode: none`**: the enforceable version of "audio never leaves the Pi". v0.2 invariant: cloud-speech plugins ship as `deploy/compose.cloud-speech.yaml`, an override that gives audio a network and makes the privacy ledger say so ([ROADMAP.md](ROADMAP.md)).
- **Cores 2–3 belong to audio.** Wake word and the capture thread get real-time priority; STT and speaker ID run on the other core at 2 threads. Core, HA and Caddy share cores 0–1 with HA weighted lowest.
- **Memory limits add up to less than the gate.** 64 + 768 + 768 + 1024 MB = 2.6 GB of hard caps on a 4 GB Pi, leaving 1.1–1.4 GB for the OS, Docker and page cache (less with `music`). The gate is G4.
- **Core must boot without HA.** The fast path, timers and the web UI work while HA restarts; the `ha` module reconnects with backoff.
- **Secrets are not in `.env`** (host, tier, locale, RAM profile, digests only; table below). Credentials live envelope-encrypted in SQLite; `/config/beepboop.yaml` holds what must exist before the DB opens ([ARCHITECTURE §9](ARCHITECTURE.md#9-configuration-model)).
- **No container mounts `docker.sock`.** Anything that needs Docker runs on the host (§5).
- **Core runs as root in its container with every capability dropped** (`cap_drop: [ALL]`, `no-new-privileges`): HA writes its `.storage` files `0600 root`, and backup and restore must read and write them. The KEK passphrase file is `0400 root`.
- **Healthchecks:** core runs `beepboop health --quiet` (GET `/api/health`); audio's capture thread touches `/run/beepboop/audio.heartbeat` every 10 s and the check fails after 30 s; HA answers HTTP on loopback. `beepboop-host.timer` restarts any container Docker reports unhealthy.

**`deploy/.env` keys** (template [`deploy/.env.example`](../deploy/.env.example), written by `install.sh`):

| Key | Default | Notes |
|---|---|---|
| `BB_HOST`, `BB_TIER`, `BB_LOCALE` | `beepboop.local`, `0`, `en-US` | Canonical host (also rendered into HA's managed YAML, [HOME_ASSISTANT §2.1](HOME_ASSISTANT.md#21-loopback-bind-plus-nftables), and used in backup bundle names), HTTPS tier, speech locale |
| `COMPOSE_PROFILES` | empty | `music` or empty. Read by Compose and by `beepboop doctor` (the profile's `ports.yaml` entries) |
| `BB_MEM_CADDY`, `BB_MEM_CORE`, `BB_MEM_AUDIO`, `BB_MEM_HA`, `BB_MEM_MUSIC` | 4 GB profile: `64m`, `768m`, `768m`, `1g`, `512m` | The RAM profile. 8 GB profile: `64m`, `1g`, `1g`, `2g`, `768m` (§7). `BB_MEM_AUDIO` also sets `memswap_limit`. Compose falls back to the 4 GB value when unset. Targets until G4 finalises them |
| `BB_CADDY_DIGEST`, `BB_CORE_DIGEST`, `BB_AUDIO_DIGEST`, `BB_HA_DIGEST`, `BB_MUSIC_DIGEST` | empty | `sha256:<64 hex>`; an empty value makes Compose refuse to start. A release pins them together |

## 4. Caddy

Caddy terminates TLS and routes three things: everything to core, one HA callback path, and admin-only gates to HA's and Music Assistant's own UIs on separate ports. Tier 0 (default) uses a local CA; Tier 1 (v0.2) swaps `tls internal` for ACME ([ADR 0013](adr/0013-https-tiers.md), [WEB_UI §7](WEB_UI.md)).

**Tier-0 root hardening.** The root certificate is generated by `install.sh`, not by Caddy, so it can carry X.509 `nameConstraints` (permitted: `BB_HOST`, the `.local` host, RFC 1918 and ULA ranges). A leaked root key can then mint certificates only for names that point at the house. Only the root *certificate* is mounted into core for the `/ca` helper page; the key never leaves Caddy's volume.

The literal file is [`deploy/caddy/Caddyfile`](../deploy/caddy/Caddyfile); `test_deploy.py` checks its sites and gates.

| Site | Routes |
|---|---|
| `https://$BB_HOST` (`:443`) | `/auth/external/callback*` → HA `127.0.0.1:8123` (HA OAuth flows; HA validates the signed state); everything else → core `127.0.0.1:8080` (SPA, `/api`, `/confirm`; `/mcp` and `/oauth` in v0.2). HSTS, `nosniff`, `Referrer-Policy same-origin`, no `Server` header |
| `http://$BB_HOST` (`:80`) | `/ca*` → core (the read-only "install the beepboop CA" helper, deliberately plain HTTP); everything else redirects to HTTPS |
| `:8443` | HA's own UI as break-glass, behind `forward_auth` to `/api/auth/check?require=admin`; the beepboop cookie is stripped; logged as an admin action |
| `:8445` | Music Assistant's UI (only with the `music` profile), same admin gate |

Caddy renews with ARI when the CA offers it; nothing assumes a certificate lifetime.

## 5. Install script and the host CLI

[`deploy/install.sh`](../deploy/install.sh) is the only installer (POSIX `sh`, `shellcheck`-clean in CI). It is idempotent and prints every step before doing it; users are told to read it first. It runs as root from a release checkout, whose `deploy/release.env` carries the release's image digests (`sudo sh deploy/install.sh`); the one-file `curl`, `less`, `sh` flow waits for the planned `beepboop.dev` release pipeline. Flags: `--dry-run`, `--ram-profile 4g|8g` (auto-detected), `--data-root PATH` (symlinked to `/srv/beepboop`), `--tier 0` (Tier 1 is v0.2), `--profiles music`, `--luks` (the owner opens the LUKS volume and mounts it at the data root first; the installer refuses otherwise), `--force`.

| Step | What it does | Fails if |
|---|---|---|
| 1 Preflight | `aarch64`; model string contains "Raspberry Pi 4"; `MemTotal` ≥ 3.4 GiB; root filesystem on USB, not `mmcblk0`; free disk ≥ 3 GB; `get_throttled` is `0x0`; temperature < 70 °C; `/dev/snd` shows an XVF3800 | Any check, with a one-line explanation and doc link. `--force` lowers all but the architecture check to warnings. Warns when a 4 GB Pi requests any profile other than `music`. |
| 2 Host packages and kernel | Installs §2, enables zram, chrony, avahi, nftables, the watchdog; appends the cgroup flags. Pre-pulls the four images in parallel with apt | apt fails |
| 3 Data root | Creates `/srv/beepboop/*`; generates the KEK passphrase file `/etc/beepboop/kek.passphrase` if absent (32 random bytes, base64, mode 0400 for core's uid; never overwritten, never in a backup; the installer does **not** program the Pi OTP key, D3, [DATA_AND_PRIVACY §6.3](DATA_AND_PRIVACY.md#63-envelope-encryption)); writes `compose.yaml`, `.env`, `Caddyfile`, `ports.yaml`, `config/beepboop.yaml`, HA's managed `configuration.yaml` block rendered with `https://$BB_HOST` ([HOME_ASSISTANT §2.1](HOME_ASSISTANT.md#21-loopback-bind-plus-nftables)), the Tier-0 root (§4), the udev rule, the nftables ruleset, and systemd units `beepboop.service` (`docker compose up -d --remove-orphans`) and `beepboop-host.timer` | — |
| 4 Models | Runs `python -m beepboop_bench.models fetch` from the audio image (host network for this one-off run only): downloads every model in `models/manifest.yaml`, verifies sha256, regenerates `LICENSES.md` | Checksum mismatch |
| 5 Images | `docker compose pull` by digest | Digest mismatch or registry unreachable |
| 6 Up | `systemctl enable --now beepboop`; waits for `core` and `audio` healthchecks only. **The device speaks its claim code as soon as core and audio are healthy**; HA finishes its first boot in the background (up to 5 min) | Core or audio health not reached; prints `docker compose logs --tail 50` |
| 7 Hand-off | Prints `https://$BB_HOST`, the CA helper URL, and "reboot once" if the cgroup flags were just added | — |

**The host CLI.** The installer drops `/usr/local/bin/beepboop`, a shim for [`deploy/host/beepboop_host.py`](../deploy/host/beepboop_host.py) (installed in `/usr/local/lib/beepboop/`; the Pi's own Python 3.11+ with `python3-yaml`, nothing else). It runs the host-only subcommands itself (`doctor`, `update`, `db restore`, `logs`, `models verify|update`) because they need Docker, the host filesystem or a stopped core, and forwards everything else, `claim --reopen` included, into core with `docker compose -f /srv/beepboop/deploy/compose.yaml exec core beepboop "$@"`. The full command list is [CLAUDE.md §9](../CLAUDE.md).

**The host timer.** `beepboop-host.timer` runs `beepboop_host.py host-json` every 10 s. It writes `/srv/beepboop/run/host.json`, mounted read-only into core for the health page: `written_at`, `containers.<name>` (`state`, `health`, `mem_mb`, `mem_limit_mb`, `cpu_pct`), `throttled` (the `get_throttled` hex), `temperature_c`, `disk_free_bytes`, `reboot_pending`, `chrony_offset_s` and `release` (the digests in `.env`). The same run restarts any container Docker reports `unhealthy` and deletes pre-update snapshots older than 7 days. `beepboop update` writes `run/update.json` (`at`, `ok`, `release`, `message`, the last log lines) for `System › Updates`. v0.2 invariant: the one-click UI update works in reverse (core writes a request file; a root path unit `beepboop-update.path` runs `beepboop update`). Core never gains Docker privileges.

## 6. First boot

```mermaid
sequenceDiagram
    participant I as install.sh
    participant C as beepboop-core
    participant A as beepboop-audio
    participant H as homeassistant
    participant U as Admin (phone)
    I->>C: compose up
    C->>C: create /data/beepboop.db; unwrap KEK with the passphrase file (OTP later, optional); generate recovery key
    C->>A: speak{"Hi, I'm beepboop. Open beepboop dot local and enter 4 7 1 9 2 2"}
    A-->>U: speech + LED pattern, repeated every 60 s for 5 minutes, then only on button press
    C->>H: in the background: wait for :8123, POST /api/onboarding/users, core_config, analytics opt-out
    C->>H: WS auth/long_lived_access_token (10 y) -> stored encrypted
    U->>C: https://beepboop.local/claim (code; single-use, 15 min, rate-limited)
    U->>C: wizard (WEB_UI §5.1)
    C->>C: mark onboarded; claim endpoint closes
```

The wizard's steps and screens are owned by [WEB_UI §5.1](WEB_UI.md#51-first-run-onboarding). Points that matter operationally:

- **The claim code is spoken as soon as core and audio are healthy**, before HA has finished its first boot. It repeats every 60 s for 5 minutes; after that the button speaks it again. A quiet device is not a broken device.
- **Restore is the first question**, not the last. A replaced Pi should be back to its old self before anyone enrols again (§10).
- **HA is never shown.** The random owner password stays in beepboop's secret store as break-glass; admins can reveal it from `System › Health` (Home Assistant card), and the reveal is audited.
- **Skipping Bedrock is allowed.** The device works offline on the fast path until credentials are added in `System › LLM`; the health page says so plainly.
- **Re-entering claim mode** without wiping data: hold the GPIO button 10 s, or `beepboop claim --reopen` on the host. The claim session then accepts only an **admin recovery code** (forcing a password and TOTP reset) or **Factory reset**; it never creates a second admin or bypasses the existing one. Both paths are audited.

## 7. Resource budget

All figures are **targets to be validated in M0** under concurrent load with HA running. "Typical" is steady state; "limit" is the cgroup hard cap.

| Service | RAM typical (target) | RAM limit 4 GB / 8 GB | CPU placement | Notes |
|---|---|---|---|---|
| OS + Docker + page cache | 300–450 MB | — | — | zram absorbs spikes; swapping audio is a failure, not a mitigation (`memswap_limit`) |
| `caddy` | 20–40 MB | 64 m / 64 m | cores 0–1 | — |
| `beepboop-core` | 250–400 MB | 768 m / 1 g | cores 0–1, `cpu_shares` 1024 | FastAPI, agent loop, registry, SQLite, boto3. No model weights. |
| `beepboop-audio` | 400–650 MB | 768 m / 1 g | cores 2–3, nice −5, RT capture thread | microWakeWord (~1 MB), Silero VAD (~2 MB), CAM++ (29 MB), streaming zipformer small (~100–150 MB with buffers), one Piper voice (~60 MB), working sets |
| `homeassistant` | 300–600 MB | 1 g / 2 g | cores 0–1, `cpu_shares` 512 | Grows with integrations; recorder `purge_keep_days: 3`, `commit_interval: 30`, noisy entities excluded by the `ha` module's default recorder config |
| **Default total** | **≈1.3–2.1 GB** | **2.6 GB caps** | | **Gate G4** |
| `music` (profile) | 150–300 MB | 512 m / 768 m | cores 0–1 | Music Assistant |
| **Default + `music`** | **≈1.5–2.4 GB** | **3.1 GB caps** | | The most a 4 GB Pi supports; G4 runs with and without it |

CPU under load during a turn (target): wake word idle ≤ 5 % of one core; streaming STT ≤ 1.2 cores while speaking; speaker ID a 60–130 ms burst at 2 threads; TTS ≤ 1 core; HA idle 2–8 % with bursts on state storms. `System › Health` shows per-container CPU and memory from `host.json` and the throttling flags from §8.

## 8. Thermals

A Pi 4 soft-throttles at 80 °C and hard-throttles at 85 °C; throttling directly inflates STT and TTS latency, which is why the fan is a requirement.

- **Health page fields:** SoC temperature, current and historical `get_throttled` bits (0x1 under-voltage, 0x2 ARM frequency capped, 0x4 currently throttled, 0x8 soft temperature limit; bits 16–19 "has happened since boot"), fan state if exposed.
- **Targets:** idle < 55 °C, sustained `beepboop bench e2e` < 70 °C; the pass/fail rule is gate G7 in [ROADMAP.md](ROADMAP.md).
- **Alerts:** a soft-limit event posts a UI notification ("beepboop got hot at 14:02; check the fan"); repeated under-voltage posts "replace the power supply".
- **Degradation** under throttling follows the ladder in [VOICE_PIPELINE §14.3](VOICE_PIPELINE.md#143-degradation-ladder); affected turns are tagged in their trace.

## 9. Updates

Rules: **notify-only by default, one mechanism for applying, never silent.** A device that can unlock doors must not change its behaviour because a registry published a new tag.

1. **Check (automatic, daily, read-only).** Core's `updates.check` job (04:30 local, after the backup window) compares the digests in the release manifest named by `BB_UPDATE_MANIFEST` (a file the admin vetted, or `https://beepboop.dev/releases/<channel>.json`, planned domain) with the running `BB_*_DIGEST` values compose passes into the core container from `.env`. The manifest is the same JSON `beepboop update --manifest` reads (`release`, `digests`, `changelog_url`, `schema_version`). A difference shows on System › Updates, in `/api/health/detail` as `update_available`, and, if the admin opted in, as one spoken line. Nothing is pulled; a missing or malformed feed reads as "unknown", never as an update.
2. **Release manifest.** A release pins all four images together, including the vetted HA digest; HA is never updated independently, because the `ha` module's contract tests run against that exact version ([HOME_ASSISTANT §13](HOME_ASSISTANT.md#13-upgrade-strategy-and-bridge-contract-tests)).
3. **Apply (admin-initiated).** `beepboop update` on the host in v0.1; a one-click `System › Updates` button is a v0.2 invariant (same host-side procedure via `beepboop-update.path`, §5):

```
preflight  free disk >= 3 GB; no reboot pending; chrony synced
snapshot   pre-update snapshot per DATA_AND_PRIVACY §11: VACUUM INTO /srv/beepboop/backups/pre-update-<ts>/;
           stop HA; copy /srv/beepboop/ha/.storage and configuration.yaml; move home-assistant_v2.db aside
           (recorder history is disposable); copy .env + compose.yaml
pull       docker compose pull   (digests from the signed manifest)
switch     write new digests to .env; docker compose up -d
verify     core and audio within 180 s: /api/health ok, audio heartbeat healthy, synthetic text turn
           beepboop chat --as guest --dry-run "what time is it" returns; HA healthy within 420 s
rollback   on any failure: restore previous .env; docker compose up -d; if core ran a DB migration, restore the
           DB snapshot; if the HA digest changed, restore the HA snapshot;
           then write "the update failed and I rolled back" and the logs to run/update.json
cleanup    on success: remove images not named by the new or the previous .env; drop older snapshots
```

4. **Downgrade** is `beepboop update --to <release>`, same procedure. Migrations are forward-only, so when the release manifest's `schema_version` is below the running schema, the newest pre-update snapshot at that schema is restored (each snapshot records its `schema_version`) and the update says so; with no such snapshot it refuses and points at `beepboop db restore`.

**Pending, not built in v0.1:** the release manifest's signature (the signing scheme is not chosen; until then `--manifest <file>` names a manifest the admin vetted), the spoken "update complete" and "rolled back" lines (core announces `run/update.json` once the audio bridge's `speak` path exists), and HA WebSocket auth as an update gate (core reports it on the health page; the host sees container health only).
5. **Not offered:** auto-apply, auto-approve for "security only", or scheduled updates without a human. An admin can cron `beepboop update` if they truly want that; the project does not ship the option.
6. **OS packages** (§2 step 9) are the one exception, limited to Debian's security pocket; they never restart containers.

## 10. Backups and restore

The operator's view; bundle contents, schedule, retention, encryption and the recovery key are owned by [DATA_AND_PRIVACY §11](DATA_AND_PRIVACY.md#11-backups).

- **Where they go.** Encrypted bundles land in `/srv/beepboop/backups/` on the SSD by default. Until a second destination exists, `System › Backups` and the health page warn *Backups are only on this disk*. `System › Backups` accepts a USB path or a mounted SMB share (credentials stored encrypted); object storage is v0.2.
- **Restore on a new Pi.** Flash, install, and answer *Restore from backup?* at the claim step: upload the bundle or point at the USB path, type the recovery phrase, and the device comes back with its people, grants, voiceprints and HA configuration.
- **Restore on the same Pi.** `beepboop db restore <file> [--identity <keyfile>]` on the host stops core and HA, runs core's own `beepboop db restore` in a one-off core container (the recovery key goes in on stdin, never on a command line), starts the stack and runs the same verify as updates. The previous database is kept in `backups/pre-restore-<ts>/`, so a bad restore is reversible.
- **Lost the recovery phrase?** Existing backups cannot be restored on a new Pi. Generate a new recovery key in `System › Backups`; the next backup is usable again.

Restore onto a second Pi is tested on x86 in `core/tests/db/test_backup.py` (different KEK and device salt; people, grants, voiceprints, secrets and HA config come back). Restoring the previous release's backup fixture into the current release joins CI with the first release; the run on two real Pis is pending (ROADMAP v0.1 #9).

## 11. Observability

**Logs.** Every service logs `structlog` JSON to stdout, collected by Docker's `json-file` driver (§2). Every line carries `service`, `level`, `event`, and inside a turn `turn_id` and `profile_id`. The audio service never logs transcripts or audio; core logs transcripts only at `debug`, which is off by default and flagged in the privacy ledger when on. `beepboop logs --turn <id>` joins both containers' lines for one turn.

**Per-turn traces.** Each turn is a row in `turns` with `stages_ms_json`, written in a batch after the turn ends. Clock zero for every stage is the **physical end of the user's speech** (the last VAD-positive frame), so the VAD hangover appears as its own `endpoint` stage inside the budget ([ADR 0015](adr/0015-latency-contract-and-fast-path.md)). The stage names below are fixed so the UI, `beepboop bench` and CI compare like with like; this table owns them.

| Stage | Measured from → to |
|---|---|
| `wake` | wake hit → `wake` event delivered to core |
| `speech` | wake → last VAD-positive frame |
| `endpoint` | last VAD-positive frame → VAD decides end-of-speech (the hangover) |
| `stt_tail` | last VAD-positive frame → `stt.final` |
| `speaker_final` | last VAD-positive frame → `speaker.final` (negative when it finished earlier) |
| `fastpath` | `stt.final` → fast-path decision |
| `ctx` | prompt assembly |
| `llm_ttft` | request sent → first token |
| `llm_first_sentence` | request sent → first sentence boundary |
| `tool.<name>` | one span per tool call, with `risk`, `decision`, `factor` |
| `tts_first_chunk` | first sentence → first PCM chunk |
| `first_audio` | last VAD-positive frame → first sample at the DAC (earcon or speech) |
| `first_speech` | last VAD-positive frame → first spoken word |

A `tool.<name>` span is an object `{ms, risk, decision, factor?}` (a second call to the same tool in one turn is `tool.<name>#2`). Besides the stages, `stages_ms_json` carries three non-timing keys the renderer skips: `rung` (the ladder rung that answered), `prefix_hash` (the first 16 hex characters of the cached prefix's hash) and `cost_estimate` (true when the cost is estimated rather than reported).

**Latency waterfall.** Activity renders `stages_ms_json` as a waterfall for every turn a user may see (their own; admins see all); the same renderer prints in the terminal (`beepboop logs --turn <id>`), one bar per stage with `risk`/`decision` on tool spans and the earcon marked against its 1 s target.

**`beepboop bench`** lives in `tools/bench/` and runs inside the audio and core containers against a fixture corpus. What each subcommand reports, which gate reads it, and the flags are owned by [tools/bench/README.md](../tools/bench/README.md#what-each-subcommand-reports); **gate thresholds live in one place, the M0 table in [ROADMAP.md](ROADMAP.md)**.

**Health endpoint.** `GET /api/health` (no auth for a bare `ok`; details need an admin session) returns per-service status, audio device present, HA connected, Bedrock reachable and last TTFT, last backup result and destination, cert expiry (the Tier-0 root), pending update, the last 24 h p50/p95 per stage, everything `host.json` carries, and a `needs_attention` list. The model is `HealthReport` in `core/beepboop/health.py` (`collect_health`); an unknown value is `null`, never a guess. OpenTelemetry export is an optional sink, off by default.

## 12. Troubleshooting runbook

Each entry: symptom → where to look → likely causes in order → fix. The health page and `beepboop doctor` surface the first checks automatically.

### No wake ("Hey beepboop" does nothing)

1. `System › Voice` shows whether the array is detected and capturing. Nothing: `docker exec beepboop-audio arecord -l` must list `XVF3800`; check the USB cable, the mute switch, and `dmesg` for USB resets (power supply, or an SSD drawing too much, §1).
2. Capturing but no wake: wrong firmware variant; `beepboop doctor` compares the reported firmware with `deploy/firmware.lock`.
3. Wake model not loaded: audio logs show `wake.model_loaded` at start; a checksum failure means `beepboop models verify`.
4. Threshold too strict: `System › Voice` wake sensitivity; `bench wake` shows the trade-off.
5. CPU starvation: throttle bits, or something else on cores 2–3 (`docker stats`).

### Wrong person (Alex is taken for Sam, or everyone is Guest)

1. `Activity` → the turn shows `score`, `margin` and the runner-up. Low score for everyone: enrolment was done in a different room or position; re-enrol at two or three positions.
2. Two people with small margins: `People › <name> › Voice` shows the household similarity check. Add clips for both; personal requests from either ask for approval when the margin is thin.
3. A child's score drifting down over weeks is expected; add fresh clips. Automatic re-enrol prompts are v0.2.
4. Use "that wasn't me" on the turn; it adds a negative example and an audit entry, and never changes what already happened.
5. Speaker model changed after an update: the health page says "re-enrolment needed" and `People › <name> › Voice` offers *Re-enrol*. No audio is kept, so there is nothing to re-embed from ([DATA_AND_PRIVACY §1](DATA_AND_PRIVACY.md#1-principles)).

### Slow replies

1. Open the turn's waterfall; the widest bar names the culprit.
2. `llm_ttft` > 900 ms: cache miss (`cache_read_tokens: 0`, or the cache expired after a quiet spell), cold TLS (pre-warm did not fire), Wi-Fi contention, or a regional endpoint. `bench llm` isolates it.
3. `stt_tail` or `tts_first_chunk` inflated: throttling (§8), swap in use (`free -m`; audio must never swap), HA recorder storm, or a module blocking the event loop (core logs `loop.lag_ms`).
4. `endpoint` long: VAD hangover too long for a noisy room; `System › Voice` endpointing.
5. Fast path missing obvious commands: the entity does not resolve for this profile (default-deny new entities; Integrations › Home Assistant › New devices) or the grant is scheduled off.

### Home Assistant unreachable

1. `docker compose ps`: first start after an update can take minutes (§9 allows 420 s). Core keeps working and says "the home is restarting" for device commands.
2. Healthy but core shows `ha.disconnected`: the long-lived token was revoked or `.storage` was restored from an older backup. `System › Health` (Home Assistant card) → *Re-pair Home Assistant*.
3. HA reachable from the LAN on `:8123`: the nftables ruleset is missing or `server_host` was edited; `beepboop doctor` flags both.
4. HA start failure after a release: `docker compose logs homeassistant`; roll back with `beepboop update --to <previous>`.

### Bedrock

The error mapping and the operator's checks for `AccessDeniedException`, `ValidationException`, signature or `ExpiredToken` errors and throttling are owned by [LLM_PROVIDERS §3](LLM_PROVIDERS.md#3-bedrock-adapter-providersllmbedrock); `System › LLM` links to them. Clock trouble is `chronyc tracking` (§2). With no network the device says so within 1 s and continues on the fast path.

### Also common

| Symptom | Fix |
|---|---|
| Browser says the certificate is not trusted | Tier 0 requires installing the device CA once per phone/laptop: `http://beepboop.local/ca` has per-OS steps. |
| "Device busy" in audio logs | A host process grabbed the array; `fuser -v /dev/snd/*` finds it. |
| Lost admin password and TOTP | Hold the button 10 s or `beepboop claim --reopen`; enter an admin recovery code (§6). Data is kept. |
| Lost the recovery phrase | See §10. |
| Approvals banner never appears for a `critical` action | The web session must belong to the profile the voice turn resolved to; a Guest-resolved turn cannot be approved by anyone. Check the turn's profile in `Activity`. |
| `beepboop doctor` says "memory limits not enforced" | The cgroup flags are missing from `cmdline.txt` (§2 step 4); re-run `install.sh` and reboot. |
| `beepboop doctor` lists an unexpected listening port | Something on the host (or a user-managed HA companion) opened a LAN socket not declared in `deploy/ports.yaml`. Add it with a reason, or stop it. |

## 13. Model and licensing hygiene

beepboop is GPL-3.0-or-later ([LICENSE](../LICENSE), [ADR 0020](adr/0020-gpl-3-and-espeak-phonemiser.md)), and several of the best speech models carry other licences. The rule is **downloaded, not vendored**: the repository contains manifests and license records, never weights. CC BY attributions are collected in [`NOTICE`](../NOTICE).

- [`models/manifest.yaml`](../models/manifest.yaml) is the single source of truth: one entry per artifact with its `url`, `sha256`, `license` (SPDX where possible), `attribution` and install keys. The schema is `ModelEntry` in `tools/bench/beepboop_bench/models.py`, which owns the field list. The installer, `beepboop models verify|update` (both run that module from the audio image) and CI all read it.
- `models/LICENSES.md` is **generated** from the manifest by `python -m beepboop_bench.models licenses` and checked in; `licenses --check` (CI, every PR) fails if it is stale or a licence is missing, `UNKNOWN` or non-commercial (`*-NC-*`), or is neither on the GPL-3.0-compatible allow-list (permissive, CC0, CC BY, MPL-2.0, LGPL, GPL-2.0-or-later, GPL-3.0) nor a research-accepted licence the owner ruled on. GPL-incompatible licences (proprietary, GPL-2.0-only, ND) are refused because they are not on the list. Module licence notes live in `modules/<id>/LICENSES.md` when a module bundles data; CI aggregates them into the release notes.
- Research-accepted, by owner ruling: `LicenseRef-Blizzard-2013-Lessac` (D1, 2026-11-03). Most English Piper voices, including the default `en_US-lessac-low`, and the LibriTTS-R sample generator used to train the wake word descend from the Blizzard 2013 Lessac corpus, which is licensed for research only. They are accepted for this personal/research project; a commercial build must swap the voice and train the wake word with the strict profile ([tools/train_wakeword/README.md §7.2](../tools/train_wakeword/README.md#72-the-lessac-provenance-accepted-by-the-owner-d1)).
- The entries and their licences are listed in the generated [`models/LICENSES.md`](../models/LICENSES.md). openWakeWord pretrained models are CC BY-NC-SA and therefore plugin-only, never downloaded by default. Moonshine and Parakeet stay out until their licenses are verified.
- The runtime is sherpa-onnx (Apache-2.0) for VAD, speaker ID, STT and TTS, plus `piper-phonemize` over espeak-ng (GPL-3.0) as the TTS phonemiser in the audio process, reading each voice's `espeak-ng-data` ([VOICE_PIPELINE §11.1](VOICE_PIPELINE.md#111-default-piper-voices-in-sherpa-onnx-sherpa-piper), [ADR 0020](adr/0020-gpl-3-and-espeak-phonemiser.md)).
- Home Assistant is a trademark of the Open Home Foundation. beepboop "works with Home Assistant"; it is not branded as or bundled with it, and the HA image is pulled unmodified ([ADR 0016](adr/0016-docker-compose-mit.md)).
- A model swap is an explicit `beepboop models update` on the host that re-verifies checksums and, for speaker models, puts every voiceprint into "re-enrolment needed" (§12).
