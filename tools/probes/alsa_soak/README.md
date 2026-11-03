# tools/probes/alsa_soak: V4, 24 h raw ALSA capture without xruns

**Status: code done; result PENDING (owner with the Pi and the XVF3800).**

ROADMAP V4: raw ALSA in the audio container with `network_mode: none` runs for 24 h without
device-busy errors or xruns > 1/h. **Variant:** the same 24 h with concurrent SSD write load
and a `docker pull` in progress, xruns <= 1/h.

| Script | What |
|---|---|
| `alsa_soak.sh` | Opens the product's capture stream (S16_LE, 16 kHz, 6 ch, 160-frame period; VOICE_PIPELINE §2.3) with `arecord -v` for 24 h to `/dev/null`, counts `overrun`/`xrun` and `Device or resource busy` lines, writes a JSON verdict. |
| `run_variant.sh` | Runs the same soak on the host with a background `dd`+fsync write loop on the SSD and a repeated `docker pull`. |

Both are POSIX `sh` + alsa-utils; no Python, nothing persisted (audio goes to `/dev/null`).

## Procedure (owner)

Plain run, inside the audio container once the compose skeleton is up (M0 step 3), so the
`/dev/snd` passthrough and `network_mode: none` are the real ones:

```bash
docker compose -f /srv/beepboop/deploy/compose.yaml cp tools/probes/alsa_soak/alsa_soak.sh audio:/tmp/
docker compose -f /srv/beepboop/deploy/compose.yaml exec audio sh /tmp/alsa_soak.sh -D hw:1 -d 86400 -o /tmp/v4-plain.json
docker compose -f /srv/beepboop/deploy/compose.yaml cp audio:/tmp/v4-plain.json tools/bench/results/$(date -u +%F)-pi4-4gb-v4-plain.json
```

(If the compose skeleton is not there yet, run it on the host with `sh alsa_soak.sh -D hw:1`;
say so in the result's `variant` field.)

Variant run, on the host (needs docker and the SSD):

```bash
sh tools/probes/alsa_soak/run_variant.sh -D hw:1 -d 86400
```

Each run writes `tools/bench/results/<date>-<host>-v4-<variant>.json` with `pass`, `xruns`,
`xruns_per_hour`, `device_busy_errors`, kernel version and the last log lines. Record both
verdicts, the firmware version and the date in VOICE_PIPELINE §2.3 / ROADMAP V4.

A quick smoke before committing to 24 h: `sh alsa_soak.sh -D hw:1 -d 60`.
