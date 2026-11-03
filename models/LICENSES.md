# Model licences

Generated from [`manifest.yaml`](manifest.yaml) by
`python -m beepboop_bench.models licenses`. Do not edit by hand; edit the manifest and
regenerate. CI compares this file with a fresh render and fails when it is stale, when a
licence is missing or `UNKNOWN`, or when one is on the deny list (`*-NC-*`, `GPL-*`)
([OPERATIONS §13](../docs/OPERATIONS.md#13-model-and-licensing-hygiene)).

Weights are downloaded at install into this folder (git-ignored); only this file and the
manifest are tracked. The runtime for every model below is sherpa-onnx (Apache-2.0) except
the wake word, which runs in `pymicro-wakeword` (Apache-2.0).

| id | kind | licence | attribution | source | sha256 | status |
|---|---|---|---|---|---|---|
| `wespeaker-en-voxceleb-campp` | speaker | CC-BY-4.0 | WeSpeaker (wenet-e2e) CAM++ trained on VoxCeleb; ONNX export published by the sherpa-onnx project (Apache-2.0 code) | [https://github.com/wenet-e2e/wespeaker](https://github.com/wenet-e2e/wespeaker) | `c46fad10b5f81e1a…` | pinned |
| `sherpa-onnx-streaming-zipformer-en-20m-2023-02-17` | stt | Apache-2.0 | icefall / k2-fsa streaming zipformer transducer, English, ~20 M params, trained on LibriSpeech; exported by the sherpa-onnx project | [https://github.com/k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | `9c559283e8498d3f…` | pinned |
| `vits-piper-en_US-lessac-low` | tts | LicenseRef-Blizzard-2013-Lessac | Piper voice en_US-lessac-low (rhasspy/piper, MIT code) trained on the Lessac Blizzard 2013 corpus; packaged for sherpa-onnx with espeak-ng-data (GPL-3.0 data files read, not linked) | [https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/low](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/low) | `8fb427b863733407…` | pinned |
| `silero-vad` | vad | MIT | Silero VAD (Silero Team), ONNX as redistributed by sherpa-onnx | [https://github.com/snakers4/silero-vad](https://github.com/snakers4/silero-vad) | `9e2449e1087496d8…` | pinned |
| `hey_beepboop` | wake | Apache-2.0 | Stock "Hey beepboop" microWakeWord model trained by tools/train_wakeword/ (training-data licences in tools/train_wakeword/DATA_LICENSES.md) | tools/train_wakeword | `—` | pending |

## Notes

- **hey_beepboop**: No url or sha256 yet: the first stock model is produced by tools/train_wakeword (M0 step 4) and pinned here when published. Until then bench wake reports with the pymicro-wakeword builtin 'okay_nabu' model, flagged stock_model=false (never a G5 result).
- **sherpa-onnx-streaming-zipformer-en-20m-2023-02-17**: The small streaming English model of VOICE_PIPELINE §10.1; int8 encoder/decoder/joiner are the default, fp32 ships in the same archive. The archive also carries test_wavs/ (LibriSpeech, CC-BY-4.0).
- **silero-vad**: Silero VAD v4 ONNX, 16 kHz, 512-sample (32 ms) windows (VOICE_PIPELINE §7.1).
- **vits-piper-en_US-lessac-low**: OWNER REVIEW NEEDED before a release: the voice's dataset licence is the Blizzard 2013 Lessac licence (https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html), not an SPDX identifier. The docs picked this voice as the M0 default (VOICE_PIPELINE §11.1); swapping voices is a manifest edit.
- **wespeaker-en-voxceleb-campp**: fp32, 16 kHz, the VOICE_PIPELINE §8.1 model. sherpa-onnx reports its embedding dim as 512 (measured 2026-11-02 on this file; the 3D-Speaker English CAM++ export is 512-d too), not the 192 the docs state: a spec issue for VOICE_PIPELINE §8.1 and the hello.speaker.dim contract. The int8 G1 fallback is not published by sherpa-onnx; add an entry if one is exported. (The release tag really is spelled 'recongition'.)
