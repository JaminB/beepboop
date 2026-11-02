# Wake word and speaker ID on a Raspberry Pi 4: research report for beepboop (as of 2026-11-01)

I found no published Pi 4 latency numbers for any speaker-embedding model. The Pi 4 figures below marked **est.** are my own scaling from the CAM++ paper's single-thread CPU numbers. Milestone 0 should measure them on real hardware.

## 1. Options compared

### 1a. Wake word engines

| Engine | Runtime on Pi 4 | License | Activity (checked on PyPI / GitHub API) | Custom "beepboop" | Notes |
|---|---|---|---|---|---|
| **openWakeWord** | Small. The README says one Pi 3 core runs 15–20 models in real time | Code Apache-2.0. **Pretrained models are CC BY-NC-SA 4.0** | Last release v0.6.0 on 2024-02-11. Last push 2025-12. 116 open issues. Effectively in maintenance mode | Yes. Trained on fully synthetic TTS clips over a frozen shared feature extractor, with a Colab notebook | Targets <5% false rejects and <0.5 false accepts/hour (measured on about 5.5 h of the Dinner Party corpus). Has a built-in Silero VAD gate, optional Speex noise suppression, and per-speaker "custom verifier" models |
| **microWakeWord** (OHF-Voice) | Tiny streaming MixConv TFLite model, 30 ms inference stride. `pymicro-wakeword` 2.5.0 (2026-09-17) ships **manylinux aarch64 wheels** | Apache-2.0 | Active. Repo pushed 2026-07. OHF's `linux-voice-assistant` (pushed 2026-10-29) supports both engines | Yes, but the README warns that "training a usable model requires a lot of experimentation." It uses the Piper sample generator plus pre-generated negative sets on Hugging Face. Community wrappers exist: a one-shot Colab, and SEED (about 180 real samples augmented to 50k) | v2 models perform about 2x better than v1 (HA Voice chapter 7). It is the engine the Home Assistant ecosystem is converging on |
| **Porcupine** (Picovoice) | Very small and accurate | Proprietary | **Free tier discontinued 2026-06-30.** The AccessKey is checked when the engine initializes, so the SDK won't run without a valid key. "No non-commercial tier planned" | Console training, previously | **Disqualified** for an MIT, local-first project |
| Others (e.g. voxrt-wake-word) | n/a | Proprietary binaries | — | — | Not suitable |

### 1b. Speaker embedding models

Accuracy and speed figures in this table come from the CAM++ paper (Interspeech 2023), Tables 1 and 3. Its RTF was measured on a single-thread x86 CPU. ONNX file sizes come from the sherpa-onnx `speaker-recongition-models` release assets.

| Model | Params | GFLOPs | RTF (x86, 1 thread) | EER VoxCeleb1-O | ONNX size | Pi 4 latency for 2 s audio (**est.**, 1–2 threads, fp32) | License |
|---|---|---|---|---|---|---|---|
| **CAM++** (3D-Speaker / WeSpeaker) | 7.18 M | **1.72** | **0.013** | **0.73%** | 29 MB | **~60–130 ms** | Code Apache-2.0. VoxCeleb weights CC BY 4.0 |
| ECAPA-TDNN (SpeechBrain `spkrec-ecapa-voxceleb`) | 14.66 M | 3.96 | 0.033 | 0.89% (SpeechBrain card: 0.80%) | ~80 MB as PyTorch weights | ~200–330 ms | Apache-2.0. SpeechBrain 1.1.2 released 2026-10-14 |
| ResNet34 (WeSpeaker; also the pyannote 4.x embedding) | 6.70 M | 6.84 | 0.032 | 0.97% | 26.5 MB | ~200–330 ms | CC BY 4.0 weights |
| ERes2Net (3D-Speaker, English VoxCeleb) | — | — | — | — | 26.5 MB | not measured | Apache-2.0 |
| NeMo TitaNet-small / large | about 6 M / 23 M | — | — | — | 40 MB / 101 MB | small: probably ~150–250 ms. large: too slow | CC BY 4.0 |
| Resemblyzer (GE2E d-vector) | small LSTM | — | — | no published EER | — | fast | Apache-2.0, but **last push 2023-10**: abandoned |

