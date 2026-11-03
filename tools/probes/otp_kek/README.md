# tools/probes/otp_kek: M0 item V5

> Status: **PENDING, owner action** (needs a Pi 4, and for the main check an OTP key you
> choose to program; that is irreversible).

[ROADMAP V5](../../../docs/ROADMAP.md#m0-prove-the-pi): *the Pi OTP device key is
readable from the core container via `/dev/vcio` and produces a stable KEK across
reboots; the passphrase-wrapped fallback works on a board without it.* The design is in
[DATA_AND_PRIVACY §6.3](../../../docs/DATA_AND_PRIVACY.md#63-envelope-encryption), and the
code under test is in [`core/beepboop/secrets/`](../../../core/beepboop/secrets/README.md).

`probe_v5.py` appends one observation per run to
`tools/bench/results/<date>-<host>-v5.json` and recomputes the verdict:

| Part | PASS when |
|---|---|
| `otp` | A programmed key was read on **≥ 2 different boots** (kernel `boot_id`), with the **same KEK fingerprint** each time and the same device salt, and **at least one read happened inside a container** with `/dev/vcio` mapped. A changed fingerprint, or a `--cross-check` mismatch with `vcmailbox`, is a FAIL. |
| `fallback` | Create, reload, wrong-passphrase rejection and a `SecretStore` round trip all work, on a board **without** a programmed OTP key. |

The key and the KEK are never printed or stored. Only `sha256("beepboop/kek-fingerprint" ‖ KEK)[:16]`
is recorded. Errors (no `/dev/vcio`, permission denied, mailbox locked) are recorded
as they happened, and the verdict stays `INCOMPLETE` until the evidence exists. If
`/dev/vcio` can never be read from a container, that is a V5 **FAIL**. Write it in
the doc with the error the probe recorded.

## Procedure (on the Pi, from the repo root, after `uv sync --all-packages --frozen`)

1. **Fallback first, while the board has no OTP key.**
   `uv run python tools/probes/otp_kek/probe_v5.py fallback`.
   `uv run python tools/probes/otp_kek/probe_v5.py read` should then show
   `otp_programmed: false`. If the key is already programmed, run `fallback` on a
   second board that has none, such as the 8 GB Pi.
2. **Decide on the OTP key (irreversible).** OTP bits can never be cleared. The
   beepboop installer will do this step itself later; for M0 you do it by hand.
   Raspberry Pi's firmware expects a P-256 private scalar, so follow their tool:
   ```bash
   sudo rpi-otp-private-key -c && echo "already programmed" || echo "blank"
   openssl ecparam -name prime256v1 -genkey -noout -out /tmp/otp.pem
   openssl ec -in /tmp/otp.pem -text -noout | awk '/priv:/{f=1;next}/pub:/{f=0}f' | tr -d ' \n:' | head -c 64 > /tmp/d.hex
   sudo rpi-otp-private-key -w "$(cat /tmp/d.hex)"     # asks you to type YES
   shred -u /tmp/otp.pem /tmp/d.hex                    # beepboop never needs the raw key off-device
   ```
   Make sure `config.txt` does **not** set `lock_device_private_key=1`. That setting
   disables the mailbox read this whole design depends on. Record whether your
   firmware supports it, because that matters for the threat model.
3. **Read on the host, with a cross-check:**
   `uv run python tools/probes/otp_kek/probe_v5.py read --data-dir ~/v5-data --cross-check`
   (`vcmailbox` comes from `raspi-utils`). Keep `~/v5-data`: it holds the probe's device
   salt, and the fingerprints are only comparable while the salt stays the same.
4. **Read from a container,** using the same device mapping and group as `beepboop-core`
   in `deploy/compose.yaml`. The core image is not built during M0, so use uv's image:
   ```bash
   docker run --rm -v "$PWD":/src -v ~/v5-data:/v5-data -w /src \
     --device /dev/vcio:/dev/vcio --group-add "$(getent group video | cut -d: -f3)" \
     -e UV_PROJECT_ENVIRONMENT=/tmp/venv \
     ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
     uv run --frozen --all-packages python tools/probes/otp_kek/probe_v5.py read --data-dir /v5-data --host "$(hostname)"
   ```
   Pass `--host` so the record lands in the same file. A container's hostname differs
   from the host's.
5. **Reboot, then repeat steps 3 and 4.** `otp` turns PASS once two boots agree.
   `probe_v5.py report` prints the verdict without adding an observation.
6. **Record it:** commit the results file (`sudo chown` it first if Docker wrote it as
   root), then write the date, Pi variant, firmware version (`vcgencmd version`) and
   result into [DATA_AND_PRIVACY §6.3](../../../docs/DATA_AND_PRIVACY.md#63-envelope-encryption)
   (ROADMAP acceptance criterion 4).

## Tests (x86)

`uv run pytest core/tests/secrets` covers the mailbox request and response parsing
against a fake firmware, HKDF against an independent RFC 5869 implementation, the
passphrase file, the store, and this probe's verdict logic. The real `/dev/vcio` test is
`uv run pytest -m hardware core/tests/secrets` on the Pi. Off a Pi every record is
labelled "synthetic / x86 / not a gate result".
