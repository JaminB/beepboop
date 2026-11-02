# Speech pipeline on Raspberry Pi 4: research report for beepboop

Today is 2026-11-01. Search coverage was thin. For several key rows I found no published Pi 4 number, and I mark those "unmeasured". I did not fill gaps from memory. Treat every figure as a hypothesis until beepboop's own benchmark harness (section 4) confirms it on a Pi 4.

## 1. Options compared

### VAD and wake word
| Option | Notes | Source |
|---|---|---|
| Silero VAD (ONNX) | Small model, usable as a gate. openWakeWord supports gating detections on a Silero VAD score. I did not verify Pi 4 per-frame cost, which is believed to be low. | openWakeWord README |
| webrtcvad | Classic, tiny, GMM-based. Less robust to noise than Silero. I did not verify its maintenance status. | not researched |
| openWakeWord | Its README says one Pi 3 core can run 15-20 models in real time, with a target of under 0.5 false accepts per hour with tuning. Home Assistant docs call it too large for low-power devices, so HA runs it server-side. A Pi 4 is much stronger than a Pi 3, so it is a fine fit for beepboop. | README; HA "about_wake_word" |
| microWakeWord | Lighter, Inception-based. Only the pre-trained models "okay nabu", "hey jarvis" and "alexa" are listed by HA. Aimed at ESP32-S3-class devices. | HA docs |

### Local STT
| Option | Pi 4 evidence | License / maturity |
|---|---|---|
| whisper.cpp tiny.en q8_0 | One old GitHub-issue log shows about 12 s of processing for the 11 s JFK clip with 3 threads, roughly real time. That is batch decoding, not streaming. No base.en Pi 4 number was found. | MIT, very active |
| faster-whisper (CTranslate2) | No Pi 4 numbers found. Moonshine's docs say it was the fastest Whisper runtime they tested. On a Pi 5, Whisper Tiny took 5,863 ms and Whisper Small 10,397 ms (vendor data). | MIT |
| sherpa-onnx offline Whisper tiny.en | Official Pi 4 RTF table: 0.685 (fp32, 1 thread) down to 0.386 (int8, 4 threads). | Apache-2.0, very active |
| sherpa-onnx streaming zipformer (en-20M class) | Docs say "all models ... run in real-time on a Raspberry Pi 4". No numeric RTF found. An unverified Chinese blog claims RTF of about 0.84. True streaming gives partial results as the user speaks. | Apache-2.0 |
| Moonshine (Useful Sensors, now moonshine-ai) | Vendor table on a Pi 5, which is faster than a Pi 4. Tiny Streaming (34M params): 12.00% WER, 237 ms. Small Streaming (123M): 7.84% WER, 527 ms. Medium Streaming (245M): 6.65% WER, 802 ms. The Pi 5 column predates a build-optimization fix, so it reads pessimistically. There is a v2 streaming paper (arXiv 2602.12241). Python package `moonshine-voice`. English-centric. | Check the model license before adopting. Not confirmed. |
| Parakeet (NVIDIA) | Beats Moonshine on accuracy leaderboards per an HN thread, but is a heavy NeMo model. I found no Pi 4 data and do not expect it to be viable there. | CC-BY-4.0 (from memory, unverified) |
| Vosk | Not researched. Believed to be older and lower accuracy than the options above. | not verified |

Moonshine's latency goal is under 200 ms after the user stops talking. Its streaming design caches work while the user speaks. Whisper must wait for the end of the utterance and then run the whole decode.

### Local TTS
| Option | Evidence |
|---|---|
| Piper | A guide gives RTF around 0.19 on a Pi 5 and says medium and high voices lag on a Pi 4. Low-quality voices are the safe choice there. The original rhasspy/piper repo is archived (Oct 2025, was MIT). Development moved to OHF-Voice/piper1-gpl, which is GPL-3.0. Voices are licensed separately on Hugging Face. |
| Kokoro (82M, ONNX) | Better quality than Piper. The only latency figures found are for an M2 Max (p50 first audio 855 ms). No Pi 4 data, and I suspect it is too slow for sub-second first audio on a Pi 4. |
| sherpa-onnx TTS | Runs both Piper (VITS) and Kokoro models in a single Apache-2.0 runtime. It avoids the GPL issue if you use it instead of the piper1-gpl package, but the voice model licenses still apply. I did not verify Pi 4 numbers. |

### Cloud
| Option | Evidence |
|---|---|
| Deepgram Nova-3 streaming | Vendor claim: under 300 ms transcription latency. No independent benchmark found. WebSocket. |
| Amazon Transcribe streaming | A third-party comparison says 500 ms to 1.5 s practical latency (unverified). HTTP/2 or WebSocket. |
| Amazon Polly, ElevenLabs | No numbers found. Both support streaming audio, and ElevenLabs offers a websocket text-in/audio-out mode. Needs a follow-up check. |

