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

   **`deploy/ports.yaml` schema** (`schema: 1`): four lists, `base`, `profiles.<profile>` (one list per Compose profile), `user` (ports of user-managed HA companions or discovery protocols) and `loopback_only` (sockets that must never bind a LAN address; `doctor` fails if one does). Every entry is `{port, proto: tcp|udp, service, reason}` and the reason is mandatory. `base` is written literally in the nftables file; `install.sh` adds the enabled profiles' and the `user` ports to its `lan_tcp`/`lan_udp` sets. `beepboop doctor` and `deploy/tests/test_deploy.py` read the file, and the test keeps `base` and the nftables file in step.
9. **Unattended OS security updates** (`unattended-upgrades`, security pocket only) are on by default. They never touch containers and never reboot; the health page shows "reboot pending". This is the only automatic update on the device (§9).
10. **Data root:** `/srv/beepboop/` on the SSD; contents per directory are in [DATA_AND_PRIVACY §2](DATA_AND_PRIVACY.md#2-storage-layout), plus `deploy/` and `run/` (`host.json`, §5). **Optional LUKS data partition** (`install.sh --luks`) puts `/srv/beepboop` on an encrypted volume so HA's config and credentials, Music Assistant data, the local CA key and transcripts are protected at rest too (beepboop's own secrets and voiceprints are envelope-encrypted regardless). The passphrase must be entered over SSH at every boot, so it is off by default ([SECURITY.md](../SECURITY.md)).

## 3. Compose layout

**This section is the canonical deploy shape**; other docs link here rather than restating it. The sketch below shows the shape with placeholders and the 4 GB values; the literal file is [`deploy/compose.yaml`](../deploy/compose.yaml), which reads digests and memory caps from `.env` (table below) and must match this sketch. Four services, host networking for the three that need the LAN or loopback, **no network at all** for audio. Everything is pinned by image digest. The only optional capability in v0.1 is the `music` profile. MQTT brokers, Z-Wave and Matter servers are Home Assistant companions the user runs and manages; beepboop neither ships nor updates them ([ADR 0016](adr/0016-docker-compose-mit.md)).

```yaml
# /srv/beepboop/deploy/compose.yaml  (sketch; digests and the HA tag are placeholders until a release pins them)
name: beepboop

x-logging: &logging
  driver: json-file
  options: { max-size: "20m", max-file: "5" }

services:
  caddy:
    image: ghcr.io/jaminb/beepboop-caddy@sha256:<digest>   # caddy:2 (+ dns module for Tier 1)
    network_mode: host               # :80 CA helper + redirect, :443 UI/API/MCP, :8443 HA gate, :8445 Music Assistant gate
    volumes:
      - /srv/beepboop/deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - /srv/beepboop/caddy/data:/data             # holds the install.sh-generated Tier-0 root (§4)
      - /srv/beepboop/caddy/config:/config
    environment:
      BB_HOST: ${BB_HOST:-beepboop.local}
      BB_TIER: ${BB_TIER:-0}
    mem_limit: 64m
    cpuset: "0-1"
    restart: unless-stopped
    logging: *logging

  core:
    image: ghcr.io/jaminb/beepboop-core@sha256:<digest>
    container_name: beepboop-core
    network_mode: host               # binds 127.0.0.1:8080 only; talks to HA at 127.0.0.1:8123
    environment:
      BB_DATA: /data
      BB_AUDIO_SOCKET: /run/beepboop/audio.sock
      BB_HA_URL: ws://127.0.0.1:8123/api/websocket
      BB_PUBLIC_URL: https://${BB_HOST:-beepboop.local}
      BB_KEK_PASSPHRASE_FILE: /run/secrets/kek_passphrase            # v0.1 KEK source (DATA_AND_PRIVACY §6.3)
    volumes:
      - /etc/beepboop/kek.passphrase:/run/secrets/kek_passphrase:ro   # host-only file, outside the data root
      - /srv/beepboop/config/beepboop.yaml:/config/beepboop.yaml:ro   # household config file
      - /srv/beepboop/core:/data                                      # beepboop.db + WAL
      - /srv/beepboop/backups:/backups                                # VACUUM INTO target (§10)
      - /srv/beepboop/ha:/ha                                          # first-boot configuration.yaml, HA snapshot during updates
      - /srv/beepboop/caddy/data/caddy/pki/authorities/local/root.crt:/caddy/root.crt:ro   # root cert only, for /ca
      - /srv/beepboop/models:/models:ro                               # manifest + LICENSES only; core never loads weights
      - /srv/beepboop/run:/hostrun:ro                                 # host.json from beepboop-host.timer (§5)
      - bb-run:/run/beepboop
    devices: ["/dev/vcio:/dev/vcio"]             # only for the optional OTP KEK upgrade; v0.1 uses the passphrase file (DATA_AND_PRIVACY §6.3)
    group_add: ["video"]
    mem_limit: 768m                              # 1g on 8 GB
    cpuset: "0-1"
    healthcheck:
      test: ["CMD", "beepboop", "health", "--quiet"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 90s
    depends_on:
      homeassistant: { condition: service_started }   # core tolerates HA being down; it must not block boot
    restart: unless-stopped
    logging: *logging

  audio:
    image: ghcr.io/jaminb/beepboop-audio@sha256:<digest>
    container_name: beepboop-audio
    network_mode: none               # PCM never leaves the process. Only text events cross the socket.
    devices: ["/dev/snd:/dev/snd"]
    group_add: ["audio"]
    cap_add: ["SYS_NICE"]            # os.nice(-5); SCHED_FIFO for the ALSA capture thread
    ulimits: { rtprio: 50, memlock: -1 }
    volumes:
      - /srv/beepboop/config/beepboop.yaml:/config/beepboop.yaml:ro   # provider selection reaches audio here, never as an event
      - /srv/beepboop/models:/models:ro
      - /srv/beepboop/audio:/data                # TTS clip cache, calibration; never captured audio
      - bb-run:/run/beepboop
    tmpfs: ["/tmp:size=64m"]                     # ring buffers live here, never on disk
    environment:
      BB_AUDIO_SOCKET: /run/beepboop/audio.sock
      BB_ALSA_DEVICE: hw:XVF3800
      BB_LOCALE: ${BB_LOCALE:-en-US}
      OMP_NUM_THREADS: "2"
    mem_limit: 768m                              # 1g on 8 GB
    memswap_limit: 768m                          # equal to mem_limit: audio never swaps
    cpuset: "2-3"
    oom_score_adj: -500                          # the OOM killer takes HA before it takes audio
    healthcheck:                                 # the capture thread touches the heartbeat file every 10 s
      # $$ escapes Compose interpolation; the container's sh sees a single $.
      test: ["CMD", "sh", "-c", "test $$(( $$(date +%s) - $$(stat -c %Y /run/beepboop/audio.heartbeat) )) -lt 30"]
      interval: 15s
      timeout: 3s
      retries: 2
      start_period: 60s
    restart: unless-stopped                      # beepboop-host.timer restarts any container Docker reports unhealthy
    logging: *logging

  homeassistant:
    image: ghcr.io/home-assistant/home-assistant@sha256:<digest>   # vetted 2026.10.x
    container_name: homeassistant
    network_mode: host               # discovery needs it; http is bound to 127.0.0.1 and the input chain drops :8123 from the LAN
    volumes:
      - /srv/beepboop/ha:/config
      - /etc/localtime:/etc/localtime:ro
      - /run/dbus:/run/dbus:ro       # Bluetooth integrations, optional
    mem_limit: 1g                    # 2g on 8 GB; final cap decided by the M0 numbers
    cpuset: "0-1"
    cpu_shares: 512                  # below core (1024) and audio (default on its own cores)
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:8123/"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 120s
    restart: unless-stopped
    logging: *logging

  # ---------- the one optional profile in v0.1: `docker compose --profile music up -d` ----------
  music:
    profiles: ["music"]              # required by the v0.1 `music` module
    image: ghcr.io/music-assistant/server@sha256:<digest>
    network_mode: host               # web UI bound to 127.0.0.1:8095; reached via Caddy :8445 (§4). Stream port 8097 is in deploy/ports.yaml
    volumes: ["/srv/beepboop/music:/data"]
    mem_limit: 512m
    cpuset: "0-1"
    restart: unless-stopped
    logging: *logging

volumes:
  bb-run:
    driver: local
    driver_opts: { type: tmpfs, device: tmpfs, o: "size=4m,mode=0770" }
```

Rules the sketch encodes:

- **Audio has `network_mode: none`**: the enforceable version of "audio never leaves the Pi". v0.2 invariant: cloud-speech plugins ship as `deploy/compose.cloud-speech.yaml`, an override that gives audio a network and makes the privacy ledger say so ([ROADMAP.md](ROADMAP.md)).
- **Cores 2–3 belong to audio.** Wake word and the capture thread get real-time priority; STT and speaker ID run on the other core at 2 threads. Core, HA and Caddy share cores 0–1 with HA weighted lowest.
- **Memory limits add up to less than the gate.** 64 + 768 + 768 + 1024 MB = 2.6 GB of hard caps on a 4 GB Pi, leaving 1.1–1.4 GB for the OS, Docker and page cache (less with `music`). The gate is G4.
- **Core must boot without HA.** The fast path, timers and the web UI work while HA restarts; the `ha` module reconnects with backoff.
- **Secrets are not in `.env`** (host, tier, locale, RAM profile, digests only; table below). Credentials live envelope-encrypted in SQLite; `/config/beepboop.yaml` holds what must exist before the DB opens ([ARCHITECTURE §9](ARCHITECTURE.md#9-configuration-model)).
- **No container mounts `docker.sock`.** Anything that needs Docker runs on the host (§5).

**`deploy/.env` keys** (template [`deploy/.env.example`](../deploy/.env.example), written by `install.sh`):

| Key | Default | Notes |
|---|---|---|
| `BB_HOST`, `BB_TIER`, `BB_LOCALE` | `beepboop.local`, `0`, `en-US` | Canonical host (also rendered into HA's managed YAML, [HOME_ASSISTANT §2.1](HOME_ASSISTANT.md#21-loopback-bind-plus-nftables)), HTTPS tier, speech locale |
| `BB_MEM_CADDY`, `BB_MEM_CORE`, `BB_MEM_AUDIO`, `BB_MEM_HA`, `BB_MEM_MUSIC` | 4 GB profile: `64m`, `768m`, `768m`, `1g`, `512m` | The RAM profile. 8 GB profile: `64m`, `1g`, `1g`, `2g`, `768m` (§7). `BB_MEM_AUDIO` also sets `memswap_limit`. Compose falls back to the 4 GB value when unset. Targets until G4 finalises them |
| `BB_CADDY_DIGEST`, `BB_CORE_DIGEST`, `BB_AUDIO_DIGEST`, `BB_HA_DIGEST`, `BB_MUSIC_DIGEST` | empty | `sha256:<64 hex>`; an empty value makes Compose refuse to start. A release pins them together |

## 4. Caddy

Caddy terminates TLS and routes three things: everything to core, one HA callback path, and admin-only gates to HA's and Music Assistant's own UIs on separate ports. Tier 0 (default) uses a local CA; Tier 1 (v0.2) swaps `tls internal` for ACME ([ADR 0013](adr/0013-https-tiers.md), [WEB_UI §7](WEB_UI.md)).

**Tier-0 root hardening.** The root certificate is generated by `install.sh`, not by Caddy, so it can carry X.509 `nameConstraints` (permitted: `BB_HOST`, the `.local` host, RFC 1918 and ULA ranges). A leaked root key can then mint certificates only for names that point at the house. Only the root *certificate* is mounted into core for the `/ca` helper page; the key never leaves Caddy's volume.

```caddyfile
# /srv/beepboop/deploy/caddy/Caddyfile (sketch)
{
    pki {
        ca local {
            root {
                cert /data/caddy/pki/authorities/local/root.crt    # generated by install.sh with nameConstraints
                key  /data/caddy/pki/authorities/local/root.key
            }
        }
    }
    # Tier 1 (v0.2) with your own domain:  acme_dns <provider> {$DNS_API_TOKEN}; with Tailscale, nothing to add.
}

(hardening) {
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options nosniff
        Referrer-Policy same-origin
        -Server
    }
    encode zstd gzip
}

(admin_gate) {
    forward_auth 127.0.0.1:8080 {                # only a beepboop admin web session passes
        uri /api/auth/check?require=admin
        copy_headers X-BB-Profile
    }
}

{$BB_HOST} {
    import hardening
    tls internal                                 # Tier 0 only

    @ha_callback path /auth/external/callback*  # HA OAuth flows bounce here via my.home-assistant.io; HA validates signed state
    handle @ha_callback {
        reverse_proxy 127.0.0.1:8123
    }
    handle {
        reverse_proxy 127.0.0.1:8080             # SPA, /api, /confirm; /mcp and /oauth in v0.2
    }
}

http://{$BB_HOST} {
    handle /ca* {
        reverse_proxy 127.0.0.1:8080             # "install the beepboop CA" helper, deliberately plain HTTP, read-only
    }
    handle {
        redir https://{host}{uri} permanent
    }
}

{$BB_HOST}:8443 {                                # Home Assistant's own UI as break-glass; logged as an admin action
    import hardening
    tls internal
    import admin_gate
    reverse_proxy 127.0.0.1:8123 {
        header_up -Cookie                        # the beepboop session cookie never reaches HA
    }
}

{$BB_HOST}:8445 {                                # Music Assistant's UI, only when the `music` profile is on
    import hardening
    tls internal
    import admin_gate
    reverse_proxy 127.0.0.1:8095 {
        header_up -Cookie
    }
}
```

Caddy renews with ARI when the CA offers it; nothing assumes a certificate lifetime.

## 5. Install script and the host CLI

`deploy/install.sh` is the only installer. It is idempotent and prints every step before doing it; users are told to read it first (`curl -fsSLO https://beepboop.dev/install.sh && less install.sh && sh install.sh`, planned domain). Flags: `--dry-run`, `--ram-profile 4g|8g` (auto-detected), `--data-root PATH`, `--tier 0|1`, `--profiles music`, `--luks`.

| Step | What it does | Fails if |
|---|---|---|
| 1 Preflight | `aarch64`; model string contains "Raspberry Pi 4"; `MemTotal` ≥ 3.4 GiB; root filesystem on USB, not `mmcblk0`; free disk ≥ 3 GB; `get_throttled` is `0x0`; temperature < 70 °C; `/dev/snd` shows an XVF3800 | Any check, with a one-line explanation and doc link. `--force` lowers all but the architecture check to warnings. Warns when a 4 GB Pi requests any profile other than `music`. |
| 2 Host packages and kernel | Installs §2, enables zram, chrony, avahi, nftables, the watchdog; appends the cgroup flags. Pre-pulls the four images in parallel with apt | apt fails |
| 3 Data root | Creates `/srv/beepboop/*`; generates the KEK passphrase file `/etc/beepboop/kek.passphrase` if absent (32 random bytes, base64, mode 0400 for core's uid; never overwritten, never in a backup; the installer does **not** program the Pi OTP key, D3, [DATA_AND_PRIVACY §6.3](DATA_AND_PRIVACY.md#63-envelope-encryption)); writes `compose.yaml`, `.env`, `Caddyfile`, `ports.yaml`, `config/beepboop.yaml`, HA's managed `configuration.yaml` block rendered with `https://$BB_HOST` ([HOME_ASSISTANT §2.1](HOME_ASSISTANT.md#21-loopback-bind-plus-nftables)), the Tier-0 root (§4), the udev rule, the nftables ruleset, and systemd units `beepboop.service` (`docker compose up -d --remove-orphans`) and `beepboop-host.timer` | — |
| 4 Models | Downloads every model in `models/manifest.yaml`, verifies sha256, writes `LICENSES.md` | Checksum mismatch |
| 5 Images | `docker compose pull` by digest | Digest mismatch or registry unreachable |
| 6 Up | `systemctl enable --now beepboop`; waits for `core` and `audio` healthchecks only. **The device speaks its claim code as soon as core and audio are healthy**; HA finishes its first boot in the background (up to 5 min) | Core or audio health not reached; prints `docker compose logs --tail 50` |
| 7 Hand-off | Prints `https://beepboop.local` and the CA helper URL | — |

**The host CLI.** The installer drops `/usr/local/bin/beepboop`, a host script. It runs the host-only subcommands itself (`doctor`, `update`, `db restore`, `logs`, `claim --reopen`, `models verify|update`) because they need Docker, the host filesystem or a stopped core, and forwards everything else into core with `docker compose -f /srv/beepboop/deploy/compose.yaml exec core beepboop "$@"`. The full command list is [CLAUDE.md §9](../CLAUDE.md).

**The host timer.** `beepboop-host.timer` runs a script every 10 s that writes `/srv/beepboop/run/host.json` (per-service memory and CPU, `get_throttled` bits, temperature, disk free, reboot-pending, chrony offset, container health), mounted read-only into core for the health page; the same script restarts any container Docker reports `unhealthy`. v0.2 invariant: the one-click UI update works in reverse (core writes a request file; a root path unit `beepboop-update.path` runs `beepboop update`). Core never gains Docker privileges.

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

1. **Check (automatic, daily, read-only).** Core's `updates` task compares the digests in the signed release manifest (`https://beepboop.dev/releases/<channel>.json`, planned domain) with `.env`. A difference produces a UI notification and, if the admin opted in, one spoken line. Nothing is pulled.
2. **Release manifest.** A release pins all four images together, including the vetted HA digest; HA is never updated independently, because the `ha` module's contract tests run against that exact version ([HOME_ASSISTANT §13](HOME_ASSISTANT.md#13-upgrade-strategy-and-bridge-contract-tests)).
3. **Apply (admin-initiated).** `beepboop update` on the host in v0.1; a one-click `System › Updates` button is a v0.2 invariant (same host-side procedure via `beepboop-update.path`, §5):

```
preflight  free disk >= 3 GB; no reboot pending; chrony synced
snapshot   pre-update snapshot per DATA_AND_PRIVACY §11: VACUUM INTO /srv/beepboop/backups/pre-update-<ts>/;
           stop HA; copy /srv/beepboop/ha/.storage and configuration.yaml; move home-assistant_v2.db aside
           (recorder history is disposable); copy .env + compose.yaml
pull       docker compose pull   (digests from the signed manifest)
switch     write new digests to .env; docker compose up -d
verify     core and audio within 180 s: /api/health ok, audio heartbeat, synthetic text turn "what time is it"
           through the fast path returns, TTS of "update complete" plays; HA within 420 s: WebSocket auth ok
rollback   on any failure: restore previous .env; docker compose up -d; if core ran a DB migration, restore the
           DB snapshot; if the HA digest changed, restore the HA snapshot;
           then say "the update failed and I rolled back" and post the logs to System › Updates
cleanup    on success: docker image prune, keeping the previous release's images for one rollback
```

4. **Downgrade** is `beepboop update --to <release>`, same procedure. Migrations are forward-only, so a downgrade across one restores the pre-update snapshot and says so.
5. **Not offered:** auto-apply, auto-approve for "security only", or scheduled updates without a human. An admin can cron `beepboop update` if they truly want that; the project does not ship the option.
6. **OS packages** (§2 step 9) are the one exception, limited to Debian's security pocket; they never restart containers.

## 10. Backups and restore

The operator's view; bundle contents, schedule, retention, encryption and the recovery key are owned by [DATA_AND_PRIVACY §11](DATA_AND_PRIVACY.md#11-backups).

- **Where they go.** Encrypted bundles land in `/srv/beepboop/backups/` on the SSD by default. Until a second destination exists, `System › Backups` and the health page warn *Backups are only on this disk*. `System › Backups` accepts a USB path or a mounted SMB share (credentials stored encrypted); object storage is v0.2.
- **Restore on a new Pi.** Flash, install, and answer *Restore from backup?* at the claim step: upload the bundle or point at the USB path, type the recovery phrase, and the device comes back with its people, grants, voiceprints and HA configuration.
- **Restore on the same Pi.** `beepboop db restore <file>` on the host stops core, snapshots the current database, swaps in the backup, restarts, and runs the same health probe as updates. A bad restore is reversible.
- **Lost the recovery phrase?** Existing backups cannot be restored on a new Pi. Generate a new recovery key in `System › Backups`; the next backup is usable again.

Restore is tested in CI by restoring the previous release's backup fixture into the current release.

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

**Latency waterfall.** Activity renders `stages_ms_json` as a waterfall for every turn a user may see (their own; admins see all); the same renderer prints in the terminal (`beepboop logs --turn <id>`), one bar per stage with `risk`/`decision` on tool spans and the earcon marked against its 1 s target.

**`beepboop bench`** lives in `tools/bench/` and runs inside the audio and core containers against a fixture corpus (`tools/bench/corpus/`, recorded with `tools/bench/record_fixture.py` through an XVF3800: WAVs with expected transcript, speaker and intent). `--gates` is valid on `all` only. **Gate thresholds live in one place, the M0 table in [ROADMAP.md](ROADMAP.md)**; this table says what each subcommand reports and which gate reads it.

| Command | Reports | Feeds |
|---|---|---|
| `bench speaker` | CAM++ embed latency p50/p95 for 1 s and 2 s clips, fp32 and int8; household EER on the fixture set | G1 |
| `bench stt` | zipformer RTF at 1 and 2 threads, final-tail latency, WER (with and without hotwords; the hotword delta is pending a BPE model, [VOICE_PIPELINE §10.2](VOICE_PIPELINE.md#102-per-profile-hotword-biasing)) | G2 |
| `bench tts` | first-chunk latency per voice, RTF | G6 (stage check) |
| `bench wake` | false accepts per 24 h on the soak corpus, false rejects at 1 m / 3 m, hit latency, CPU | G5 |
| `bench aec` | wake-hit rate and STT WER during playback vs silence | V3 |
| `bench endpoint` | truncation and over-run across VAD hangover values on hand-labelled command ends | G6 (chooses the hangover) |
| `bench llm` | Bedrock TTFT p50/p95 for the first and second (cached) call, warm vs cold cache (gap ≥ 6 min), with and without pre-warm; cache read tokens; cost per turn | G3 |
| `bench e2e` | full pipeline with HA running and a synthetic HA state storm; waterfall per turn; p50/p90/p95 for fast path (incl. the 1 s command case), no-tool and tool turns | G6 |
| `bench ram` | peak of (`MemTotal` − `MemAvailable`) at 1 Hz during `e2e`, zram used, OOM kills per container; default profile and with `music` | G4 |
| `bench thermal` | SoC temperature and throttle bits during a 20-minute `e2e` | G7 |
| `bench all [--gates] [--json]` | everything above into `tools/bench/results/<date>-<host>.json`; `--gates` exits non-zero on any failed gate; CI diffs against the last accepted run | all |

Flags and M0 scope: `--live` (alias `--llm-live`) lets `bench llm` and `e2e` call real Bedrock (about 20 billed calls); without it the LLM stages are not reported. `--ram-from <results.json>` (repeatable) merges the 24 h RAM profile from an earlier run on the same host and RAM variant, so G4 sees both `default` and `music`. Gates are judged on a 4 GB Pi; an 8 GB run is labelled as such and recorded alongside, never instead. **In M0, `bench e2e` is an in-process emulation**: it chains the real speech stages and the LLM call but has no agent loop, and the fast path is a stub, so its G6 numbers are provisional until the v0.1 loop exists.

**Health endpoint.** `GET /api/health` (no auth for a bare `ok`; details need an admin session) returns per-service status, audio device present, HA connected, Bedrock reachable and last TTFT, last backup result and destination, cert expiry, pending update, the last 24 h p50/p95 per stage, and everything `host.json` carries. OpenTelemetry export is an optional sink, off by default.

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

Error mapping is owned by [LLM_PROVIDERS §3](LLM_PROVIDERS.md#3-bedrock-adapter-providersllmbedrock). Operator checks: `AccessDeniedException` → IAM lacks `bedrock:InvokeModelWithResponseStream` or model access was not requested in the console (`System › LLM` links to both); `ValidationException` on the model ID → use an inference-profile id such as `global.anthropic.claude-haiku-5-5`; signature errors or `ExpiredToken` → the clock (`chronyc tracking`, §2) or an expired API key; `ThrottlingException` → the fallback ladder engages within one turn, persistent throttling shows on the health page; no network → the device says so within 1 s and continues on the fast path.

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

- `models/manifest.yaml` is the single source of truth: one entry per artifact with `id`, `kind` (wake | vad | speaker | stt | tts), `url`, `sha256`, `size`, `license` (SPDX where possible), `attribution`, `source`, `notes`, and four install keys: `dest` (install path relative to `models/`, which containers mount at `/models`), `archive` (`tar.bz2` when the download is an archive, extracted with the `data` filter), `files` (named files the code opens inside an extracted archive) and `status` (`pinned`, or `pending` for an artifact with no published `url` yet, such as the first wake model). The schema is `ModelEntry` in `tools/bench/beepboop_bench/models.py`. The installer, `beepboop models verify` and CI all read it.
- `models/LICENSES.md` is **generated** from the manifest by `python -m beepboop_bench.models licenses` and checked in; `licenses --check` (CI) fails if it is stale or a licence is missing, `UNKNOWN` or non-commercial (`*-NC-*`), or is neither on the GPL-3.0-compatible allow-list (permissive, CC0, CC BY, MPL-2.0, LGPL, GPL-2.0-or-later, GPL-3.0) nor a research-accepted licence the owner ruled on. GPL-incompatible licences (proprietary, GPL-2.0-only, ND) are refused because they are not on the list. Module licence notes live in `modules/<id>/LICENSES.md` when a module bundles data; CI aggregates them into the release notes.
- Research-accepted, by owner ruling: `LicenseRef-Blizzard-2013-Lessac` (D1, 2026-11-03). Most English Piper voices, including the default `en_US-lessac-low`, and the LibriTTS-R sample generator used to train the wake word descend from the Blizzard 2013 Lessac corpus, which is licensed for research only. They are accepted for this personal/research project; a commercial build must swap the voice and train the wake word with the strict profile ([tools/train_wakeword/README.md §7.2](../tools/train_wakeword/README.md#72-the-lessac-provenance-accepted-by-the-owner-d1)).
- Known entries: Silero VAD (MIT), CAM++ WeSpeaker VoxCeleb ONNX (code Apache-2.0, weights CC BY 4.0, attribution in `NOTICE`), sherpa-onnx streaming zipformer English small (Apache-2.0), Piper-format voices (per-voice licence; lessac-derived ones under D1), the stock "Hey beepboop" microWakeWord model (Apache-2.0; trained by `tools/train_wakeword/`, training-data licences in its `DATA_LICENSES.md`). openWakeWord pretrained models are CC BY-NC-SA and therefore plugin-only, never downloaded by default. Moonshine and Parakeet stay out until their licenses are verified.
- The runtime is sherpa-onnx (Apache-2.0) for VAD, speaker ID, STT and TTS, plus `piper-phonemize` over espeak-ng (GPL-3.0) as the TTS phonemiser in the audio process, reading each voice's `espeak-ng-data` ([VOICE_PIPELINE §11.1](VOICE_PIPELINE.md#111-default-piper-voices-in-sherpa-onnx-sherpa-piper), [ADR 0020](adr/0020-gpl-3-and-espeak-phonemiser.md)).
- Home Assistant is a trademark of the Open Home Foundation. beepboop "works with Home Assistant"; it is not branded as or bundled with it, and the HA image is pulled unmodified ([ADR 0016](adr/0016-docker-compose-mit.md)).
- A model swap is an explicit `beepboop models update` on the host that re-verifies checksums and, for speaker models, puts every voiceprint into "re-enrolment needed" (§12).
