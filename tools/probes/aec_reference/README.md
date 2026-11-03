# tools/probes/aec_reference: V3, is playback through the array the AEC reference?

**Status: code done; result PENDING (owner with the Pi, the XVF3800, a powered speaker and a
second person).** Nothing can be measured without the hardware.

ROADMAP V3: the XVF3800 16 kHz USB firmware exposes the processed ASR beam (channel 0), and
playback through the array's own output is the echo-canceller reference, so TTS at 70 dB SPL
from 1 m neither triggers the wake word nor corrupts STT of a simultaneous speaker. If this
cannot be confirmed, barge-in stays in v0.3+ and the audio service mutes the wake path during
playback (VOICE_PIPELINE §2.4, §15.2).

The measurement is `beepboop bench aec` (`tools/bench/beepboop_bench/aec.py`): two 30 s phases,
quiet then with a 10 s TTS clip playing through the array, during which a person at 2 m says
"hey beepboop, turn off the kitchen lights" five times. It reports wake-hit rate and STT WER per
phase and sets `aec_verified` when the playback phase keeps >= 90 % of the quiet hit rate and
WER within 5 absolute points. Audio is captured straight from `arecord` into the models and
never written to disk (ADR 0018).

## Procedure (owner)

1. Pi 4 with the pinned 16 kHz firmware on the array, powered speaker on the array's audio out,
   SPL meter app on a phone. `arecord -l` and `aplay -l` to find the card, e.g. `hw:1`.
2. Install the stock `hey_beepboop` model (`python -m beepboop_bench.models fetch` once it is
   pinned) and the TTS voice.
3. Set the speaker so the TTS clip reads **70 dB SPL at 1 m** (`speaker-test` or
   `aplay` a tone first and read the meter).
4. Run, with the second person standing 2 m from the array:

   ```bash
   uv run beepboop bench aec --device hw:1 --host pi4-4gb
   ```

   Follow the prompts: five utterances in silence, then five while the clip plays.
5. The result lands in `tools/bench/results/<date>-pi4-4gb.json` under `sections.aec`. Record
   `aec_verified`, the firmware version and the date in VOICE_PIPELINE §2.4; if false, note
   that barge-in is deferred and the wake path is muted during playback.

## Also confirm while you are there (VOICE_PIPELINE §2.2-2.3)

- `arecord -D hw:1 --dump-hw-params` shows 6 channels, S16_LE, 16000 Hz.
- Channel 0 is the processed beam (speak from different angles; channels 2-5 are raw mics).
- Whether the playback endpoint needs 48 kHz (`aplay --dump-hw-params`).
