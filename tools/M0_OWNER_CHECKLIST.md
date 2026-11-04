# M0 owner checklist

Everything in M0 that needs the Pi, the XVF3800, Docker, AWS on the home connection or a
human, in the order to do it. The code for each step is done and tested on x86
([ROADMAP M0 status](../docs/ROADMAP.md#m0-prove-the-pi)); **none of these results exist
yet**. Each step names the README that has the full procedure; the commands here are the
short form. Every result lands in `tools/bench/results/`; commit each file as you go.

Two result files already exist; **neither is a gate result**:
`results/2026-11-02-workstation-g3-preliminary.json` (x86 workstation G3 probe) and
`results/2026-11-03-desktop-n9qe44h-synthetic.json` (*synthetic / x86 / not a gate result*).

## What you need

- Raspberry Pi 4 **4 GB** (the gate board) and **8 GB** (recorded alongside), each with an SSD,
  the reference fan case and the official PSU
- ReSpeaker XVF3800 (USB, 16 kHz firmware), a speaker, an SPL meter (V3)
- A second machine on the same LAN (V1) and a laptop for recording and wake-word training
- At least 6 people for the corpus (step 7) and a living room you can record for 24 h
- An AWS profile with Bedrock access to Haiku 5.5 (see decision D2)

## 1. Prepare each Pi (4 GB first, then 8 GB)

Follow [OPERATIONS §2](../docs/OPERATIONS.md) steps 1–8 by hand (`install.sh` is not written yet):

```bash
# 1. Flash Raspberry Pi OS Lite 64-bit to the SSD with Raspberry Pi Imager (hostname, user, SSH key).
# 2. Once, from an SD card on a fresh Pi 4: sudo rpi-eeprom-update -a, then USB-first boot.
sudo apt install -y nftables zram-tools chrony fake-hwclock alsa-utils git   # + docker-ce and the compose plugin (Docker's apt repo)
sudo sed -i '$ s/$/ cgroup_enable=memory cgroup_memory=1 swapaccount=1/' /boot/firmware/cmdline.txt && sudo reboot
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <this repo> ~/beepboop && cd ~/beepboop
uv sync --all-packages --frozen
uv run python -m beepboop_bench.models fetch        # pinned weights, sha256-verified
```

## 2. Compose skeleton and HA

[deploy/README.md](../deploy/README.md#run-it-on-the-pi-owner). Fill `BB_HA_DIGEST` (vetted
2026.10.x) in `/srv/beepboop/deploy/.env`; give the three unbuilt beepboop images the
placeholder `sha256:` + 64 zeros. Then:

```bash
docker compose -f /srv/beepboop/deploy/compose.yaml config
sudo nft -c -f /etc/nftables.d/beepboop.nft && sudo nft -f /etc/nftables.d/beepboop.nft
docker compose -f /srv/beepboop/deploy/compose.yaml up -d homeassistant
ss -ltn | grep 8123                                   # expect 127.0.0.1:8123
```

## 3. V1: HA loopback bind and nftables drop

[tools/probes/ha_bind/README.md](probes/ha_bind/README.md): three scenarios (`both`,
`bind-only`, `nft-only`), each with a LAN-side run on the second machine:

```bash
python3 tools/probes/ha_bind/v1_report.py lan <pi-ip> > lan-both.json                      # second machine
sudo bash tools/probes/ha_bind/verify_v1.sh pi --scenario both --lan-result ~/lan-both.json # Pi
```

Commit `results/<date>-<host>-v1.json`; record the verdict in
[HOME_ASSISTANT §16](../docs/HOME_ASSISTANT.md#16-m0-checklist-ha-part).

## 4. V2: headless HA onboarding

[tools/probes/ha_onboarding/verify_v2.md](probes/ha_onboarding/verify_v2.md), against a
**fresh** HA volume:

```bash
uv run python tools/probes/ha_onboarding/record_v2.py --data-dir ~/v2-data
```

Commit `results/<date>-<host>-v2.json`, write the sequence into HOME_ASSISTANT, `rm -r ~/v2-data`.

## 5. V5: passphrase-wrapped key-encryption key

[tools/probes/otp_kek/README.md](probes/otp_kek/README.md). Under D3 (2026-11-03) the
passphrase-wrapped KEK is the v0.1 default and alone decides V5. **Do not program the OTP
key**; that is an optional, irreversible later upgrade (README steps 2–6).

```bash
uv run python tools/probes/otp_kek/probe_v5.py fallback
uv run python tools/probes/otp_kek/probe_v5.py read     # expect otp_programmed: false
uv run python tools/probes/otp_kek/probe_v5.py report
```

Commit `results/<date>-<host>-v5.json`; record it in DATA_AND_PRIVACY.

## 6. Train the stock wake word (laptop with a GPU, hours, ~100 GB downloads)

[tools/train_wakeword/README.md](train_wakeword/README.md) §6. The lessac-derived generator
voices are accepted (D1), so the default profile applies.

```bash
cd tools/train_wakeword && make deps check
make synth && make negatives && make augment && make train && make export && make eval
make export ARGS="--probability-cutoff <recommended>"
```

Record the dataset sha256s step 02 prints into the config, publish
`hey_beepboop_v1.{tflite,json}`, and add the generated row (url + sha256) to
`models/manifest.yaml`. Until then `bench wake` reports `stock_model: false` and G5 cannot pass.

## 7. Record the fixture corpus (bench laptop with the XVF3800)

[tools/bench/README.md](bench/README.md#pending-on-hardware-owner-actions); consent text and
prompts are in the script. At least 6 speakers, 200 utterances and a 24 h soak:

```bash
arecord -l                                                     # find the array, e.g. hw:1
uv run python tools/bench/record_fixture.py --device hw:1 --speaker <name> --kind adult   # per speaker
uv run python tools/bench/record_fixture.py --device hw:1 --soak-hours 24
uv run python tools/bench/record_fixture.py --finish           # validates the manifest
```

Hand-check `speech_end_s` in `tools/bench/corpus/manifest.yaml`, then copy `corpus/` to the Pi.
The corpus is git-ignored and never leaves your machines (ADR 0018).

## 8. V3: AEC reference

[tools/probes/aec_reference/README.md](probes/aec_reference/README.md): TTS at 70 dB SPL from 1 m, a person speaking.

```bash
uv run beepboop bench aec --device hw:1 --host pi4-4gb
```

## 9. V4: 24 h ALSA soak (plain, then under load)

[tools/probes/alsa_soak/README.md](probes/alsa_soak/README.md). The audio image does not
exist yet, so run the plain soak on the host and say so in the result's `variant` field:

```bash
sh tools/probes/alsa_soak/alsa_soak.sh -D hw:1 -d 60          # smoke first
sh tools/probes/alsa_soak/alsa_soak.sh -D hw:1 -d 86400
sh tools/probes/alsa_soak/run_variant.sh -D hw:1 -d 86400
```

## 10. G3: Bedrock TTFT from the Pi on the home connection

[core/beepboop/providers/llm/bedrock/README.md](../core/beepboop/providers/llm/bedrock/README.md). Uses at most 20 short billed calls.

```bash
uv run beepboop llm probe --profile <aws-profile> --region us-west-2 --pairs 5 \
    --cold-gap-minutes 6 --host pi4-4gb --gate --out tools/bench/results/$(date -u +%F)-pi4-4gb-g3.json
```

Repeat with `--host pi4-8gb`. This also gives the first valid cold-cache and pre-warm numbers.
`--gate` is refused on anything that is not a Raspberry Pi, so a `gate` label can only come from
the Pi.

## 11. G1–G6: the gate runs (4 GB, then 8 GB), HA running

[tools/bench/README.md](bench/README.md#pending-on-hardware-owner-actions). Fast-path turns
with the network unplugged (`--offline`), LLM turns online (`--live`). G4 needs two 24 h soaks
judged together, so the second `bench ram` and the final `bench all` merge the earlier file(s)
with `--ram-from`:

```bash
R=tools/bench/results; D=$(date -u +%F)
uv run beepboop bench ram --profile default --duration 86400 --host pi4-4gb \
    --out $R/$D-pi4-4gb-ram-default.json                                   # G4, 24 h
docker compose -f /srv/beepboop/deploy/compose.yaml --profile music up -d
uv run beepboop bench ram --profile music --duration 86400 --host pi4-4gb \
    --ram-from $R/$D-pi4-4gb-ram-default.json --out $R/$D-pi4-4gb-ram-both.json   # G4, 24 h, music on
docker compose -f /srv/beepboop/deploy/compose.yaml --profile music down
uv run beepboop bench all --gates --ha-running --offline --live --host pi4-4gb \
    --ram-from $R/$D-pi4-4gb-ram-both.json                                # G1-G6 in one file; exit 0 = all pass
```

Then the same three runs on the 8 GB board with `--host pi4-8gb`. ROADMAP judges every gate on
the **4 GB** board and records the 8 GB run alongside: the 8 GB file is labelled `not a gate
result`, its gate table can only say `incomplete: checks met but run preconditions unmet`, and
`--gates` therefore exits 1 there. That is expected; keep the file.

## 12. G7: thermal (reference fan case)

[tools/probes/thermal_g7/README.md](probes/thermal_g7/README.md):

```bash
sh tools/probes/thermal_g7/run_g7.sh -d 1200 -- beepboop bench thermal --duration 1200 --live
```

## 13. Close M0

1. Commit every `tools/bench/results/*.json` (`sudo chown` files written as root first).
2. Cite them with dates in [ROADMAP M0](../docs/ROADMAP.md#m0-prove-the-pi) (replace the status
   table), [VOICE_PIPELINE](../docs/VOICE_PIPELINE.md), [HOME_ASSISTANT §16](../docs/HOME_ASSISTANT.md#16-m0-checklist-ha-part)
   and [DATA_AND_PRIVACY](../docs/DATA_AND_PRIVACY.md), with HA version, XVF3800 firmware version and date.
3. For every failing gate, open an ADR choosing the ROADMAP fallback before any v0.1 work.

## Decisions

Decided 2026-11-03:

- **Licence**: the project is GPL-3.0-or-later ([ADR 0020](../docs/adr/0020-gpl-3-and-espeak-phonemiser.md));
  piper-phonemize / espeak-ng are normal dependencies of the Piper TTS path.
- **D1 accepted**: the `lessac`-derived Piper voices (Blizzard 2013, research-only provenance)
  feed the wake-word generator and the default TTS voice; the provenance is recorded in
  `models/LICENSES.md` and `tools/train_wakeword/README.md` §7.2. The strict profile stays an option.
- **D3 deferred**: the Pi OTP key is not programmed; the argon2id passphrase-wrapped KEK is the
  v0.1 default (step 5). OTP is an optional later upgrade.
- **D4 resolved**: every spec error the builders found is fixed in docs, code and tests; see
  "Resolved spec findings" in [ROADMAP M0](../docs/ROADMAP.md#m0-prove-the-pi).

Still yours:

- **D2** Which AWS profile Bedrock runs under. The preliminary probe used the only configured
  profile on the workstation, which may belong to an unrelated account; use a dedicated one.
- Confirm the `speak` unknown-clip-key rule (error earcon + `speak.done` reason `error`,
  [ARCHITECTURE §4.2](../docs/ARCHITECTURE.md#42-event-catalog)).
- Verify the `verify`-marked rows in `core/beepboop/providers/llm/pricing.yaml` (list-price
  estimates, [LLM_PROVIDERS §10](../docs/LLM_PROVIDERS.md)).