### Wyoming protocol
- It is an Open Home Foundation peer-to-peer TCP protocol: JSON-lines headers plus optional binary PCM payloads (OHF-Voice/wyoming, PyPI `wyoming` 1.10.x).
- It defines services for ASR, TTS, wake word, intent and satellite control. A `describe`/`info` exchange handles discovery.
- It has no authentication or encryption by design, so it is LAN-only.
- Its info response can carry satellite metadata (area, local VAD, wake words).

## 2. Recommendation

1. **Define `SttProvider`, `TtsProvider`, `WakeProvider` and `SpeakerIdProvider` as small async-streaming Python interfaces.** Ship Wyoming adapters in both directions: a client that consumes any Wyoming STT/TTS server, and optionally a server that exposes beepboop's providers. Do not make Wyoming the internal bus. It has no auth and adds a process hop on every audio chunk. As a boundary it is excellent, because it gives beepboop the whole Rhasspy/HA voice-model ecosystem for free.
2. **Default pipeline.**
   - Wake word: openWakeWord, gated by Silero VAD.
   - Speaker ID: a CAM++ or WeSpeaker ONNX model (192-dim embeddings) via sherpa-onnx, run on the audio from the wake word plus the start of the command. Compare by cosine similarity against enrolled profiles. I found no Pi 4 timing, so the under-200 ms target is unverified and is a gate for the benchmark harness.
   - STT: a streaming model, either sherpa-onnx streaming zipformer or Moonshine Tiny/Small Streaming. A streaming model is the only realistic way to approach the 1.5 s budget on a Pi 4. whisper.cpp or faster-whisper tiny is the offline fallback and is the weaker choice because decode starts only after the user stops speaking.
   - TTS: Piper low or medium voice through sherpa-onnx, with sentence chunking. Kokoro is opt-in for users with more powerful hardware.
   - Cloud providers (Deepgram, Transcribe, Polly, ElevenLabs) are optional plugins.
3. **Latency budget (hypothesis, validate on device).**

| Stage | Budget |
|---|---|
| Endpointing | about 250 ms |
| Final STT tail | about 100-250 ms |
| LLM time to first sentence (Bedrock streaming) | about 500-700 ms |
| TTS first chunk | about 200-300 ms |
| **Total** | **about 1.2-1.5 s** |

   The target is tight. The LLM and the TTS first chunk are the largest items.
4. **Stream LLM tokens into a sentence splitter.** Start TTS on the first complete sentence or clause while the rest generates.
5. **Do not make benchmark numbers a spec claim until measured.** Ship a `beepboop bench` harness first.

## 3. Gotchas and risks specific to beepboop

