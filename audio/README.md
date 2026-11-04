# audio: the `beepboop-audio` package

The audio service (container `beepboop-audio`, `network_mode: none`): ALSA I/O, wake
word, VAD, speaker ID, STT, TTS and earcons. It speaks to core only through the text
event socket ([ARCHITECTURE §4](../docs/ARCHITECTURE.md#4-audio--core-event-protocol));
no audio ever leaves this package ([CLAUDE.md hard rule 4](../CLAUDE.md#4-hard-rules)).
Provider Protocols: [VOICE_PIPELINE §17](../docs/VOICE_PIPELINE.md#17-provider-interfaces).

**State (M0):** `beepboop_audio/cli.py` (an argparse CLI so the audio image stays free
of Typer/Click; it installs the shared redacting log setup) and `tests/`. The event
protocol (M0 step 2) and the log setup live in the shared
[`protocol/`](../protocol/README.md) package (`beepboop-protocol`, a dependency of both
services; [ADR 0019](../docs/adr/0019-shared-protocol-package-and-negotiated-embedding-dim.md)). The one piece of the speech pipeline that exists is the product TTS phonemiser,
`beepboop_audio/tts/sherpa_piper/phonemize.py` (`piper-phonemize` over espeak-ng,
GPL-3.0; [ADR 0020](../docs/adr/0020-gpl-3-and-espeak-phonemiser.md)), which the bench
re-exports so it measures what the product runs. No providers or service loop yet; the
stage benchmarks live in `tools/bench/`. The pinned speech runtimes (`sherpa-onnx` 2.0.1,
`pymicro-wakeword` 2.5.0, `piper-phonemize` 1.5.9) are declared in `pyproject.toml` and
install on Windows x86_64, Linux x86_64 and Linux aarch64 from wheels.

## Run

```bash
uv sync --all-packages
uv run beepboop-audio version
uv run pytest audio
```

## Pending on hardware

Everything that touches the XVF3800 or ALSA (V3 AEC reference, V4 24 h ALSA soak) is
an owner action on the Pi; see [ROADMAP M0](../docs/ROADMAP.md#m0-prove-the-pi).
