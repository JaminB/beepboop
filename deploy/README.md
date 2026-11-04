# deploy

What runs on the Pi: the Compose stack, its reverse proxy, the host firewall and
Home Assistant's managed config. [docs/OPERATIONS.md §3](../docs/OPERATIONS.md#3-compose-layout)
is the canonical shape and [§4](../docs/OPERATIONS.md#4-caddy) the Caddy routes; these
files implement them. If you change one, change the doc in the same PR (hard rule 9).

| File | Installed at | What it is |
|---|---|---|
| [`compose.yaml`](compose.yaml) | `/srv/beepboop/deploy/compose.yaml` | Four services (`caddy`, `core`, `audio`, `homeassistant`) plus the optional `music` profile. Host networking for caddy/core/HA/music, **no network** for audio, `/dev/snd`, cpusets, memory caps, images by digest. |
| [`.env.example`](.env.example) | `/srv/beepboop/deploy/.env` | Host, tier, locale, memory caps for the RAM profile, image digests. **Never secrets.** An empty digest makes Compose refuse to start. |
| [`caddy/Caddyfile`](caddy/Caddyfile) | `/srv/beepboop/deploy/caddy/Caddyfile` | Tier 0 local CA; `:443` → core, except HA's `/auth/external/callback*`; `:80` `/ca` helper + redirect; `:8443`/`:8445` HA and Music Assistant UIs behind `forward_auth /api/auth/check?require=admin`. |
| [`ports.yaml`](ports.yaml) | `/srv/beepboop/deploy/ports.yaml` | Every listening port with a reason: the LAN allowlist (`base`, per-profile, user-declared) and the loopback-only sockets. `beepboop doctor` reads it. |
| [`nftables/beepboop.nft`](nftables/beepboop.nft) | included from `/etc/nftables.conf` | Inbound default-deny; drops LAN → `:8123` first; allows exactly `ports.yaml` `base` plus the `lan_tcp`/`lan_udp` sets. Never flushes Docker's tables. |
| [`homeassistant/configuration.yaml`](homeassistant/configuration.yaml) | `/srv/beepboop/ha/configuration.yaml` | The managed block from [HOME_ASSISTANT §2.1](../docs/HOME_ASSISTANT.md#21-loopback-bind-plus-nftables): `server_host: 127.0.0.1`, trusted proxy, canonical URLs, recorder tuning. `install.sh` renders it with `https://$BB_HOST` in place of `https://beepboop.local`; this is the only place HA's URLs are set. |

`install.sh`, the systemd units, the udev rule and `firmware.lock` are not written yet
([CLAUDE.md §3](../CLAUDE.md#3-repository-layout)); until then the steps below are manual.

## Check it (any machine, no Docker)

```bash
uv run pytest deploy/tests      # services, networking, cpusets, memory budget vs OPERATIONS §7,
                                # digest pinning, no secrets, ports.yaml <-> nftables, Caddy routes
```

These are static checks. They cannot prove the stack starts; that needs the Pi.

## Run it on the Pi (owner)

```bash
sudo mkdir -p /srv/beepboop/{deploy/caddy,caddy/data,caddy/config,config,core,backups,ha,models,run,audio,music}
sudo cp deploy/compose.yaml /srv/beepboop/deploy/
sudo cp deploy/caddy/Caddyfile /srv/beepboop/deploy/caddy/
sudo cp deploy/.env.example /srv/beepboop/deploy/.env        # then fill in the digests
sudo cp deploy/homeassistant/configuration.yaml /srv/beepboop/ha/
docker compose -f /srv/beepboop/deploy/compose.yaml config    # validates interpolation and schema

# Firewall: keep your distro's /etc/nftables.conf and add one line to it:
#   include "/etc/nftables.d/beepboop.nft"
sudo install -D -m 0644 deploy/nftables/beepboop.nft /etc/nftables.d/beepboop.nft
sudo nft -c -f /etc/nftables.d/beepboop.nft && sudo nft -f /etc/nftables.d/beepboop.nft
```

Look digests up with `docker buildx imagetools inspect <image>:<tag>` and record the HA
tag next to `BB_HA_DIGEST` in a comment. **During M0 only Home Assistant has a published
image**: the beepboop `caddy`, `core` and `audio` images are not built yet, so start only
HA with `docker compose -f /srv/beepboop/deploy/compose.yaml up -d homeassistant`.
Compose interpolates the whole file, so the three unbuilt digests still need a
syntactically valid value (`sha256:` followed by 64 zeros); they are never pulled because
those services are not started. Remove the placeholders as soon as real images exist.

Caddy needs the `install.sh`-generated, name-constrained root at
`/srv/beepboop/caddy/data/caddy/pki/authorities/local/root.{crt,key}` before it starts
([OPERATIONS §4](../docs/OPERATIONS.md#4-caddy)).

## Pending on hardware

- **V1** (HA bound to loopback, LAN → `:8123` dropped, which layer is load-bearing):
  **PENDING, owner action.** Procedure and probe: [tools/probes/ha_bind/](../tools/probes/ha_bind/README.md).
- `docker compose config` and a real `up` on Pi OS Lite 64-bit: not run (no Docker on the dev box).
- Memory caps are the OPERATIONS §7 **targets**; G4 decides the final values.
