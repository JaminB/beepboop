# tools/bench: `beepboop bench`

The concurrent pipeline benchmark and the M0 go/no-go gates (M0 steps 5, 6 and the tooling for
9-12). It is the `beepboop-bench` workspace package (import `beepboop_bench`) with no console
script of its own: core mounts `beepboop_bench.cli:app` as `beepboop bench`.

- What each subcommand reports: [OPERATIONS §11](../../docs/OPERATIONS.md#11-observability).
- Gate thresholds G1-G7: only in [ROADMAP M0](../../docs/ROADMAP.md#m0-prove-the-pi);
  `beepboop_bench/gates.py` transcribes them and is tested against those numbers.
- Results: [`results/`](results/README.md) (`<date>-<host>.json`, committed).
- Corpus: `corpus/` (recorded on the owner's bench machine with `record_fixture.py`; git-ignored).
- Synthetic CI bench: [`synthetic/`](synthetic/README.md).

## Run

```bash
uv sync --all-packages --all-extras            # --all-extras: bench-only phonemizer for TTS
uv run python -m beepboop_bench.models fetch   # pinned weights -> models/ (sha256-verified)
uv run beepboop bench --help
uv run beepboop bench speaker|stt|tts|wake|aec|endpoint|llm|e2e|ram|thermal
uv run beepboop bench all --gates              # Pi + real corpus only; exits non-zero on a failure
uv run beepboop bench all --gates --allow-non-pi --duration 60   # dev: "not a gate result"
uv run pytest tools/bench
```

Every run prints the p50/p90/p95 tables and writes `results/<date>-<host>.json`; `--json`
also prints the JSON. `--gates` is accepted on `all` only. The judge **refuses** to run unless
the host is a Raspberry Pi (linux/aarch64 with a Raspberry Pi device tree) and the corpus was
recorded through the XVF3800; `--allow-non-pi` lets a development run through with the file
labelled `not a gate result`, and in that mode no gate can end as `pass` (at best
`incomplete: checks met but run preconditions unmet`). A gate result also needs the **4 GB**
board, HA running (`--ha-running`) and the fast-path turns offline (`--offline`), as ROADMAP
states; the 8 GB run is not refused but is "recorded alongside" (`not a gate result`, every gate
at best `incomplete`), so `--gates` exits 1 there by design.

G4 needs two 24 h soaks (`bench ram --profile default`, then `--profile music`) judged together.
Each run writes its own file; `--ram-from <file>` (on `ram` and `all`, repeatable) copies the
RAM profiles of an earlier file into the current result so one file holds both. It only accepts
a file from the same host and RAM variant, and a `gate` file when the current run is a gate run;
every import is noted in `sections.ram.notes`.

## Layout

| Module | What |
|---|---|
| `cli.py` | The Typer app. Cheap to import (core loads it on every start); heavy imports live inside commands. |
| `run.py` | Orchestration: options, the gate refusal, running sections in OPERATIONS §11 order, result naming, rich tables. |
| `gates.py` | G1-G7 as data (`Check` paths into the result JSON, thresholds, preconditions, G3's arbiter) and `evaluate()`. |
| `schema.py` | `BenchResult`, one section per subcommand, `Turn` waterfalls with the OPERATIONS §11 stage names, deterministic JSON Schema. |
| `stats.py` | `Summary` (p50/p90/p95) and the ROADMAP statistic rule. |
| `host.py` | Host facts: board model, MemTotal, kernel, `vcgencmd get_throttled` bits, "is this a Pi?". |
| `models.py` | `models/manifest.yaml` loader, fetcher (https + sha256 + safe tar extraction), `LICENSES.md` generator. `python -m beepboop_bench.models fetch\|verify\|licenses`. |
| `corpus.py` | The corpus manifest schema (clip kinds, labels) and loader. |
| `runtime.py` | The only module importing sherpa-onnx / pymicro-wakeword: typed shims with explicit thread caps. |
| `phonemize.py` | Lazy `piper_phonemize` wrapper (sherpa-onnx 2.0 has no built-in espeak frontend). |
| `stages.py` | `bench speaker\|stt\|tts\|wake\|endpoint`: real models, real clips, `perf_counter` around the inference the product would run. |
| `e2e.py` | Concurrent realtime replay: VAD -> hangover -> STT flush and speaker embed in parallel -> fast path or LLM -> TTS; one `Turn` per clip. |
| `samplers.py` | 1 Hz RAM (`MemAvailable`, zram, OOM kills from the kernel log) and thermal (`vcgencmd`) samplers; no-ops with a reason off the Pi. |
| `llm.py` | `bench llm`: lazy wrapper over core's Bedrock probe; `skipped` unless `--llm-live`. |
| `aec.py` | `bench aec` (V3): the live procedure through `arecord`/`aplay`; `pending` off Linux. |
| `synthetic.py` | TTS-generated corpus for CI. |
| `testing.py` | Builders used by tests. |

## Corpus manifest (`corpus/manifest.yaml`)

```yaml
schema_version: 1
name: household-xvf3800
synthetic: false              # true -> never a gate result
recorded_through: xvf3800     # xvf3800 | synthetic-tts | other-mic
sample_rate: 16000
capture_channel: 0            # the processed ASR beam
consent: "..."                # the text every speaker agreed to
speakers: [{id: alex, kind: adult}, {id: sam, kind: child}]
clips:
  - {id: alex_quiet_command000, path: clips/alex_quiet_command000.wav, kind: command,
     speaker: alex, transcript: "turn off the kitchen lights", scenario: fast_path,
     distance_m: 1.0, ambient: quiet, speech_end_s: 1.84}
  - {id: alex_quiet_wake070, path: clips/..., kind: wake, speaker: alex, distance_m: 3.0,
     phrase_end_s: 1.1}
  - {id: soak0000, path: clips/soak0000.wav, kind: soak}          # no wake word, 10 min
  - {id: stranger01, path: clips/..., kind: negative, speaker: stranger, transcript: "..."}
```

`kind` is `command | wake | soak | negative | enroll`; `scenario` is `fast_path | command_1s |
no_tool | tool` (G6's "1 s command" case is `command_1s`). `speech_end_s` (hand-checked) is clock
zero for `bench endpoint`; `phrase_end_s` is the end of "boop" for wake-hit latency.

## What the e2e turn measures (and what it cannot, off the Pi)

Clock zero is the last VAD-positive frame. The worker feeds 10 ms frames at real-time pace into
Silero VAD and the streaming zipformer, waits the 250 ms hangover (`endpoint`), flushes STT with
0.3 s of silence (`stt_tail`) while the CAM++ embedding of the last 2 s runs on another thread
(`speaker_final`), then either serves the cached "Done." clip (fast path: `first_speech`) or,
with `--live`, calls Bedrock with the probe's turn-shaped requests (`llm_ttft`,
`llm_first_sentence`, the `ack` earcon as `first_audio` on tool turns) and synthesises the first
sentence (`tts_first_chunk`, `first_speech`). Without `--live` the LLM turns report local stages
only and no `first_speech`. Not measured anywhere but the Pi: the output buffer and DAC, the wake
earcon by loopback, the HA state storm; each is listed in the section notes.

## Pending on hardware (owner actions)

| Step | What | How |
|---|---|---|
| 9 | Record the corpus (>= 6 speakers, >= 200 utterances, 24 h soak) through the XVF3800 | `record_fixture.py` on the bench laptop; copy `corpus/` to the Pi |
| 10 | Gate runs on the 4 GB Pi (the gate board) and the 8 GB Pi (recorded alongside) with HA running | `beepboop bench all --gates --ha-running --offline --live` (fast-path turns with the network unplugged, then LLM turns online), then the two 24 h `bench ram` soaks merged with `--ram-from`; on each board; commit `results/` |
| 11 | V3 AEC reference, V4 24 h ALSA soak | [`tools/probes/aec_reference/`](../probes/aec_reference/README.md), [`tools/probes/alsa_soak/`](../probes/alsa_soak/README.md) |
| 12 | G7 thermal: 20 min `bench e2e` in the reference fan case | `beepboop bench thermal` plus the host-side sampler in [`tools/probes/thermal_g7/`](../probes/thermal_g7/README.md) |
| 4 -> 5 | Publish the stock `hey_beepboop` model | pin its url + sha256 in `models/manifest.yaml`; until then `bench wake` uses the pymicro-wakeword builtin and says so (`stock_model: false`) |
