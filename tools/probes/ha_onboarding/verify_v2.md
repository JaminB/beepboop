# V2: verify headless HA onboarding on the Pi (owner procedure)

> Status: **PENDING, owner action** (needs the Pi with Docker and the pinned HA image).
> Item: [ROADMAP M0 V2](../../../docs/ROADMAP.md#m0-prove-the-pi). Design: [HOME_ASSISTANT §3](../../../docs/HOME_ASSISTANT.md#3-headless-onboarding).

V2 asks two things: does headless onboarding (`/api/onboarding/*`) work against the
**pinned** HA container, and can `auth/long_lived_access_token` be called over the
WebSocket on the onboarding session? It also asks you to **record the exact sequence**.
`record_v2.py` runs the production code (`core/beepboop/ha/onboarding.py`) and writes
what it saw to `tools/bench/results/<date>-<host>-v2.json`.

Time: about 15 minutes. **The run creates the HA owner user, so it needs a fresh,
never-onboarded HA volume.** Do not point it at an HA you care about.

## 0. Before you start

- Pi 4 on Pi OS Lite 64-bit, Docker installed, this repo checked out (for example in `~/beepboop`).
- `uv` on the Pi (`curl -LsSf https://astral.sh/uv/install.sh | sh`), then from the
  repo root: `uv sync --all-packages --frozen`.
- `deploy/.env` filled in as described in [deploy/README.md](../../../deploy/README.md#run-it-on-the-pi-owner),
  with `BB_HA_DIGEST` set to the pinned 2026.10.x image. Write down the tag.

## 1. Start the pinned HA on a fresh volume

```bash
# Keep any existing HA config out of the way (skip if /srv/beepboop/ha is new).
sudo mv /srv/beepboop/ha /srv/beepboop/ha.before-v2 2>/dev/null || true
sudo mkdir -p /srv/beepboop/ha
sudo cp deploy/homeassistant/configuration.yaml /srv/beepboop/ha/

docker compose -f /srv/beepboop/deploy/compose.yaml up -d homeassistant
docker compose -f /srv/beepboop/deploy/compose.yaml logs -f homeassistant   # wait for "Home Assistant initialized", then Ctrl-C
curl -s http://127.0.0.1:8123/api/onboarding                               # every "done" must be false
```

If any step already shows `"done": true`, the volume is not fresh. Stop HA, empty
`/srv/beepboop/ha` (keep the copied `configuration.yaml`) and start again.

## 2. Run the recorder

From the repo root on the Pi (host networking, so HA's loopback port is reachable):

```bash
uv run python tools/probes/ha_onboarding/record_v2.py --data-dir ~/v2-data
```

- On a Pi with a programmed OTP key the token is encrypted under the OTP KEK. Without
  one, pass `--passphrase-file <file>`; any passphrase works for this test.
  `--data-dir` holds the device salt and the secrets database. Keep it until you have
  checked the result, then delete it with `rm -r ~/v2-data`.
- The script prints `V2 PASS|FAIL|INCOMPLETE` and the path of the results file. It never
  prints the password, codes or tokens, and never writes them anywhere except the
  encrypted database.

## 3. Check the result

Open `tools/bench/results/<date>-<host>-v2.json`:

| Field | Expect |
|---|---|
| `status` | `PASS` |
| `ha_version` | the pinned tag (for example `2026.10.3`) |
| `sequence` | `/api/onboarding` → `/api/onboarding/users` → `/auth/token` → `core_config` → `analytics` → `integration` → WS `auth` → WS `auth/long_lived_access_token` → `/auth/token` (`action=revoke`) → `/api/onboarding`, all with HTTP 200 |
| `checks.onboarding` | all four steps done |
| `checks.llat_auth` | the stored long-lived token opens a new WebSocket (`auth_ok`) |
| `checks.llat_lifespan` | `exp - iat` = 3650 days |
| `checks.llat_registered` | exactly one long-lived token named `beepboop-core` |
| `checks.llat_on_llat` | informational: whether an LLAT-authenticated connection may mint another LLAT. HOME_ASSISTANT §3 assumes it may not. Record either answer; it does not gate V2. |

Also confirm, in HA's own UI if you like, that the user `beepboop` (username
`beepboop-system`) exists and is an administrator. You can get there through an SSH
tunnel, because HA listens on loopback only: `ssh -L 8123:127.0.0.1:8123 pi`.

`INCOMPLETE` with `onboarding_status: needs_login` means HA was already onboarded, so
go back to step 1. `FAIL` lists the failing request under `sequence` with its HTTP
status and `checks.onboarding.detail` with HA's error message. Keep that file; it is
the evidence.

## 4. Record it

1. Commit `tools/bench/results/<date>-<host>-v2.json`. If you ran with `sudo`, `chown` it first.
2. Note the date, the HA tag and digest, the result and the `llat_on_llat` answer in
   [docs/HOME_ASSISTANT.md](../../../docs/HOME_ASSISTANT.md) (ROADMAP acceptance criterion 4),
   and tick the onboarding row in its §16 checklist.
3. If the sequence differs from HOME_ASSISTANT §3, change the doc and
   `core/beepboop/ha/onboarding.py` in the same PR (hard rule 9).
4. Clean up: `docker compose -f /srv/beepboop/deploy/compose.yaml down`, `rm -r ~/v2-data`,
   and restore `/srv/beepboop/ha.before-v2` if you moved one aside.
