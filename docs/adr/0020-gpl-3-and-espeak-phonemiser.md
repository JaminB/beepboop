# 0020. GPL-3.0-or-later; espeak-ng phonemiser in the TTS path

> Status: Accepted · 2026-11-03 (owner decision) · Applies to: M0, v0.1, v0.2, v0.3+

beepboop is licensed GPL-3.0-or-later. `piper-phonemize` (espeak-ng, GPL-3.0) is a normal runtime dependency of the audio service's Piper TTS path, and the lessac-derived Piper voices are accepted (D1).

Related: [LICENSE](../../LICENSE) · [docs/OPERATIONS.md §13](../OPERATIONS.md#13-model-and-licensing-hygiene) · [docs/VOICE_PIPELINE.md §11.1](../VOICE_PIPELINE.md#111-default-piper-voices-in-sherpa-onnx-sherpa-piper) · [ADR 0009](0009-microwakeword-and-sherpa-onnx.md) · [ADR 0016](0016-docker-compose-mit.md)

## Context

The M0 bench found that sherpa-onnx 2.x dropped its built-in espeak-ng frontend for Piper voices ("`--vits-data-dir` is deprecated and ignored in sherpa-onnx >= v2.0.0"): text must be phonemised outside the runtime and passed as `GenerationConfig.phoneme_codepoints`. The only maintained phonemiser for Piper voices is espeak-ng, GPL-3.0, wrapped by `piper-phonemize` (cp312 wheels for aarch64, x86_64 and Windows). Under MIT ([ADR 0016](0016-docker-compose-mit.md)) and "no GPL code in a beepboop process" ([ADR 0009](0009-microwakeword-and-sherpa-onnx.md)), the product had no TTS path. beepboop is a personal/research project; maximum permissive reuse is not a goal. Most English Piper voices, including the default `en_US-lessac-low` and the LibriTTS-R sample generator, descend from the Blizzard 2013 Lessac corpus, licensed for research only.

## Decision

The owner decided on 2026-11-03:

1. **Licence.** The project is GPL-3.0-or-later (`LICENSE` is the GPL-3.0 text; every `pyproject.toml` says `GPL-3.0-or-later`). Contributions and in-repo modules are GPL-3.0-or-later; a module's own licence must be GPL-3.0-compatible.
2. **Phonemiser.** The TTS pipeline is text → normalise → `piper-phonemize` (espeak-ng) → phoneme codepoints → sherpa-onnx `OfflineTts`. `piper-phonemize==1.5.9` is pinned in `audio/pyproject.toml` from the k2-fsa wheel index; `espeak-ng-data` ships with each voice.
3. **Model licences.** GPL-compatible model licences are allowed (permissive, CC BY, LGPL, GPL-2.0-or-later, GPL-3.0). Non-commercial, unknown and GPL-incompatible licences stay denied. The lint uses an explicit allow-list (`tools/bench/beepboop_bench/models.py`).
4. **D1.** The lessac-derived Piper voices (research-only provenance) are accepted for wake-word training data and as the default TTS voice, as the only non-SPDX licence accepted by owner ruling (`LicenseRef-Blizzard-2013-Lessac`). The strict profile without them stays documented as an option.

This supersedes the licence clause of ADR 0016 ("MIT", "GPL-linked artifacts are denied") and the "no GPL code in a beepboop process" consequence of ADR 0009. It also overrides the licence row of `docs/research/DECISIONS.md` (Packaging: "MIT license"; `LICENSE` (MIT)), which stays unedited as provenance ([CLAUDE.md §1](../../CLAUDE.md#1-project-summary)).

## Consequences

- The Piper TTS path works on sherpa-onnx 2.x; the bench measures the same phonemiser the product runs.
- Anyone redistributing beepboop or a modified image must provide source under GPL-3.0-or-later.
- `piper1-gpl` is now licence-acceptable as an alternative TTS engine. It is not adopted: sherpa-onnx already runs the voices, and a second runtime would break "one runtime to pin" (ADR 0009). A future swap needs only a benchmark, not a licence ADR.
- The default voice is not cleared for commercial use. A commercial build must swap the voice and use the strict wake-word profile.
- `NOTICE` collects the CC BY attributions.

## Alternatives considered

- **Stay MIT, run espeak-ng in a separate process**: an extra service and IPC for every sentence, to avoid a licence nobody here needs to avoid.
- **A non-espeak phonemiser (g2p models, dictionary lookup)**: Piper voices are trained on espeak phonemes; others degrade pronunciation.
- **Pin sherpa-onnx 1.x with the built-in frontend**: freezes the whole speech runtime on an old release.
- **Strict voices only (D1 rejected)**: 6 voices instead of ~700 for wake-word training and a weaker default voice.
