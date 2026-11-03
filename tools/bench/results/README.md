# tools/bench/results

One JSON file per `beepboop bench` run, named `<date>-<host>[-synthetic][-N].json`; nothing is
ever overwritten. The shape is `beepboop_bench.schema.BenchResult` (`schema_version: 1`); every
file carries `measurement` (`"gate"` or `"not a gate result"`), a free-text `label`, the host
facts (board model, RAM, kernel, throttle bits) and the corpus it ran against.

Docs cite numbers from these files with the date and Pi variant (CLAUDE.md §13). Only files
with `"measurement": "gate"` may back a G1-G7 claim: the **4 GB** Raspberry Pi 4, the XVF3800
corpus, HA running (ROADMAP M0). The 8 GB run is a real run but is "recorded alongside": its
file says `not a gate result` and its gate table can only show `incomplete: checks met but run
preconditions unmet`. Everything else is a development run. `beepboop llm probe` writes its own
shape (`ProbeResult`, `measurement: preliminary | gate`); `--gate` is refused off the Pi.

| File | What |
|---|---|
| `2026-11-03-desktop-n9qe44h-synthetic.json` | **synthetic / x86 / not a gate result**: the full harness on a Windows 11 x86_64 dev box with the TTS-generated corpus, `--gates --allow-non-pi` so the gate table is exercised (every gate ends `incomplete`/`skipped`, never `pass`). |
| `2026-11-02-workstation-g3-preliminary.json` | Bedrock G3 probe from a workstation (`beepboop llm probe`), **preliminary, not a gate measurement**; its `notes` say which scenarios have no valid sample. |
| *pending* `<date>-pi4-4gb.json` | **Owner with Pi:** `beepboop bench all --gates --ha-running --offline --live` with the recorded corpus, plus the two 24 h `bench ram` soaks merged in with `--ram-from` (M0 step 10). The only file that can say `pass`. |
| *pending* `<date>-pi4-8gb.json` | **Owner with Pi:** the same on the 8 GB board, recorded alongside (`not a gate result` by construction). |
| *pending* `<date>-pi4-4gb-g3.json`, `<date>-pi4-8gb-g3.json` | **Owner with Pi:** `beepboop llm probe --gate` from the Pi on the home connection (G3). |
| *pending* `<date>-<host>-v1.json` … `-v5.json`, `-g7.json` | **Owner with Pi:** the [probes](../../probes/README.md) (V1-V5, G7). |
