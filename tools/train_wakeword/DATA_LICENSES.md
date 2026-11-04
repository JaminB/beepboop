# Training-data licences for the "Hey beepboop" model

> Status: recorded 2026-11-02 from each source's own licence page or model card (links in the
> `source` column). No training run has happened yet; this table is the gate the run must pass.

beepboop is GPL-3.0-or-later and redistributes the stock wake model (released under Apache-2.0,
see below), so **everything that feeds the shipped model must permit redistribution, and its
licence must name no NC, research-only or unknown terms**. The one exception is provenance, not
data: the lessac-derived voices flagged below were accepted by the owner on 2026-11-03 (D1,
[ADR 0020](../../docs/adr/0020-gpl-3-and-espeak-phonemiser.md)) for this personal/research
project. Rows with `shipped_ok = no` may
only be used under `--profile research` (experiments, never exported as `hey_beepboop_vN`).
`train-wakeword check` (and the smoke tests) read this table: every `license_id` in
[config/hey_beepboop.yaml](config/hey_beepboop.yaml) must have a row, every `shipped: true` item
must sit on a `yes` row, and a `yes` row may not carry an NC / research-only / unknown licence.

The columns are fixed (`train_wakeword/licenses.py` parses them): `id`, `what`, `licence`,
`shipped_ok`, `source`, `notes`. Keep one row per line.

