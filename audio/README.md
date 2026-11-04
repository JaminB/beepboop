# audio: the `beepboop-audio` package

The audio service (container `beepboop-audio`, `network_mode: none`): ALSA I/O, wake
word, VAD, speaker ID, STT, TTS and earcons. It speaks to core only through the text
event socket ([ARCHITECTURE §4](../docs/ARCHITECTURE.md#4-audio--core-event-protocol));
no audio ever leaves this package and none is ever written to disk
([CLAUDE.md hard rule 4](../CLAUDE.md#4-hard-rules), [ADR 0018](../docs/adr/0018-no-audio-retention.md)).
The spec is [VOICE_PIPELINE](../docs/VOICE_PIPELINE.md); the provider Protocols are its §17.

## Layout

| Path | What |
|---|---|
| `beepboop_audio/types.py` | The §17 Protocols and dataclasses |
| `wake/microwakeword/`, `wake/decider.py` | `microwakeword` provider; window, cutoff, refractory |
| `speaker/sherpa_campp/`, `speaker/identifier.py`, `speaker/clustering.py` | `sherpa-campp` embedder; `SpeakerIdentifier` (multi-centroid, AS-norm, calibration, accept rule, Guest fallback, fusion) |
| `stt/sherpa_zipformer/` | `sherpa-zipformer` streaming provider; `bpe.py` reads the pinned `bpe.model` for hotwords |
| `tts/sherpa_piper/`, `tts/normalize_en.py`, `tts/phrases.py` | `sherpa-piper` (piper-phonemize → sherpa-onnx); text normalisation; system phrases and the in-RAM clip cache |
| `pipeline/machine.py` | The turn state machine: wake → provisional/final ID → STT → speak, reply window, PIN capture, mute, enrollment |
| `pipeline/frontend.py`, `vad.py`, `endpoint.py` | Capture-thread front end (ring buffer, Silero VAD, wake, VAD gate); endpointing |
| `pipeline/mixer.py`, `earcons.py`, `leds.py` | Three-lane mixer; generated earcons; LED states and the mute-button seam |
| `pipeline/io.py`, `alsa.py` | Capture/playback devices and threads; raw ALSA via `ctypes` |
| `pipeline/pin.py`, `enroll.py` | Spoken-PIN verification (argon2id, digits never leave); enrollment quality gates |
| `service.py`, `cli.py` | Service wiring, `hello`/`health`, event server; `beepboop-audio serve` |
| `testing.py`, `harness.py` | Fakes for every Protocol and a deterministic pipeline harness |

## Run

```bash
uv sync --all-packages
uv run beepboop-audio version
uv run beepboop-audio serve --fake-mic some.wav --socket tcp://127.0.0.1:8765   # real models
uv run beepboop-audio serve --fake-providers --fake-mic some.wav               # no models
uv run pytest audio                                   # unit + conformance (skips missing models)
uv run pytest -m conformance audio/tests -k sherpa-zipformer
```

Models resolve through `models/manifest.yaml` under `$BB_MODELS_DIR` (`/models` in the
container; fetch with `python -m beepboop_bench.models fetch`). Until the stock
`hey_beepboop` model is published, set `providers.wake.models: [okay_nabu]` for
development (a pymicro-wakeword builtin; never a G5 result).

## Pending on Pi

Built and tested on x86 with fakes and recorded clips; **not** run on a Pi or an XVF3800:
ALSA capture/playback on the array (channel map V3, 24 h soak V4), the LED ring and
mute-button control path (`pipeline/leds.py` has only the log/recording drivers), every
latency and accuracy number (G1, G2, G5, G6), the hotword WER delta, the thermal
degradation switch, and the filesystem scan for audio bytes on the Pi runner
(ROADMAP v0.1 #8; the x86 tests in `tests/test_no_audio_on_disk.py` cover the code paths).
