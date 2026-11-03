# tools/probes

Pass/fail hardware probes for the M0 verification items (V1–V5 in
[docs/ROADMAP.md](../../docs/ROADMAP.md#m0-prove-the-pi)). Unlike `tools/bench/`, a probe
answers a yes/no question about the platform rather than measuring latency. Each probe
writes its result next to the bench results, in `tools/bench/results/<date>-<host>-<item>.json`.

| Probe | Item | Status |
|---|---|---|
| [`ha_bind/`](ha_bind/README.md) | V1: HA binds `127.0.0.1:8123`; nftables drops LAN → `:8123`; which layer is load-bearing | Code done; **result PENDING (owner with Pi + a second LAN machine)** |
| [`ha_onboarding/`](ha_onboarding/README.md) | V2: headless HA onboarding + long-lived token from core, exact sequence recorded | Code done; **result PENDING (owner with Pi + pinned HA on a fresh volume)** |
| [`aec_reference/`](aec_reference/README.md) | V3: XVF3800 processed ASR beam + playback as AEC reference (`beepboop bench aec`) | Code done; **result PENDING (owner with Pi, array and a person)** |
| [`alsa_soak/`](alsa_soak/README.md) | V4: 24 h raw ALSA capture without device-busy errors or xruns > 1/h (plain and loaded variant) | Code done; **result PENDING (owner with Pi + XVF3800)** |
| [`otp_kek/`](otp_kek/README.md) | V5: OTP device key readable via `/dev/vcio`, stable KEK across reboots; passphrase fallback | Code done; **result PENDING (owner with Pi; programming OTP is irreversible)** |
| [`thermal_g7/`](thermal_g7/README.md) | G7: no `0x4` throttle bit during a 20 min `bench e2e` in the reference fan case | Code done; **result PENDING (owner with Pi in the fan case)** |

`ha_bind/` runs on the Pi host with the system `python3` (standard library only); `ha_onboarding/`
and `otp_kek/` run from the uv workspace (`uv run python ...`); `alsa_soak/` and `thermal_g7/`
are POSIX `sh`. Python probe logic is unit-tested on x86 (`uv run pytest tools/probes core/tests`).
The ordered owner procedure for all of M0 is [tools/M0_OWNER_CHECKLIST.md](../M0_OWNER_CHECKLIST.md).