The runtime pick is **sherpa-onnx** 2.0.1 (2026-10-27, Apache-2.0). It ships every one of these as ONNX files with a C++ core and Python bindings, plus a built-in speaker-embedding manager (register, search, verify). For a sense of scale on the same hardware: Whisper tiny.en runs at RTF 0.386 on a Pi 4 (int8, 4 threads). STT will be what loads the CPU, not speaker ID.

### 1c. Anti-spoofing

The candidate is **AASIST-L**: 85K parameters, about 332 KB, 0.99% EER on ASVspoof2019 LA. It is trained on TTS and voice-conversion attacks, though, not on physical replay through a loudspeaker. I found no result showing AASIST-L generalizes to real-world replay. Cross-corpus generalization of these countermeasures is known to be poor.

## 2. Recommendation

**Wake word: microWakeWord through `pymicro-wakeword` as the default, behind a `WakeWordEngine` interface, with openWakeWord as an alternate plugin.**
- It is Apache-2.0 end to end, ships aarch64 wheels, is actively maintained by OHF, and matches what HA satellites run. That keeps the future satellite path (ESP32 boxes) on the same model format.
- openWakeWord's pretrained models are NC-licensed, so they are incompatible with an MIT default. The project is also stale. Keep it as a plugin because its training and verifier tooling is easier for hobbyists.
- **Feasibility of a custom "beepboop" model:** good. Two plosives and a repeated syllable give a distinctive phrase. Train on synthetic Piper data plus negatives, then fine-tune on about 50–200 real household recordings collected during enrollment (the SEED approach). Ship a stock model in the repo, built through a reproducible training pipeline (`tools/train_wakeword/`). Check the licenses of the Piper voices and the negative datasets used to train the shipped model.
- **Acceptance target:** ≤0.5 false accepts per 24 h of household TV and conversation (stricter than openWakeWord's 0.5/h target), with ≤5% false rejects at 3 m. Measure it with a recorded "living-room soak" corpus in CI.

**Speaker ID: CAM++ (WeSpeaker English VoxCeleb ONNX) through sherpa-onnx.**
- It has the best accuracy-per-FLOP in the comparison: about 2.5x faster than ECAPA and ResNet34 at better EER. That is the only option that plausibly meets the <200 ms budget with headroom on a Pi 4 already running STT.
- Keep the `SpeakerEmbedder` interface model-agnostic so ERes2Net or TitaNet-small can be swapped in. Store the model ID with every saved embedding, because embeddings from different models are incompatible and switching models forces re-enrollment.
- **Try int8 quantization, but validate it.** Dynamic int8 often costs speaker models some EER, so run an A/B on household data before making it the default.

**Run ID in parallel with STT. Yes, this works, and it should be the design.**
- The wake word ("beepboop", about 0.6–0.8 s) is a fixed phrase. Enrolling on that same phrase makes the wake-word segment a **text-dependent** sample, which is much more discriminative than free speech of the same length.
- Pipeline:
  1. On wake, the ring buffer gives about 1 s of pre-roll containing the wake word. Embed it right away (about 50 ms est.) and get a provisional ID.
  2. Keep accumulating the command audio. At about 1.5–2 s into the command (or at end-of-speech, whichever comes first), embed wake word plus command and fuse the scores.
  3. The final ID is ready before or at end-of-speech. It adds no latency because STT finalization and the LLM call come after.
- Short-utterance accuracy degrades sharply on open-set, in-the-wild data. One baseline went from about 20% EER at 1 s to 8.9% at 2 s and 6.6% at 3 s. Closed-set ID among about 2–6 enrolled household members, with a text-dependent wake word, is a far easier problem. Still, **never authorize high-risk actions on voice alone.** This matches the tiered-trust design already decided.

**Decision logic: closed-set ID with an open-set reject.**
- Score: cosine similarity against **several centroids per user**, one per condition cluster (near/far, morning voice, etc.), not a single mean.
- Accept when `top1 ≥ τ_accept` **and** `top1 − top2 ≥ margin`. Otherwise use the Guest profile, or optionally ask "Is this Alex?" for medium-risk actions.
- Calibrate per household. Enrollment produces genuine scores for each user and impostor scores against the other members plus a bundled cohort of about 200 VoxCeleb embeddings. Set τ for a target false-accept rate (e.g. 1%), and use AS-Norm or a z-norm cohort to stabilize scores across mic distance and noise.
- Expose per-tier thresholds:
  - low risk: lenient
  - medium risk (read my email or calendar): strict
  - high risk: always a second factor

**Enrollment UX.**
- Collect about 5 wake-word utterances, plus about 5 free sentences (about 30 s of speech total), recorded **through the Pi's own mic** at two or three positions in the room. Prompt "say it from the couch" and "say it from the kitchen". A phone browser mic is a different channel and will hurt accuracy.
- Quality gates per clip: SNR, clipping, VAD speech ratio, duration ≥1 s, and consistency with the user's other clips (reject outliers).
- Show a live "match confidence" meter, and test immediately against the other enrolled users so the UI can warn "Alex and Sam sound similar; we'll ask to confirm for sensitive actions."
- **Continuous adaptation:** after a high-confidence ID (top1 well above τ and a large margin) with no user correction, add the embedding to a bounded rolling buffer of about 50 per user. Recompute centroids nightly. Never adapt from Guest or borderline decisions, which avoids identity drift and poisoning. Keep the original enrollment clips as the anchor set.

**Anti-spoofing.** Do not promise it. Ship an optional `LivenessCheck` hook that is off by default (an AASIST-L ONNX plugin later). Rely on a structural defense instead: high-risk tools always need a PIN or phone confirmation. Document plainly that voice ID is a convenience and personalization signal, not authentication.

## 3. Gotchas and risks specific to beepboop

- **Children.** An adult-trained model reached **30.65% EER** on the youngest children in the OGI set, against 0.8% on adults (arXiv 2508.01637). Children's voices also change month to month.
  - Mitigations: more enrollment clips, faster adaptation, an automatic "re-enroll suggested" prompt when a child's mean score drifts, and kids' profiles defaulting to restricted-trust tools. A child misidentified as a parent must still be blocked by the second factor.
- **Similar family voices** (siblings, parent and same-sex adult child): the margin rule plus a confirmation prompt handles this. Surface pairwise similarity in the admin UI.
- **CPU contention.** Wake word, VAD, speaker ID, local STT, and TTS all run on 4 A72 cores. Pin the wake word to a core, run speaker ID at nice priority with 1–2 threads, and benchmark the *concurrent* pipeline, not models in isolation. Watch thermal throttling: require a heatsink or fan in the hardware docs.
- **AEC and the mic DSP.** The XVF3800 beamformer and AGC alter the voice signal. Enroll and identify on the *same* processed channel. If the array exposes multiple outputs (ASR beam vs. comms), choose one and document it.
- **TV and radio voices** can trigger the wake word, then get ID'd as Guest. Guest therefore needs a safe default, and false wakes should be logged so the admin can add them as negatives.
- **Embeddings are biometric data** (GDPR Article 9, Illinois BIPA, etc.). Store them encrypted at rest on the Pi, never send them to cloud model providers, and give each user a "delete my voiceprint" button. The MCP server must never expose raw embeddings or audio as tools.
- **License hygiene.** NC-licensed openWakeWord models and some research checkpoints cannot be bundled in an MIT distribution. Keep `models/LICENSES.md` with an entry for every shipped artifact.
- **Model swap invalidates enrollments.** Store `{model_id, model_sha}` alongside embeddings, keep raw enrollment audio (with consent), and allow one-click re-embedding.

## 4. Ideas the user hasn't asked for

1. **Per-user wake words or aliases.** microWakeWord runs several models cheaply, so "hey beepboop" plus a personal word could double as a strong identity prior.
2. **Identity events as a first-class bus topic** (`speaker.identified {user, confidence, margin}`). Routines, presence ("Sam is home"), proactive announcements, and memory can all consume it.
3. **"Who am I?" / "That wasn't me"** voice commands. The correction feeds back as a negative example and an audit log entry.
4. **Confidence-aware tool gating.** MCP tools declare `min_id_confidence` in `manifest.yaml` next to the risk level, so module authors don't reimplement the policy.
5. **Diarization for follow-up and barge-in.** Reuse the same embedder to check that a follow-up turn comes from the same speaker. This guards against a child hijacking a parent's open session.
6. **A household voice ID test page.** Have each user say a phrase and display the score matrix. This builds trust and debugs placement.
7. **False-wake review queue.** Store the last N false-wake clips locally (opt-in) and retrain or adjust thresholds from them, the same idea as openWakeWord's verifier negatives.
8. **Optional text-dependent enrollment boost.** openWakeWord-style per-speaker verifier heads could serve as a cheap second-stage household filter that also cuts false wakes from TV.
9. **Benchmark harness in the repo** (`bench/` with Pi 4 latency and EER on a household fixture set) so contributors can prove a swapped model is better.

## 5. Sources

- openWakeWord README and verifier docs: https://github.com/dscripka/openWakeWord , https://github.com/dscripka/openWakeWord/blob/main/docs/custom_verifier_models.md
- openWakeWord on PyPI (v0.6.0, 2024-02-11): https://pypi.org/project/openwakeword/0.5.1/
- microWakeWord: https://github.com/kahrendt/microWakeWord , https://gittrend.io/repo/OHF-Voice/micro-wake-word , https://esphome.io/components/micro_wake_word/
- pymicro-wakeword (aarch64 wheels, 2.5.0): https://pypi.org/project/pymicro-wakeword/ , https://github.com/OHF-Voice/pymicro-wakeword
- HA Voice chapter 7 (microWakeWord v2, OHF): https://www.home-assistant.io/blog/2024/06/26/voice-chapter-7/
- SEED custom wake word: https://community.home-assistant.io/t/seed-train-a-custom-wake-word-from-just-a-handful-of-recordings-not-hundreds/1018736
- Picovoice free tier shutdown: https://community.home-assistant.io/t/fyi-picovoice-confirmed-free-tier-accesskeys-will-stop-working-after-june-30-2026/1012744 , https://community.home-assistant.io/t/porcupine-free-tier-shutdown-alternatives-for-home-assistant-voice-users/1012382
- CAM++ paper (params, FLOPs, RTF, EER): https://arxiv.org/pdf/2303.00332
- sherpa-onnx speaker model assets: https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models ; Pi 4 Whisper RTF: https://k2-fsa.github.io/sherpa/onnx/pretrained_models/whisper/tiny.en.html
- WeSpeaker: https://arxiv.org/pdf/2210.17016 ; license card: https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM
- SpeechBrain ECAPA: https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb
- Resemblyzer: https://github.com/resemble-ai/Resemblyzer
- Short-utterance degradation: https://arxiv.org/pdf/2005.03329 , https://papers.cool/venue/kang26@interspeech_2026@ISCA
- Children's speaker verification: https://arxiv.org/pdf/2508.01637 , https://aclanthology.org/2021.nodalida-main.9
- Score normalization / AS-Norm: https://arxiv.org/pdf/2102.01760 , https://arxiv.org/html/2504.04512v1 , https://arxiv.org/pdf/2609.01221
- AASIST: https://arxiv.org/pdf/2110.01200 ; replay generalization: https://ar5iv.arxiv.org/html/1901.08025
- Alexa Voice ID enrollment: https://venturebeat.com/ai/amazons-alexa-can-now-learn-your-voice-automatically