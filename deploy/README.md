# deploy

What runs on the Pi: the Compose stack, its images, its reverse proxy, the host firewall,
Home Assistant's managed config, the installer and the host CLI.
[docs/OPERATIONS.md](../docs/OPERATIONS.md) is the spec (§2 OS, §3 Compose, §4 Caddy, §5
install and host CLI, §9 updates, §10 backups); these files implement it. If you change one,
change the doc in the same PR (hard rule 9).

| File | Installed at | What it is |
|---|---|---|
| [`install.sh`](install.sh) | run from a release checkout | The only installer: preflight, host packages, data root, KEK passphrase file, Tier-0 root with `nameConstraints`, firewall, models, images, start. POSIX `sh`, idempotent, `--dry-run`. |
| [`compose.yaml`](compose.yaml) | `/srv/beepboop/deploy/compose.yaml` | Four services (`caddy`, `core`, `audio`, `homeassistant`) plus the optional `music` profile. Host networking for caddy/core/HA/music, **no network** for audio, cpusets, memory caps, images by digest. |
| [`.env.example`](.env.example) | `/srv/beepboop/deploy/.env` | Host, tier, locale, `COMPOSE_PROFILES`, memory caps, image digests. **Never secrets.** An empty digest makes Compose refuse to start. |
| [`docker/`](docker/) | built by the release pipeline | `core.Dockerfile` (workspace venv from `uv.lock`, modules, the built SPA; no Node at runtime), `audio.Dockerfile` (audio + `beepboop-bench`, `libasound2`; no weights), `caddy.Dockerfile`. Base images pinned by digest. Build context is the repo root (see `/.dockerignore`). |
| [`host/beepboop_host.py`](host/beepboop_host.py) | `/usr/local/lib/beepboop/` | The host CLI: `doctor`, `update`, `db restore`, `logs`, `models verify\|update`, `host-json`; forwards everything else into core. Stdlib + `python3-yaml`, Python 3.11+. |
| [`bin/beepboop`](bin/beepboop) | `/usr/local/bin/beepboop` | The shim that runs the host CLI. |
| [`systemd/`](systemd/) | `/etc/systemd/system/` | `beepboop.service` (`compose up -d --remove-orphans`), `beepboop-host.service` + `.timer` (every 10 s: `host.json`, restart unhealthy containers). |
| [`udev/99-beepboop-xvf3800.rules`](udev/99-beepboop-xvf3800.rules) | `/etc/udev/rules.d/` | Stable ALSA id `hw:XVF3800`, `audio` group. |
| [`firmware.lock`](firmware.lock) | (read by `doctor`) | The vetted XVF3800 firmware. **Pending on Pi**: the version is not recorded yet. |
| [`caddy/Caddyfile`](caddy/Caddyfile) | `/srv/beepboop/deploy/caddy/Caddyfile` | Tier 0 local CA; `:443` → core, except HA's `/auth/external/callback*`; `:80` `/ca` helper + redirect; `:8443`/`:8445` HA and Music Assistant UIs behind `forward_auth /api/auth/check?require=admin`. |
| [`ports.yaml`](ports.yaml) | `/srv/beepboop/deploy/ports.yaml` | Every listening port with a reason: the LAN allowlist (`base`, per-profile, user-declared) and the loopback-only sockets. `beepboop doctor` reads it. |
| [`nftables/beepboop.nft`](nftables/beepboop.nft) | `/etc/nftables.d/beepboop.nft` | Inbound default-deny; drops LAN → `:8123` first; allows exactly `ports.yaml` `base` plus the `lan_tcp`/`lan_udp` sets. Never flushes Docker's tables. |
| [`homeassistant/configuration.yaml`](homeassistant/configuration.yaml) | `/srv/beepboop/ha/configuration.yaml` | The managed block from [HOME_ASSISTANT §2.1](../docs/HOME_ASSISTANT.md#21-loopback-bind-plus-nftables), rendered with `https://$BB_HOST`. |

## Check it (any machine, no Docker)

```bash
uv run pytest deploy/tests      # compose invariants vs OPERATIONS §3/§7, ports.yaml <-> nftables,
                                # Caddy routes, install.sh/units/Dockerfiles, and the host CLI
                                # (doctor, update + rollback, db restore, host.json) against fakes
shellcheck deploy/install.sh deploy/bin/beepboop
sudo sh deploy/install.sh --dry-run   # on a Pi: prints every step, changes nothing
```

These are static checks and fakes. They cannot prove the stack starts; that needs the Pi.

## Run it on the Pi (owner)

With published images and a `deploy/release.env`, `sudo sh deploy/install.sh` does everything
([OPERATIONS §5](../docs/OPERATIONS.md#5-install-script-and-the-host-cli)). **Until the beepboop
images exist (M0)**, only Home Assistant can run; the V1 probe needs just that:

```bash
sudo mkdir -p /srv/beepboop/{deploy/caddy,caddy/data,caddy/config,config,core,backups,ha,models,run,audio,music}
sudo cp deploy/compose.yaml /srv/beepboop/deploy/
sudo cp deploy/.env.example /srv/beepboop/deploy/.env        # then fill in BB_HA_DIGEST
sudo cp deploy/homeassistant/configuration.yaml /srv/beepboop/ha/
sudo install -D -m 0644 deploy/nftables/beepboop.nft /etc/nftables.d/beepboop.nft
sudo nft -c -f /etc/nftables.d/beepboop.nft && sudo nft -f /etc/nftables.d/beepboop.nft
docker compose -f /srv/beepboop/deploy/compose.yaml up -d homeassistant
```

Look digests up with `docker buildx imagetools inspect <image>:<tag>`. Compose interpolates the
whole file, so the unbuilt digests need a syntactically valid placeholder (`sha256:` and 64
zeros); they are never pulled because those services are not started. Keep your distro's
`/etc/nftables.conf` and add `include "/etc/nftables.d/beepboop.nft"` to it.

## Pending on Pi (owner)

Nothing here has run on a Pi yet; none of it is claimed.

- ROADMAP v0.1 #1: `install.sh` on a fresh Pi OS Lite 64-bit image reaching a spoken claim code
  in ≤ 15 minutes. Needs published images and a `deploy/release.env` with their digests.
- ROADMAP v0.1 #9: restore onto a second Pi with the recovery phrase; `beepboop update` to a
  deliberately broken image rolling back; `beepboop doctor` flagging an undeclared port on the
  real host (all three are tested with fakes in `deploy/tests/test_host.py` and
  `core/tests/db/test_backup.py`).
- Image builds for `linux/arm64` (`docker buildx`), the udev rule's match on the real array,
  `firmware.lock`, and `--luks`.
- **V1** (HA bound to loopback, LAN → `:8123` dropped): procedure and probe in
  [tools/probes/ha_bind/](../tools/probes/ha_bind/README.md).
- Memory caps are the OPERATIONS §7 **targets**; G4 decides the final values.