| id | what | licence | shipped_ok | source | notes |
|---|---|---|---|---|---|
| `libritts_r_generator` | Piper sample generator checkpoint `en_US-libritts_r-medium.pt`, 904 LibriTTS-R speakers | CC BY 4.0 (LibriTTS-R, openslr.org/141); generator code MIT | yes (see note) | https://github.com/rhasspy/piper-sample-generator/releases/tag/v2.0.0 · model card https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/libritts_r/medium/MODEL_CARD | PROVENANCE FLAG: the model card says it was fine-tuned from the `lessac` medium voice, whose corpus (Blizzard 2013) is research-only. The weights are a derivative of a research-only model even though the LibriTTS-R audio is CC BY 4.0. Accepted by owner 2026-11-03 (D1); the strict alternative, kept as an option, is `generator_share: 0`, which uses only the from-scratch voices below (README §7.2). |
| `piper_ljspeech` | Piper voice `en_US-ljspeech-medium` (1 speaker, female) | Public domain (LJ Speech); trained from scratch | yes | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/ljspeech/medium/MODEL_CARD | Not a lessac derivative. |
| `piper_kristin` | Piper voice `en_US-kristin-medium` (1 speaker, female) | Public domain (LibriVox recordings); trained from scratch | yes | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/kristin/medium/MODEL_CARD | Not a lessac derivative. |
| `piper_norman` | Piper voice `en_US-norman-medium` (1 speaker, male) | Public domain (LibriVox recordings); trained from scratch | yes | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/norman/medium/MODEL_CARD | Not a lessac derivative. |
| `piper_john` | Piper voice `en_US-john-medium` (1 speaker, male) | Public domain (LibriVox recordings); fine-tuned from `kristin` | yes | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/john/medium/MODEL_CARD | Not a lessac derivative (kristin is from scratch). |
| `piper_bryce` | Piper voice `en_US-bryce-medium` (1 speaker, male, the author's own voice) | Public domain; fine-tuned from an unreleased base trained by the same author | yes | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/bryce/medium/MODEL_CARD | Base voice unreleased; author states public domain. |
| `piper_joe` | Piper voice `en_US-joe-medium` (1 speaker, male) | CC0 (OHF-Voice voice-datasets); fine-tuned from `lessac` | yes (see note) | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/joe/medium/MODEL_CARD | Same lessac-derivative provenance flag as `libritts_r_generator`. Accepted by owner 2026-11-03 (D1). |
| `piper_kathleen` | Piper voice `en_US-kathleen-low` (1 speaker, female, 16 kHz) | CC0 (rhasspy/dataset-voice-kathleen); fine-tuned from `ryan` low | yes (see note) | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/kathleen/low/MODEL_CARD | Fine-tuned from `ryan`, whose corpus is CC BY-NC-SA: same class of provenance flag, accepted by owner 2026-11-03 (D1). |
| `piper_cori` | Piper voice `en_GB-cori-medium` (1 speaker, female) | Public domain (LibriVox recordings); trained from scratch | yes | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_GB/cori/medium/MODEL_CARD | Not a lessac derivative. |
| `piper_aru` | Piper voice `en_GB-aru-medium` (12 speakers) | CC BY 4.0 (University of Liverpool ARU corpus); fine-tuned from `lessac` | yes (see note) | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_GB/aru/medium/MODEL_CARD | Lessac-derivative provenance flag. Accepted by owner 2026-11-03 (D1). |
| `piper_alba` | Piper voice `en_GB-alba-medium` (1 speaker, female) | CC BY 4.0 (Edinburgh DataShare); fine-tuned from `lessac` | yes (see note) | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_GB/alba/medium/MODEL_CARD | Lessac-derivative provenance flag. Accepted by owner 2026-11-03 (D1). |
| `piper_vctk` | Piper voice `en_GB-vctk-medium` (109 speakers) | CC BY 4.0 (VCTK); fine-tuned from `lessac` | yes (see note) | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_GB/vctk/medium/MODEL_CARD | Lessac-derivative provenance flag. Accepted by owner 2026-11-03 (D1). |
| `piper_hfc_female` | Piper voice `en_US-hfc_female-medium` | CC BY-NC-SA 4.0 (Hi-Fi Captain) | no | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/hfc_female/medium/MODEL_CARD | Research profile only. |
| `piper_ryan` | Piper voice `en_US-ryan-medium` | CC BY-NC-SA 4.0 (RyanSpeech) | no | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/ryan/medium/MODEL_CARD | Research profile only. |
| `piper_l2arctic` | Piper voice `en_US-l2arctic-medium` (24 non-native speakers) | CC BY-NC 4.0 (L2-ARCTIC) | no | https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/l2arctic/medium/MODEL_CARD | Research profile only; valuable accent coverage we cannot ship. |
| `piper_lessac` | Piper voice `en_US-lessac-*` (the base many voices are fine-tuned from) | Blizzard 2013 lessac corpus: research purposes only, no commercial use, no redistribution | no | https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html | Never used directly in training. Listed because other voices derive from it (see flags above). The runtime default TTS voice `en_US-lessac-low` is this voice; D1 accepts it as well (`models/manifest.yaml`). |
| `librispeech` | LibriSpeech `train-other-500`, `dev-clean`, `test-clean` (read audiobooks) | CC BY 4.0 | yes | https://www.openslr.org/12/ | Speech negatives and the ambient evaluation stream. Attribution: Panayotov et al. 2015. |
| `speech_commands` | Google Speech Commands v0.02 (105 829 one-second words + `_background_noise_`) | CC BY 4.0 | yes | https://huggingface.co/datasets/google/speech_commands · http://download.tensorflow.org/data/speech_commands_v0.02.tar.gz | Short-word negatives and background noises. Attribution: Warden 2018. |
| `voices` | VOiCES devkit (far-field read speech with TV, music, babble, HVAC distractors) | CC BY 4.0 | yes | https://iqtlabs.github.io/voices/ (public S3 bucket `lab41openaudiocorpus`) | Manual download (no direct URL). Attribution: Richey et al. 2018. |
| `musan` | MUSAN: music, speech and noise corpus (openslr 17) | CC BY 4.0 | yes | https://www.openslr.org/17/ | Music + noise as no-speech negatives/backgrounds; speech subset as ambient evaluation. Attribution: Snyder, Chen, Povey 2015. |
| `openslr28` | RIRs and noises (openslr 28): real RIRs from RWCP, REVERB 2014 and AIR; point-source and isotropic noises | Apache-2.0 | yes | https://www.openslr.org/28/ | Attribution: Ko et al. 2017. |
| `fsd50k` | FSD50K dev set, **filtered to clips whose own Freesound licence is CC0 or CC BY** | CC0 / CC BY (per clip; the dataset-level licence is CC BY) | yes | https://zenodo.org/records/4060432 | `02_fetch_negatives` drops the 4 616 CC BY-NC and 1 374 Sampling+ dev clips using `dev_clips_info_FSD50K.json`. Attribution: Fonseca et al. 2022 plus per-clip uploader credit (kept in the metadata). |
| `mit_ir_survey` | MIT Impulse Response Survey (271 IRs, Traer and McDermott 2016) | not stated by the source | no | https://mcdermottlab.mit.edu/Reverb/IR_Survey.html · mirror https://huggingface.co/datasets/davidscripka/MIT_environmental_impulse_responses | The mirror's card says "check the original source"; the original states no licence. Research profile only until the authors confirm terms. |
| `kahrendt_negatives` | microWakeWord's pre-generated negative *features* (`speech`, `no_speech`, `dinner_party`, `dinner_party_eval`), built from CHiME-6, DipCo, FMA, FSD50K, WHAM!, LibriSpeech, VOiCES | CC BY-NC 4.0 | no | https://huggingface.co/datasets/kahrendt/microwakeword | What the upstream notebook uses. Contains CHiME-6 (commercial licence required) and WHAM! (CC BY-NC 4.0) material. Research profile only, README §7.3. |
| `chime6` | CHiME-5/6 dinner-party recordings | Non-commercial licence; commercial licence costs 2 000 GBP | no | https://chimechallenge.github.io/chime6/download.html | Not used. Listed because it is inside `kahrendt_negatives`. |
| `wham` | WHAM! ambient noise | CC BY-NC 4.0 | no | http://wham.whisper.ai/ | Not used. Listed because it is inside `kahrendt_negatives`. |
| `fma` | Free Music Archive (`fma_xs`, `fma_medium`) | Mixed per-track Creative Commons, including NC and ND tracks | no | https://github.com/mdeff/fma | Not used (the notebook's `fma_16k` background set). MUSAN music replaces it. |
| `audioset` | AudioSet balanced train (`agkphysics/AudioSet` mirror) | Labels CC BY 4.0; the audio is YouTube content with no redistribution licence | no | https://research.google.com/audioset/ · https://huggingface.co/datasets/agkphysics/AudioSet | VOICE_PIPELINE §6.4 names AudioSet subsets; not usable for a shipped model, reported as a spec issue (README §10). |
| `common_voice` | Mozilla Common Voice (English) | CC0 | yes | https://commonvoice.mozilla.org/en/datasets | Permitted but not wired in: requires an account and click-through terms. Add a `manual: true` dataset row if more speech negatives are wanted. |
| `demand` | DEMAND environmental noise | CC BY-SA 3.0 | no | https://zenodo.org/records/1227121 | Still excluded under GPL-3.0-or-later. If a trained model is an adaptation of its training data (unsettled), BY-SA 3.0 would force the model under BY-SA, which conflicts with its Apache-2.0 release, and CC BY-SA 3.0 is not GPL-compatible either (only BY-SA 4.0 is, one-way). MUSAN and FSD50K cover the same noise classes, so keeping it out costs little. |

## Tooling licences (not training data; nothing here ships on the Pi)

| Tool | Licence | Role |
|---|---|---|
| microWakeWord (`OHF-Voice/micro-wake-word`, pinned commit in `pyproject.toml`) | Apache-2.0 | trainer, feature extraction, augmentation wrapper |
| piper-sample-generator (`rhasspy/piper-sample-generator`, pinned commit) | MIT | synthetic clip generation |
| piper-tts 1.3.0 (`piper1-gpl`) | GPL-3.0-or-later | TTS engine used inside this offline training tool, in its own venv, via subprocess. Its licence is compatible with the project's GPL-3.0-or-later (ADR 0020); it is not in the Pi image only because sherpa-onnx already runs the voices. Its output WAVs carry the voice licences above, not the GPL. |
| pymicro-wakeword 2.5.0 | Apache-2.0 | the Pi's inference library; used by `06_evaluate` so the numbers come from the production code path |
| audiomentations 0.33.0 | MIT | augmentation transforms |
| TensorFlow, ai-edge-litert | Apache-2.0 | training and TFLite conversion |

## The shipped model

The exported `hey_beepboop_vN.tflite` is released under Apache-2.0 (like the upstream
`micro-wake-word-models`), with this file as its training-data record. Its row in
`models/manifest.yaml` (OPERATIONS §13) is generated by `05_export` and points back here;
`models/LICENSES.md` is generated from that manifest, so the attribution lines above must survive
into the manifest entry's `attribution` field when the owner publishes the model.
