# tools/probes/thermal_g7: G7, no throttling during a 20-minute `bench e2e`

**Status: code done; result PENDING (owner with the Pi in the reference fan case).**

ROADMAP G7: no `0x4` (currently throttled) bit in `vcgencmd get_throttled` during a 20-minute
`bench e2e` with the reference fan case. OPERATIONS §8 also wants the temperature trace
(target: sustained < 70 °C) and the other bits (0x1 under-voltage, 0x2 ARM frequency capped,
0x8 soft temperature limit; bits 16-19 "since boot").

Two samplers record the same run:

- `beepboop bench thermal --duration 1200` (in the core container) runs the concurrent e2e
  for 20 minutes under `beepboop_bench.samplers.ThermalSampler`, which uses `vcgencmd` at 1 Hz
  *if* the container can reach `/dev/vchiq`; otherwise the section says `skipped` with the reason.
- `run_g7.sh` (this folder, on the host) samples `vcgencmd measure_temp` / `get_throttled` at
  1 Hz around that bench command and writes the authoritative verdict JSON.

## Procedure (owner)

```bash
# on the Pi host, SSD boot, reference fan case, HA running, the recorded corpus in place
sh tools/probes/thermal_g7/run_g7.sh -d 1200                      # runs `beepboop bench thermal`
sh tools/probes/thermal_g7/run_g7.sh -d 1200 -- beepboop bench thermal --duration 1200 --live
```

Output: `tools/bench/results/<date>-<host>-g7.json` with `pass`, `samples`, `temp_c_max`,
`temp_c_mean`, `throttle_bits_union`, `samples_with_0x4`, kernel and firmware; edit the `case`
field with the fan case model. The bench's own result file carries the per-turn waterfalls and,
when `vcgencmd` worked inside the container, `sections.thermal`.

If it fails: document which cases pass and make a stronger case the BOM default (ROADMAP G7).
Repeat on the 8 GB board; record both with dates in OPERATIONS §8 / ROADMAP.