- **Piper licensing.** The maintained Piper engine is GPL-3.0, while beepboop is MIT. Calling it through a subprocess or Wyoming is likely safe, and linking it into the MIT process is not. Using sherpa-onnx with Piper-format voices avoids the question. Check the license of every voice model separately.
- **Moonshine and Parakeet licenses are unverified.** Check them before bundling any model into an MIT repo. Prefer download-on-install over redistribution.
- **Pi 4 CPU contention.** Home Assistant Core, the web UI, wake word, VAD, speaker ID, STT and TTS all share 4 Cortex-A72 cores. TTS and STT will run at the same time during barge-in. Pin threads, set ONNX intra-op threads to 1-2 per model, and measure with HA running. Thermal throttling in a case without a fan will inflate latency.
- **RAM.** Budget it explicitly. HA is already heavy, so a 2 GB Pi 4 is probably too small and the spec should say 4 GB minimum (inference from general knowledge).
- **Speaker ID on wake-word-length audio.** A 1-2 s hotword clip is a short, weak sample for an embedding. Combine it with the first words of the command. Make the confidence threshold and the admin-configured Guest fallback explicit. Short-utterance accuracy and spoofing (a recording of someone's voice) are real risks, which is why the tiered trust and PIN design matters.
- **AEC wiring (XVF3800).** The chip cancels echo from audio sent to its USB playback device, and the Seeed wiki does not document the exact reference routing. I found nothing on barge-in. The implication is that beepboop must play all TTS and chime audio through the array's own USB audio output, not the Pi's headphone jack or HDMI. Confirm this against the XMOS datasheet.
- **XVF3800 firmware variants.** There are standard, 16 kHz 6-channel and 48 kHz 2-channel USB images, plus I2S firmwares. The 6-channel image may keep raw mic channels silent until enabled with `amixer`. Pick one image and document it.
- **ALSA vs PipeWire.** Pi OS 64-bit (Bookworm onwards) uses PipeWire. A container reading ALSA directly through `/dev/snd` is simplest. Mixing PipeWire in the host with ALSA in a container can cause device-busy errors.
- **Ducking.** Lowering music volume on wake word (HA media players, Spotify Connect and so on) needs a defined hook.
- **Wyoming has no auth.** Never expose it beyond localhost or a trusted LAN. It is another reason for LAN-only.
- **Cloud STT leaks audio.** Make cloud STT/TTS strictly opt-in per household. Show in the UI which providers see the audio.

## 4. Ideas the user did not ask for

- **`beepboop bench` and a latency trace per turn.** Record per-stage timings (endpoint, STT final, LLM first token, first sentence, TTS first audio, first sound). Show them in the web UI, and use them for regression testing in CI on a real Pi.
- **Audio-in-the-loop test fixtures.** A corpus of WAV files with expected transcript, speaker and intent, so a provider swap can be regression-tested.
- **Per-user STT vocabulary and phrase bias.** Contact names, device nicknames and room names from the user's profile and HA entity names, passed as hotwords to providers that support them.
- **Per-user TTS voice and speaking rate.** Kids get a different voice, and the profile owns the setting.
- **Pre-recorded acks.** Cache short confirmations like "OK" and "Done" as audio, so the first response can play before the LLM returns. A strong trick for the 1.5 s target.
- **Local fast-path intents.** Simple commands (lights, timers) matched without an LLM and run through HA directly. It cuts hundreds of ms and works offline.
- **Fallback chains.** If the cloud STT or the network is down, fall back to local automatically and say so.
- **Privacy affordances.** Hardware mute respected by software, an LED and chime when listening, an audio retention default of "none", and a "what did you hear" log with a short TTL.
- **Satellite-readiness.** Define the Wyoming-compatible audio/event boundary now so a later satellite (an ESP32 or a second Pi) can stream to the core.
- **Speaker ID enrollment quality checks.** Reject noisy enrollment, report the margin between users, and let the system learn from confirmed corrections.
- **Diagnostics page.** Mic level meter, DoA/VAD from the XVF3800 (the Seeed wiki shows `xvf_host AEC_AZIMUTH_VALUES` for DoA), and a speaker-test tone.

## 5. Sources

- Moonshine repo: https://github.com/moonshine-ai/moonshine
- Moonshine benchmarks: https://moonshine-voice.readthedocs.io/en/latest/using/benchmarks/
- Moonshine vs Whisper: https://moonshine-voice.readthedocs.io/en/latest/moonshine-vs-whisper/
- Moonshine v2 paper: https://arxiv.org/pdf/2602.12241
- Decoder article on Moonshine: https://the-decoder.com/open-source-moonshine-speech-recognition-model-is-up-to-five-times-faster-than-openais-whisper/
- sherpa-onnx Whisper tiny.en page with Pi 4 RTF table: https://k2-fsa.github.io/sherpa/onnx/pretrained_models/whisper/tiny.en.html
- sherpa-onnx small online models: https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/pretrained_models/small-online-models.rst
- whisper.cpp Pi issue: https://github.com/ggerganov/whisper.cpp/issues/7
- Piper (archived, moved): https://github.com/rhasspy/piper and https://github.com/OHF-Voice/piper1-gpl
- Piper on Pi guide: https://aivideosensei.com/guides/piper-tts-offline-voice-guide
- Piper vs Kokoro: https://www.codesota.com/text-to-speech/piper-vs-kokoro
- Wyoming: https://github.com/OHF-Voice/wyoming and https://pypi.org/project/wyoming/
- HA wake word approach: https://home-assistant.io/voice_control/about_wake_word
- openWakeWord mirror (README): https://github.com/jbflow/openWakeWord
- Seeed XVF3800 wiki: https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/
- XMOS XVF3800: https://www.xmos.com/xvf3800
- Deepgram latency docs: https://developers.deepgram.com/docs/measuring-streaming-latency.mdx
- Deepgram vs AWS Transcribe comparison (third party): https://convertaudiototext.com/es/blog/deepgram-vs-aws-transcribe
- Speaker embedding ONNX exports: https://huggingface.co/kenrunio/transcriber-speaker-onnx
- Online diarization latency study: https://arxiv.org/pdf/2407.04293

Not covered and needing follow-up:
- Polly, ElevenLabs and Transcribe first-byte latencies from primary AWS/vendor docs.
- Silero and webrtcvad Pi 4 cost.
- Vosk status.
- The XVF3800 AEC reference routing and barge-in behavior.
- PipeWire ducking specifics.
- Real end-to-end latency figures from comparable projects (HA Assist, Rhasspy, Willow, OVOS).