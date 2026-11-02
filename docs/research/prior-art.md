# Prior art for beepboop: open-source and commercial voice assistants

**Evidence quality.** Web search returned thin or old results for most topics. Items marked [V] are backed by a source below. Items marked [M] are from my background knowledge. Re-verify every [M] item before it goes into the spec. Pi 4 latency and RAM numbers are not confirmed by any source I found; treat them as estimates to benchmark on real hardware. I also did not confirm current activity levels (commit dates, stars) for Willow, Leon or Picovoice. I ran no WebFetch calls, so source claims rest on search snippets only.

## 1) Options compared

| Project | What it is | Multi-user voice ID | Status / license | Pi 4 relevance |
|---|---|---|---|---|
| **HA Assist + Wyoming + Voice PE** | Pipeline of wake word, STT, intent, TTS, linked by Wyoming (an Open Home Foundation protocol) [V]. Voice PE has dual mics and an XMOS chip, with on-device microWakeWord for "Okay Nabu", "Hey Jarvis" and "Hey Mycroft" [V]. Server-side openWakeWord is the heavier alternative [V]. | None found. I found no speaker recognition in HA or ESPHome docs [V, absence]. | Very active. Apache-2.0 for the core. Release cadence is monthly; the 2026.4 notes say Assist shows what it is thinking during requests [V]. | Wyoming is the de facto interchange for voice components and worth reusing. Custom openWakeWord models on Voice PE are unresolved [V]. |
| **Rhasspy 3** | Wyoming-based pipeline of wake, ASR, intent and TTS programs [V]. | None [M]. | Rhasspy 3 was a developer preview and effectively superseded by HA's voice work (Wyoming now lives in HA) [M/V]. MIT. | Useful design reference only. |
| **Willow** | ESP-IDF firmware for ESP32-S3-BOX, aiming at Echo-class quality with HA and openHAB [V]. Relies on a separate inference server (WIS) for speed [M]. | None [M]. | Development appeared to slow after 2024 [M]. Apache-2.0. | Wrong architecture for beepboop (thin client plus GPU server). Its "fast local speech" goals are the same as ours. |
| **OpenVoiceOS / Neon** | Fork of Mycroft's codebase, with skills still compatible [V]. OVOS now has a foundation [V]. | Neon has some speaker-aware features [M]. | Active but small. Apache-2.0 / BSD. | Heavy skills and messagebus framework. A cautionary tale on modularity. |
| **Leon** | Node/Python personal assistant with skills. | None [M]. | Small community, slow progress [M]. MIT. | Low relevance. |
| **Picovoice (Porcupine / Eagle / Cheetah)** | Commercial on-device SDKs. Eagle is speaker recognition, Porcupine is wake word. | Yes (Eagle). | Proprietary, requires an access key and has free-tier limits [M]. | Excellent on a Pi, but a closed-source dependency conflicts with MIT/open-source. Make it an optional plugin at most. |
| **Alexa+ / Alexa Voice ID** | "Alexa, learn my voice" enrolls a profile. Used for personalized messages, news and skills. Kids profiles exist [V]. | Yes. | Proprietary. | Good UX reference. A 2016 report said Voice ID could restrict purchases to a parent's voice without a PIN; whether this shipped is unconfirmed [V]. |
| **Gemini for Home / Voice Match** | Voice Match is set up in the Home app and can be applied to all current and future devices. Turning it off disables personal results. Google warns that a sound-alike voice could reach personal results (email, payments, calendar) [V]. | Yes. | Proprietary. Owners reported that Gemini for Home and Alexa+ made voice control worse [V, opinion]. | Confirms that voice ID alone is not authentication. This supports the tiered-trust design. |
| **Siri** | Personal requests are gated by voice recognition plus device state [M]. | Yes. | Proprietary. | No usable documentation found. Skip as a reference. |

**Speaker-ID model options (Pi 4, estimates only).**
- ECAPA-TDNN has 192-dim embeddings, an ~84 MB ONNX export, and cosine scoring [V]. It was the slowest embedding model in a diarization latency study (22.1M parameters) [V].
- A small-footprint verification paper reached 45 ms for streaming on one Pi 3B core. That is a different model and an older board [V].
- Faster candidates [M]: WeSpeaker ResNet34 (smaller than ECAPA), ERes2Net and 3D-Speaker models (via sherpa-onnx), and Titanet-small.
- sherpa-onnx offers speaker embedding extraction and ARM builds, and its project description says smaller models suit boards like the Pi [V].

The <200 ms target is plausible with a small int8 ONNX model on 1 to 2 s of post-hotword audio. It is not plausible with stock ECAPA-large on a busy Pi 4. Benchmark first.

## 2) Recommendation

