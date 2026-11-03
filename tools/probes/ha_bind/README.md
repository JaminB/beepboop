# tools/probes/ha_bind: M0 item V1

**Question** ([ROADMAP V1](../../../docs/ROADMAP.md#m0-prove-the-pi)): does the pinned Home
Assistant image honour `http: server_host: 127.0.0.1` in a container, and does the
nftables default-deny input chain drop LAN → `:8123`? Record which layer (or both) is
load-bearing. The two layers are described in
[HOME_ASSISTANT §2.1](../../../docs/HOME_ASSISTANT.md#21-loopback-bind-plus-nftables).

**Result: PENDING (owner action).** Nothing has been measured. This needs the Pi and a
second machine on the same LAN.

| File | Runs on | Does |
|---|---|---|
| `verify_v1.sh` | the Pi (`pi` mode, as root) and a second LAN host (`lan` mode) | Collects `ss -Hltn`, `nft -j list ruleset`, the HA container's image and version, and the Pi model; calls `v1_report.py` |
| `v1_report.py` | both (Python ≥ 3.11, standard library only; also works on Windows) | `lan`: TCP-probes `<pi>:8123` and a control port. `pi`: records one scenario and recomputes the verdict |
| `tests/` | dev box | `uv run pytest tools/probes` (parsers and verdict logic with fixture inputs) |

Why a second machine: a connection from the Pi to its own LAN address enters through
`lo`, which the ruleset accepts, so it never exercises the drop. (`beepboop doctor`'s
"connect to `<lan-ip>:8123` from the host" check has the same blind spot; see the report
that came with this probe.)

## Owner procedure

Prerequisites: Pi OS Lite 64-bit, Docker with Compose v2, `nftables`, the repo checked
out on the Pi, and `python3` on the second machine. Note the Pi's LAN address (`hostname -I`).
The control port is SSH (22): it must answer for a `:8123` timeout to count as a drop.

1. **Set up** per [deploy/README.md](../../../deploy/README.md#run-it-on-the-pi-owner): copy
   `configuration.yaml` into `/srv/beepboop/ha/`, fill `BB_HA_DIGEST` with the vetted
   2026.10.x digest, `docker compose -f /srv/beepboop/deploy/compose.yaml up -d homeassistant`,
   load `beepboop.nft`. Wait until `ss -ltn | grep 8123` shows `127.0.0.1:8123`.

2. **Scenario `both`** (the shipped configuration):
   ```bash
   # second machine
   python3 tools/probes/ha_bind/v1_report.py lan <pi-ip> > lan-both.json
   scp lan-both.json <you>@<pi-ip>:
   # Pi
   sudo bash tools/probes/ha_bind/verify_v1.sh pi --scenario both --lan-result ~/lan-both.json
   ```

3. **Scenario `bind-only`** (firewall off, is `server_host` enough?):
   ```bash
   sudo nft delete table inet beepboop                      # Pi
   python3 tools/probes/ha_bind/v1_report.py lan <pi-ip> > lan-bind-only.json   # second machine, then scp
   sudo bash tools/probes/ha_bind/verify_v1.sh pi --scenario bind-only --lan-result ~/lan-bind-only.json
   sudo nft -f /etc/nftables.d/beepboop.nft                 # restore immediately
   ```

4. **Scenario `nft-only`** (bind off, is nftables enough?): comment out the
   `server_host:` line in `/srv/beepboop/ha/configuration.yaml`, then
   `docker compose -f /srv/beepboop/deploy/compose.yaml restart homeassistant` and wait until
   `ss -ltn | grep 8123` shows `0.0.0.0:8123` or `[::]:8123`. With the firewall loaded:
   ```bash
   python3 tools/probes/ha_bind/v1_report.py lan <pi-ip> > lan-nft-only.json   # second machine, then scp
   sudo bash tools/probes/ha_bind/verify_v1.sh pi --scenario nft-only --lan-result ~/lan-nft-only.json
   ```
   Then **restore** `server_host: 127.0.0.1`, restart HA and check it is back on `127.0.0.1:8123`.

5. **Commit** `tools/bench/results/<date>-<host>-v1.json` (`sudo chown` it to your user
   first) and record the outcome, HA version and date in the
   [HOME_ASSISTANT §16](../../../docs/HOME_ASSISTANT.md#16-m0-checklist-ha-part) checklist
   (ROADMAP M0 acceptance criterion 4). If `load_bearing` is not `both`, also update
   §2.1, which currently assumes both layers work.

Each run merges one scenario into the same day's file; re-running a scenario replaces it.

## Reading the result

`verdict.status` is `PASS` only when all three scenarios are recorded, the shipped
configuration blocks the LAN, and at least one layer works alone (which one is the
finding V1 asks for). It is `FAIL` if the shipped configuration lets a LAN connection
through or neither layer works alone.
Otherwise it is `INCOMPLETE`. A run where HA was not listening, the Pi did not answer on
the control port, or the scenario was mis-set (for example `nft-only` with `server_host`
still in effect) leaves that layer `null` instead of guessing.

| `lan.outcome` | Meaning |
|---|---|
| `timeout` | No answer: the packet was dropped (nftables) |
| `refused` | RST: the Pi answered but nothing listens on a LAN address (`server_host` works, firewall not dropping) |
| `connected` | HA is reachable from the LAN: V1 fails for that scenario |

`load_bearing` is `both` (each layer is enough alone), `server_host`, `nftables` or `neither`.
