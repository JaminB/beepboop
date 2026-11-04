# tools/train_wakeword: the "Hey beepboop" microWakeWord model

> Status: M0 step 4 pipeline, written 2026-11-02. **No model has been trained yet.** The
> shipped `hey_beepboop_v1` is PENDING the owner's training run (§8). Everything here runs
> on a laptop or Colab, never on the Pi.

This directory regenerates the stock wake model from scratch: synthetic "Hey beepboop" clips
from many TTS voices, public negative corpora, augmentation, the
[microWakeWord](https://github.com/OHF-Voice/micro-wake-word) trainer, TFLite export with the
manifest `pymicro-wakeword` loads, and an evaluation report. Its contract is
[VOICE_PIPELINE §6](../../docs/VOICE_PIPELINE.md#6-wake-word-hey-beepboop) (engine, detection
parameters, acceptance targets, §6.4 pipeline) and
[ROADMAP M0](../../docs/ROADMAP.md#m0-prove-the-pi) gate G5. The licence record is
[DATA_LICENSES.md](DATA_LICENSES.md).

Contents:

| Path | What |
|---|---|
| `config/hey_beepboop.yaml` | the single source of truth for the run: phrases, voices, datasets, augmentation, trainer, export, evaluation |
| `train_wakeword/` | the pipeline package (`config.py` schema, `manifest.py`, `licenses.py`, one module per step, `_vendor.py` adapters) |
| `scripts/01_generate_positives.py` … `06_evaluate.py` | numbered entry points, thin wrappers over `train-wakeword <step>` |
| `Makefile` | `make synth / negatives / augment / train / export / eval` as VOICE_PIPELINE §6.4 names them |
| `pyproject.toml`, `uv.lock` | the stand-alone training environment (not a workspace member), every version pinned |
| `DATA_LICENSES.md` | every voice and dataset with its licence and whether the shipped model may use it |
| `tests/` | x86 smoke tests: config schema, manifest schema, licence cross-check, trainer YAML, eval maths |
| `data/` (git-ignored) | everything a run produces; layout in `train_wakeword/paths.py` |

## 1. Hardware and time expectations

All durations below are **estimates, not measurements**: this pipeline has not been run end to
end yet. They extrapolate from the upstream notebook (1 000 clips, 10 000 steps on a Colab T4)
and the dataset sizes.

| Step | Needs | Estimated time | Disk |
|---|---|---|---|
| 01 positives (20 000 clips + 4 000 near-misses) | CPU ok; a CUDA GPU makes the 904-speaker generator ~20× faster | GPU ~15–30 min; CPU 4–8 h (batch size 1) | ~2 GB |
| 02 negatives | bandwidth; `7z` for FSD50K | 30 GB LibriSpeech + 11 GB MUSAN + 19 GB FSD50K + 2.3 GB Speech Commands + 1.3 GB RIRs: 1–4 h download, 1–3 h conversion on 8 cores | ~90 GB raw, ~40 GB converted |
| 03 augment | CPU (8+ cores recommended) | 1–3 h | ~15 GB of mmaps |
| 04 train (30 000 steps) | GPU strongly recommended (any 8 GB CUDA card, or a Colab T4) | GPU 1–3 h; CPU 12–24 h | small |
| 05 export | nothing | seconds | < 1 MB |
| 06 evaluate | CPU | 10–30 min for ~10 h of ambient audio | small |

A quick end-to-end smoke run (§5) with the small datasets takes well under an hour on a laptop.

Platform: Linux x86_64 (bare metal, WSL2, or Colab). `ai-edge-litert` and modern TensorFlow
have no native Windows wheels, so on Windows use WSL2; macOS is untested (the upstream notebook
needs forks of two packages on Apple Silicon). Python 3.12 (`uv` installs it if missing).

## 2. Setup

```bash
cd tools/train_wakeword
make deps          # uv sync (creates ./.venv from uv.lock) + clones piper-sample-generator
make check         # validates config/hey_beepboop.yaml against DATA_LICENSES.md
```

`make deps` runs `uv sync` and clones
[rhasspy/piper-sample-generator](https://github.com/rhasspy/piper-sample-generator) at commit
`2971426` into `data/third_party/`. It is cloned rather than installed because the PyPI wheel
omits `piper_train/`, which the multi-speaker `.pt` generator imports; step 01 runs
`python -m piper_sample_generator` from that checkout. `uv sync` uses `uv.lock`, which pins
TensorFlow 2.19.1, torch 2.15.0, ai-edge-litert 2.3.0, pymicro-wakeword 2.5.0,
pymicro-features 2.0.2, piper-tts 1.3.0, audiomentations 0.33.0 and microwakeword at commit
`4665173` (138 packages). CPU-only torch: `uv sync --index https://download.pytorch.org/whl/cpu`
before anything else, or accept the CUDA wheels.

Two tools are needed outside Python: `git` and, for FSD50K's multi-part zip, `7z`
(`apt install p7zip-full`). VOiCES needs the AWS CLI (`--no-sign-request`, see the config note).

Colab: upload this directory (or clone the repo), `pip install uv`, then the same commands; put
`data/` on the mounted Drive so downloads survive the session.

## 3. The config

[config/hey_beepboop.yaml](config/hey_beepboop.yaml) is validated by the pydantic schema in
`train_wakeword/config.py` (`extra: forbid`, so typos fail fast). The parts that matter:

- **Phrases**: five spellings, three of them the IPA strings from VOICE_PIPELINE §6.4
  (`hˈeɪ bˈiːpbuːp`, `hˈeɪ bˈiːp bˈuːp`, clipped `hˈeɪ bipbup`) plus two text forms that let
  espeak choose its own phonemes. Speed variation comes from `length_scales` 0.8–1.3; pitch
  variation is applied by step 03's `PitchShift` because Piper has no pitch control.
- **Voices**: the 904-speaker LibriTTS-R generator (capped at 600 speakers) for 70 % of clips,
  eleven single-speaker Piper voices for the rest, so timbre as well as speaker varies. Three
  NC-licensed voices are listed with `shipped: false` for research runs only.
- **Near-misses**: the four from the spec ("hey people", "beat loop", "he bumped",
  "hey, be quick") plus twelve more, rendered with the same voices and trained as hard negatives.
- **Datasets**: each with a `role` (`speech`, `no_speech`, `background`, `rir`, `ambient_eval`),
  a `license_id` into DATA_LICENSES.md and a `shipped` flag. Everything cleared for shipping is
  CC BY, CC0, Apache-2.0 or public domain; see §7.
- **Profiles**: `--profile shipped` (default) silently drops every `shipped: false` voice and
  dataset; `--profile research` uses everything and taints the run so step 05 refuses to export
  it under the shippable name.
- **Trainer**: the upstream notebook's MixedNet architecture, 10 ms feature stride with
  `stride: 3` (one inference per 30 ms, VOICE_PIPELINE §6.1), two stages (20 000 + 10 000 steps)
  with the negative class weight raised in the second stage, best weights chosen by
  `ambient_false_positives_per_hour ≤ 0.5` then max `average_viable_recall`.
- **Export**: `probability_cutoff: 0.97` and `sliding_window_size: 5` from §17.1 as the starting
  point; step 06 recommends a value and step 05 accepts `--probability-cutoff`.

## 4. The steps

Every step is `train-wakeword <step>` (inside the venv: `uv run train-wakeword …`), or the
numbered script, or the Make target. Common options: `--config`, `--data-dir` (default
`./data`), `--profile`. Steps are idempotent: re-running skips what exists.

### 4.1 `01_generate_positives` (`make synth`)

Downloads the voice files, writes `data/near_misses/prompts.txt`, then runs the sample generator
once per phrase × voice group (the generator refuses to mix `.pt` and `.onnx` models in one
call). `--dry-run` prints the exact commands. Output: `data/positives/<phrase_id>/{generator,piper}/N.wav`
and `data/near_misses/{generator,piper}/N.wav`, 16 kHz mono. Finally a seeded 10 % of every
phrase directory moves to `data/positives_holdout/` and is never augmented or trained on.

### 4.2 `02_fetch_negatives` (`make negatives`)

Downloads each archive once into `data/negatives/_archives/`, records its sha256 in
`data/negatives/checksums.json` (the config's `sha256` fields are empty until the first real run
fills them; copy them in and commit), extracts, selects files by the dataset's `include`/`exclude`
globs and converts to 16 kHz mono PCM16 under `data/negatives/<id>/wav16k/`. FSD50K is filtered
to CC0 and CC BY clips from its own metadata. Manual datasets (VOiCES, the MIT IRs) are logged
as `PENDING manual download` with the instruction from the config and skipped; drop the archive
into `_archives/` and re-run. `--only <id> …` restricts the run.

### 4.3 `03_augment` (`make augment`)

Builds the feature sets the trainer reads, using microwakeword's own `Clips`, `Augmentation`
and `SpectrogramGeneration` so the features match what `pymicro-features` computes on the Pi:

- positives and near-misses: RIR (OpenSLR 28 real rooms) p=0.5, background noise from MUSAN,
  Speech Commands and FSD50K at 0–25 dB SNR p=0.75, gain −35–0 dB, pitch ±3 semitones p=0.25,
  EQ, band-stop, colour noise; 3.2 s windows with the phrase jittered to end ~200 ms before the
  edge; 90/5/5 train/validation/test; training split repeated twice with 10 sliding frames;
- speech and no-speech negatives: the same augmenter without jitter, cut into 5 s spectrograms;
- ambient evaluation streams: unaugmented, full length, 45 % to `validation_ambient` and 45 % to
  `testing_ambient` (the trainer slides over them to estimate false accepts per hour).

**XVF3800 frequency response (VOICE_PIPELINE §6.4) is PENDING an M0 owner sweep.** Record the
array's impulse response through the processed ASR beam, put the WAV(s) in a directory, set
`augment.array_ir_dir` and re-run; it joins the RIR pool. Until then the model is trained without
it, which §6.4 lists as part of the recipe.

Output: `data/features/<set>/{training,validation,testing}/<name>_mmap` and
`data/features/ambient/{validation_ambient,testing_ambient}/`, plus `data/features/MANIFEST.json`
(profile, pools, counts) which step 05 copies into the provenance.

### 4.4 `04_train` (`make train`)

Writes `data/trained_models/hey_beepboop_v1/training_parameters.yaml` from the config and runs
`python -m microwakeword.model_train_eval … mixednet …`. `--dry-run` writes the YAML and prints
the command. Training resumes from the last checkpoint by default (`--no-restore` starts over).
Watch it with `tensorboard --logdir data/trained_models/hey_beepboop_v1/logs`. The trainer leaves
`tflite_stream_state_internal_quant/stream_state_internal_quant.tflite` (int8, streaming) and a
`tflite_streaming_roc.txt` next to it.

### 4.5 `05_export` (`make export`)

Copies the quantised streaming model to `data/out/hey_beepboop_v1.tflite` and writes:

- `hey_beepboop_v1.json`: the v2 manifest (`type`, `wake_word`, `author`, `website`, `model`,
  `trained_languages`, `version: 2`, `micro.{probability_cutoff, feature_step_size,
  sliding_window_size, tensor_arena_size, minimum_esphome_version}`). pymicro-wakeword 2.5.0
  reads `model`, `wake_word`, `trained_languages`, `micro.probability_cutoff` and
  `micro.sliding_window_size`; the rest is for ESPHome satellites. The export loads the pair with
  pymicro-wakeword to prove it parses.
- `hey_beepboop_v1.provenance.json`: config hash, profile, every voice and dataset with its
  licence, trainer YAML, package versions, git commit, date.
- `hey_beepboop_v1.models-manifest-entry.yaml`: the row for `models/manifest.yaml`
  ([OPERATIONS §13](../../docs/OPERATIONS.md#13-model-and-licensing-hygiene)) with sha256 and size
  filled in and `url: PENDING` until the files are published as a release asset.

`--probability-cutoff 0.95` overrides the config value (use step 06's recommendation). A run built
under `--profile research` is refused unless `--allow-research`, and then exported as
`hey_beepboop_v1_research`.

### 4.6 `06_evaluate` (`make eval`)

Streams held-out audio through **pymicro-wakeword, the library the Pi runs**, one pass per file
with `sliding_window_size=1` to capture every raw 30 ms probability, then sweeps
`sliding_window_sizes × cutoffs` offline with the 2 s refractory from §6.2:

- false-reject rate on `data/positives_holdout/` (per phrase and per voice group) and, with
  `--real-clips <dir>` (one sub-directory per speaker), on consented real recordings;
- false accepts per hour on the `ambient_eval` datasets or `--ambient <dir> …`.

Output: `data/results/<date>-eval-<host>.json` with the full sweep table, per-group FRR, and a
`recommended` operating point: the highest cutoff that meets both targets
(`≤ 0.0208 FA/h`, i.e. 0.5 per 24 h, and `≤ 5 %` FRR). `--require-targets` makes a miss exit 1.
`--max-files N` for quick runs.

**Every report is labelled `synthetic / x86 / not a gate result`.** G5 (≤ 0.5 false accepts per
24 h on the soak corpus, ≤ 5 % false rejects at 3 m) is measured later by `beepboop bench wake`
on the 24 h living-room soak corpus and the 200 labelled clips recorded through the XVF3800
(VOICE_PIPELINE §16, CLAUDE.md §12 steps 9–10). This step tells you whether a model is worth
putting on the Pi; the bench decides.

## 5. Quick smoke run (small data, ~30–60 min on a laptop)

```bash
cd tools/train_wakeword && make deps
uv run train-wakeword check
uv run train-wakeword positives --dry-run                 # see the commands
uv run train-wakeword negatives --only librispeech_dev_clean librispeech_test_clean \
                                      speech_commands_v2 speech_commands_background \
                                      openslr28_rirs openslr28_noises
# a small positives run: edit target_count/near_miss_count or pass a copy of the config
uv run train-wakeword --config /tmp/small.yaml positives
uv run train-wakeword --config /tmp/small.yaml augment
uv run train-wakeword --config /tmp/small.yaml train       # set training_steps: [2000] in the copy
uv run train-wakeword --config /tmp/small.yaml export
uv run train-wakeword --config /tmp/small.yaml evaluate --max-files 200
```

The model from a smoke run is for checking the plumbing, not for shipping.

## 6. Full run, in order

```bash
make deps check
make synth                 # 01
make negatives             # 02 (fetch VOiCES manually first; see the config note)
make augment               # 03
make train                 # 04
make export                # 05
make eval                  # 06 -> read data/results/*.json, pick the cutoff
make export ARGS="--probability-cutoff <recommended>"
```

Then: publish `data/out/hey_beepboop_v1.{tflite,json}` as a release asset, paste the generated
entry into `models/manifest.yaml` with the real `url`, regenerate `models/LICENSES.md`, commit the
dataset sha256s into the config and the provenance JSON next to the results, and run
`beepboop bench wake` on the Pi for G5.

## 7. Licences and the two profiles

[DATA_LICENSES.md](DATA_LICENSES.md) is the record; `train-wakeword check` enforces it. Three
things to know:

### 7.1 Nothing NC feeds the shipped model

The upstream notebook's recipe uses datasets that are not cleared for a model beepboop
redistributes (the stock model is released under Apache-2.0, inside a GPL-3.0-or-later project): its
pre-generated negative features are CC BY-NC 4.0 (and contain CHiME-6 and WHAM! material),
`fma_xs` is mixed-licence music, AudioSet audio is YouTube content, the MIT IR Survey states no
licence, and several Piper voices (hfc_female, ryan, l2arctic) are NC. The notebook itself says
models trained that way are "suitable for non-commercial personal use". The shipped profile
replaces all of it with LibriSpeech, Speech Commands, VOiCES, MUSAN, OpenSLR 28 and a
licence-filtered FSD50K.

### 7.2 The lessac provenance (accepted by the owner, D1)

Most Piper English voices, including the 904-speaker LibriTTS-R generator, were fine-tuned from
the `lessac` voice, whose Blizzard 2013 corpus is licensed for research only. The audio those
voices were fine-tuned *on* is CC BY / CC0 / public domain, but the weights descend from a
research-only model, and the generated clips contain none of the lessac corpus.

**Accepted by the owner 2026-11-03 (D1, [ADR 0020](../../docs/adr/0020-gpl-3-and-espeak-phonemiser.md)):**
beepboop is a personal/research project, so the lessac-derived voices and the LibriTTS-R generator
are used for the stock model (`shipped: true` with a `(see note)` flag in the table), and the
same provenance is accepted for the default TTS voice
([VOICE_PIPELINE §11.1](../../docs/VOICE_PIPELINE.md#111-default-piper-voices-in-sherpa-onnx-sherpa-piper)).

The **strict profile** stays documented as an option, not the default, for anyone who wants a
model with no research-only ancestry (for example before a commercial use): set
`generator_share: 0` and mark the flagged voices `shipped: false`. The from-scratch voices
(ljspeech, cori, kristin, norman, bryce, john) then supply every clip, at the cost of speaker
diversity (6 voices instead of ~700). That trade-off is the main reason to collect real clips for
the bench corpus early.

### 7.3 Research profile

`--profile research` adds the NC voices and the MIT IRs. To also use the upstream negative
feature sets (CC BY-NC), download `speech.zip`, `no_speech.zip`, `dinner_party.zip` and
`dinner_party_eval.zip` from `huggingface.co/datasets/kahrendt/microwakeword`, unzip them under
`data/features/speech/`, `…/no_speech/` and `…/ambient/` (they already have the
`training/validation/testing[_ambient]` layout), and run from step 04. Step 05 will only export
such a model as `hey_beepboop_v1_research`; it never ships.

## 8. What is pending on the owner

| Item | Why it cannot be done here |
|---|---|
| The training run itself (steps 01–06) | hours of GPU time and ~100 GB of downloads; the shipped `hey_beepboop_v1.tflite` does not exist until it is run |
| Dataset sha256s in the config | recorded by step 02 on first download; never invented |
| VOiCES and MIT IR downloads | no direct URL (S3 bucket; unlicensed mirror) |
| XVF3800 impulse response | needs the array (VOICE_PIPELINE §6.4, M0 sweep) |
| G5 numbers | `beepboop bench wake` on the soak corpus and the 200-clip fixture, recorded on the Pi (CLAUDE.md §12 steps 9–10) |
| Publishing the model | release asset + `models/manifest.yaml` row + `models/LICENSES.md` regeneration |

## 9. Household fine-tuning (SEED-style): v0.2+ only, ADR-gated

The research notes and ADR 0009 mention fine-tuning the stock model on 50–200 real household
recordings, the way the community SEED recipe augments ~180 real samples into a training set.
**This is not possible in v0.1 and is not implemented here.**
[ADR 0018](../../docs/adr/0018-no-audio-retention.md) is absolute: the device never persists audio,
so there are no household clips to fine-tune on: no enrollment clips, no false-wake queue, no
debug captures. The only real recordings that exist are the consented bench fixtures made with
`tools/bench/record_fixture.py` on the contributor's own machine, and those are for measuring G5,
not for training the shipped model.

If a v0.2 ADR supersedes 0018 with an opt-in, bounded, encrypted false-wake queue (the ROADMAP
carries a one-line note), a `07_finetune` step could consume clips exported from that queue plus
the positives here. It needs that ADR first, a consent and export path in the web UI, and a
decision on whether a household-tuned model is ever redistributed (it should not be). Nothing in
this directory should be read as pre-approving any of that.

## 10. Spec issues found while building this

1. **AudioSet and MUSAN** (VOICE_PIPELINE §6.4): AudioSet's audio has no redistribution licence,
   so it cannot feed the shipped model; MUSAN (CC BY 4.0) stays. Reported, not changed (doc is
   outside this step's paths). *Resolved 2026-11-03: §6.4 rewritten.*
2. **`make negatives` "downloads microWakeWord's pre-generated negative features"** (§6.4): those
   features are CC BY-NC 4.0 and include CHiME-6 and WHAM!. The shipped profile generates its own
   negative features from CC BY / CC0 / Apache sources instead; the pre-generated sets remain
   usable in the research profile (§7.3). §6.4 should say so. *Resolved 2026-11-03.*
3. **`models/LICENSES.md` as the home for voice/RIR/noise licences** (§6.4): OPERATIONS §13 says
   that file is *generated* from `models/manifest.yaml`, one row per artefact. The training data
   record therefore lives here in `DATA_LICENSES.md`, referenced from the model's manifest
   `attribution`; §6.4 should point here. *Resolved 2026-11-03.*
4. **Piper voice provenance**: the lessac research-only base behind most English Piper voices
   (§7.2) was not mentioned anywhere in the spec. *Resolved 2026-11-03: accepted by the owner
   (D1, ADR 0020); recorded in OPERATIONS §13, VOICE_PIPELINE §6.4 and §11.1.*
5. **`make eval` "fails the build on any missed target"** (§6.4): targets are G5 numbers that only
   the soak corpus can produce. Step 06 reports synthetic estimates and only fails with
   `--require-targets`, so a CI run on synthetic data cannot claim or deny a gate.
   *Resolved 2026-11-03.*
6. **Pitch shifts in `make synth`** (§6.4): Piper cannot shift pitch; it happens in `make augment`.
   Same clips, different step. *Resolved 2026-11-03.*

## 11. Known upstream quirks (pinned commit `4665173`)

- `mixednet.py`'s default `--residual_connection` has five entries while the other lists have
  four, so the defaults alone raise; this pipeline always passes all five flags explicitly.
- `train.py` never assigns `recall_at_no_faph` if no cutoff reaches zero false accepts on the
  ambient validation set; keep `validation_ambient` reasonably sized (hours, not minutes) so a
  cutoff exists.
- `piper_sample_generator` from PyPI lacks `piper_train`; use the clone (`make deps`).
- `microwakeword`'s `setup.py` lists `audio_metadata`, whose PyPI release is broken on current
  `attrs`; `pyproject.toml` pins the fork the upstream notebook installs.
- `Clips` needs `random_split_seed` set to produce splits at all; every call here sets it from
  `positives.seed`.

## 12. Tests

```bash
# from the repo root, in the workspace venv (no training deps needed):
uv run pytest tools/train_wakeword/tests
uv run ruff check tools/train_wakeword && uv run pyright -p tools/train_wakeword
# inside the training venv the one skipped test also runs (adapter vs upstream signatures):
cd tools/train_wakeword && uv run pytest tests
```