1. **Reuse Wyoming** for the wake, STT and TTS provider seam. It is the proven open standard, gives beepboop access to existing Piper, Whisper and openWakeWord servers, and keeps providers swappable. Add one new component type, `speaker-id`, in the same style.
2. **Do not fork or adopt a skills framework** (OVOS, Rhasspy, Leon). Mycroft-style skill and intent systems are what MCP tools plus an LLM agent loop replace. The beepboop modules (manifest plus AGENT.md) are the right abstraction.
3. **Speaker ID is the differentiator and is unserved in open source.** Build it as its own small component: a small ONNX embedding model with per-user centroid embeddings, cosine scoring with thresholds, and a confidence margin between the top two users. Run the embedding in parallel with STT, taking the hotword audio plus the first ~1 s of speech. This hides the cost in the STT window. If confidence is low, fall back to Guest.
4. **Treat voice ID as identification, not authentication.** Google's own warning about sound-alikes [V] and replay attacks are why risk-tiered second factors are correct. Never let the ID score alone unlock anything above low risk.
5. **Wake word:** use openWakeWord or microWakeWord (both Apache-2.0) by default. Offer Porcupine as an optional plugin only.
6. **STT/TTS defaults:** faster-whisper small/base (int8) or a sherpa-onnx streaming model, plus Piper. HA's Speech-to-Phrase is a fast option for constrained command vocabularies [V]. Benchmark on the Pi 4 and budget carefully. Whisper small on a Pi 4 is often 2 s or more for short clips [M], which breaks the 1.5 s target. Streaming ASR (sherpa-onnx zipformer or similar) or Speech-to-Phrase for common commands, with cloud or LLM fallback, is the realistic path.

## 3) Gotchas and risks for beepboop

- **Latency budget.** The Pi 4 has 4 CPU cores and no GPU. If the LLM runs in the cloud (Bedrock), the largest risk is time to first token. Speak the first sentence while the rest streams (sentence-chunked streaming TTS). Add a fast local path for simple intents (timers, lights) before the LLM. Cache the prompt prefix. The real budget (<1.5 s end of speech to first audio) cannot afford VAD tail plus STT plus an LLM round trip plus TTS without all of these.
- **RAM and thermal limits.** A 4 GB Pi cannot run HA Core, SvelteKit, wake, STT, TTS, speaker ID and the agent comfortably. Plan for 8 GB and a heatsink or fan. Budget memory per container [M estimates to benchmark].
- **False wakes and the mic.** The top community complaints about every assistant are false wakes, missed wakes and accuracy. Keep a "why did you wake" log and one-click false-wake reporting in the UI. Gather negative audio locally (opt-in).
- **Why Mycroft failed.** Sources agree on a patent lawsuit plus cash trouble, with hardware procurement and pandemic effects; they disagree on the end date (2023 vs early 2024) [V]. Mark II shipped to only a minority of backers [V]. Lesson: hardware plus cloud services plus a small team is fatal; beepboop avoids this by being software-only on commodity hardware. A second lesson, from OVOS's fork history [V], is that a framework that ties everyone to one skills API fragments when the vendor disappears. MCP is a neutral standard.
- **Voice ID failures.** Colds, whispering, distance, TV audio, kids' voices changing (a kid's profile drifts as they grow), twins and siblings, and enrollment in a quiet room versus use in a noisy kitchen. Require enrollment across several rooms and distances, and add continuous adaptive enrollment with a human confirmation step.
- **Privacy and legal.** Voice embeddings are biometric data (BIPA in Illinois, GDPR Art. 9). Keep embeddings local and encrypted, give per-user delete and export, and obtain explicit consent at enrollment. Guests are non-consenting; store nothing for them by default.
- **Cloud LLM data flow.** The agent sends transcripts and tool results to Bedrock. Show users what leaves the house. Redact high-risk details (PINs must never reach the model).
- **HA sidecar coupling.** HA's entity IDs and integration models change monthly. Pin versions, write a thin adapter with contract tests, and hide HA behind the abstraction layer.
- **Prompt injection.** Tool outputs (email subjects, calendar events, web results) can carry instructions. Combined with unlock or purchase tools, this matters. Enforce risk tiers outside the model, in code, and never let model output change the active profile.
- **Open-ended voice commands from TV or radio.** Add an echo-cancel check and a "who is speaking" gate; media playing should not trigger hotword plus Guest actions.

## 4) Features the user hasn't asked for

**MVP (architecture must support these from day one)**
1. **Timers, alarms, reminders.** Every competitor's most-used voice feature. They must be per-user ("my timer"), survive restarts (persist to SQLite), and ring on the right device.
2. **Shopping and to-do lists.** Per-user and shared lists. Back them with HA's list entities or a native module.
3. **Confirmation and "who did that" audit log.** Every tool call is logged with the profile, confidence score, risk level and second-factor status. Display it in the UI with a one-tap "that wasn't me" correction that feeds speaker-ID retraining.
4. **Guest profile and per-tool risk gating.** Already decided. Add a configurable per-profile "allowed in rooms/times" rule.
5. **Visible and audible state.** LED or chime feedback for wake, listening, thinking, and error states. A mic mute hardware switch or software kill switch with a visible indicator. Users distrust devices that cannot show whether they are listening.
6. **Offline degradation.** If the internet or Bedrock is down, the local intent path still handles timers and lights, and says so.
7. **Module dev kit.** `beepboop module new`, a manifest linter, an AGENT.md quality check, and a simulator that runs a text transcript as a given profile without a mic. This serves the "joy to work with" value.
8. **Text/CLI mode and web chat** for testing without a mic, using the same MCP path.
9. **Eval harness.** Record-and-replay test sets for wake word, speaker ID (FAR/FRR per user), and end-to-end latency, with a latency breakdown per stage displayed in the UI.

**Later**
10. **Follow-up conversation and barge-in.** Keep the session open for N seconds after a reply, with echo cancellation allowing interruption. Plan the dialogue state machine now; implement later.
11. **Per-user memory.** Facts and preferences, viewable and editable in the UI ("what do you know about me"), with forget-on-command. Memory is scoped per profile and never visible to other profiles or Guest.
12. **Routines and proactive announcements.** Time, event or presence-triggered, addressed to a person ("tell Sam when the dryer is done") using presence or last-heard-in-room. Add do-not-disturb and quiet hours.
13. **Multi-room and satellites.** The Wyoming satellite model covers this. Key issues are nearest-device arbitration (who answers when two hear the hotword), per-room timers, and intercom or broadcast.
14. **Kids/parental controls.** Per-profile allowed tools, content filters on the LLM, bedtime schedules, and a purchase block. Kids' voice drift needs periodic re-enrollment.
15. **Music.** Everyone expects it. Route through HA media players (Music Assistant is the HA ecosystem choice [M]). Per-user accounts (Spotify) via native adapters.
16. **Anti-spoofing / liveness.** Replay detection, and a spoken one-time challenge phrase for high-risk actions.
17. **Away mode and presence-aware answers.** Use presence to prefer the right profile when voice ID is ambiguous (e.g., one adult home).
18. **Backup and restore, OTA update with rollback.** A Pi appliance needs one-click backups of profiles, embeddings and config, and safe updates.
19. **Observability.** Prometheus-style metrics, a local trace viewer for each utterance (audio snippet, transcript, tools called), with retention controls.
20. **Accessibility.** Text-only responses on a display or phone for deaf users, and a "speak slower" preference per profile.
21. **Multilingual per profile.** Language preference per user, which affects STT/TTS model selection. Keep the model-loading cost in mind on a Pi.

## 5) Sources

- https://home-assistant.io/voice_control/about_wake_word [V]
- https://esphome.io/components/voice_assistant [V]
- https://community.home-assistant.io/t/how-can-i-use-a-custom-openwakeword-model-with-home-assistant-voice-preview-edition/1019895 [V]
- https://www.home-assistant.io/atom.xml (2026.4 release notes) [V]
- https://github.com/rhasspy/rhasspy3 and https://pypi.org/project/wyoming/ [V]
- https://en.wikipedia.org/wiki/Mycroft_(software) [V]
- https://blog.openvoiceos.org/posts/2025-05-20-ovos-and-mycroft-a-fork-that-wasnt-meant-to-be [V]
- https://community.mycroft.ai/t/faq-ovos-neon-and-the-future-of-the-mycroft-voice-assistant [V]
- https://www.cnx-software.com/2025/02/24/the-openvoiceos-foundation-aims-to-enable-open-source-privacy-and-customization-for-voice-assistants/ [V]
- https://lwn.net/Articles/926484 (open-source voice assistants overview) [not opened]
- https://support.google.com/googlenest/answer/16618650 and https://support.google.com/googlehome/answer/7320960 (Gemini for Home, Voice Match) [V]
- https://arxiv.org/pdf/2407.04293 (online diarization latency; ECAPA slowest) [V]
- https://arxiv.org/pdf/2011.01709 (small-footprint speaker verification, Pi 3B) [V]
- https://huggingface.co/kenrunio/transcriber-speaker-onnx (ECAPA-TDNN ONNX, 192-dim) [V]
- https://www.sourcepulse.org/projects/1896301 (sherpa-onnx) [V]
- https://www.gearbrain.com/amp/alexa-voice-profile-feature-explained-2647656954 (Alexa voice profiles) [V]
- Not found or not verified: Willow current status, Picovoice licensing and pricing, Leon activity, Siri personal-request documentation, and any Pi 4 benchmark for speaker-ID models.