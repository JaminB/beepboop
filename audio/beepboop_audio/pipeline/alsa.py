"""Raw ALSA capture and playback through ``libasound.so.2`` and ``ctypes`` (VOICE_PIPELINE §2.3).

No PipeWire or PulseAudio in the path and no extra Python dependency: the
container has ``/dev/snd`` and ``libasound2``. One capture stream (S16_LE,
16 kHz, 6 ch, period 160 frames = 10 ms) and one playback stream (S16_LE,
16 kHz, 2 ch, period 320 frames = 20 ms, 4 periods) on the same device.
Capture returns the configured channel (0 = the XVF3800's processed ASR beam).
If the playback endpoint will not run at 16 kHz the stream resamples with
``soxr`` at the last step; the internal rate stays 16 kHz. Overruns and
underruns are recovered with ``snd_pcm_recover`` and logged; any other error
is fatal (device loss restarts the container).

**Pending on Pi**: the XVF3800 channel map (V3) and the 24 h soak (V4) have not
run; on Windows and on hosts without ``libasound`` construction raises
:class:`AlsaUnavailableError`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from functools import cache

import numpy as np
import structlog

from beepboop_audio._audiofile import Resampler
from beepboop_audio.frames import FRAME_SAMPLES, SAMPLE_RATE
from beepboop_audio.pipeline.mixer import PERIOD_SAMPLES
from beepboop_audio.types import PCM16

log = structlog.get_logger(__name__)

SND_PCM_STREAM_PLAYBACK = 0
SND_PCM_STREAM_CAPTURE = 1
SND_PCM_ACCESS_RW_INTERLEAVED = 3
SND_PCM_FORMAT_S16_LE = 2
EPIPE = 32
ESTRPIPE = 86
PLAYBACK_PERIODS = 4


class AlsaUnavailableError(RuntimeError):
    """libasound is not available on this host (Windows, macOS, a bare container)."""


class AlsaError(OSError):
    """An ALSA call failed (other than a recovered over/underrun)."""


@cache
def _lib() -> ctypes.CDLL:
    if sys.platform != "linux":
        raise AlsaUnavailableError("raw ALSA needs Linux; use --fake-mic on this host")
    name = ctypes.util.find_library("asound") or "libasound.so.2"
    try:
        lib = ctypes.CDLL(name)
    except OSError as exc:
        raise AlsaUnavailableError(f"libasound not found ({exc}); install libasound2") from exc
    lib.snd_strerror.restype = ctypes.c_char_p
    lib.snd_pcm_readi.restype = ctypes.c_long
    lib.snd_pcm_writei.restype = ctypes.c_long
    return lib


def _check(rc: int, what: str) -> int:
    if rc < 0:
        msg = (_lib().snd_strerror(rc) or b"?").decode(errors="replace")
        raise AlsaError(f"{what}: {msg} ({rc})")
    return rc


class _Pcm:
    def __init__(
        self, device: str, stream: int, *, channels: int, rate: int, period: int, periods: int
    ) -> None:
        lib = _lib()
        self._lib = lib
        self._pcm = ctypes.c_void_p()
        _check(lib.snd_pcm_open(ctypes.byref(self._pcm), device.encode(), stream, 0), "open")
        params = ctypes.c_void_p()
        _check(lib.snd_pcm_hw_params_malloc(ctypes.byref(params)), "hw_params_malloc")
        try:
            _check(lib.snd_pcm_hw_params_any(self._pcm, params), "hw_params_any")
            _check(
                lib.snd_pcm_hw_params_set_access(self._pcm, params, SND_PCM_ACCESS_RW_INTERLEAVED),
                "set_access",
            )
            _check(
                lib.snd_pcm_hw_params_set_format(self._pcm, params, SND_PCM_FORMAT_S16_LE),
                "set_format",
            )
            _check(lib.snd_pcm_hw_params_set_channels(self._pcm, params, channels), "set_channels")
            actual_rate = ctypes.c_uint(rate)
            _check(
                lib.snd_pcm_hw_params_set_rate_near(
                    self._pcm, params, ctypes.byref(actual_rate), None
                ),
                "set_rate",
            )
            frames = ctypes.c_ulong(period)
            _check(
                lib.snd_pcm_hw_params_set_period_size_near(
                    self._pcm, params, ctypes.byref(frames), None
                ),
                "set_period",
            )
            buffer = ctypes.c_ulong(period * periods)
            _check(
                lib.snd_pcm_hw_params_set_buffer_size_near(self._pcm, params, ctypes.byref(buffer)),
                "set_buffer",
            )
            _check(lib.snd_pcm_hw_params(self._pcm, params), "hw_params")
        finally:
            lib.snd_pcm_hw_params_free(params)
        _check(lib.snd_pcm_prepare(self._pcm), "prepare")
        self.rate = int(actual_rate.value)
        self.period = int(frames.value)
        self.channels = channels
        self.xruns = 0

    def _recover(self, rc: int, what: str) -> None:
        if rc in (-EPIPE, -ESTRPIPE):
            self.xruns += 1
            log.warning("alsa.xrun", what=what, count=self.xruns)
            _check(self._lib.snd_pcm_recover(self._pcm, rc, 1), "recover")
            return
        _check(rc, what)

    def readi(self, frames: int) -> np.ndarray[tuple[int, int], np.dtype[np.int16]]:
        buf = np.zeros((frames, self.channels), dtype=np.int16)
        got = 0
        while got < frames:
            ptr = buf[got:].ctypes.data_as(ctypes.c_void_p)
            rc = int(self._lib.snd_pcm_readi(self._pcm, ptr, frames - got))
            if rc < 0:
                self._recover(rc, "readi")
                continue
            got += rc
        return buf

    def writei(self, data: np.ndarray[tuple[int, int], np.dtype[np.int16]]) -> None:
        data = np.ascontiguousarray(data)
        done = 0
        while done < len(data):
            ptr = data[done:].ctypes.data_as(ctypes.c_void_p)
            rc = int(self._lib.snd_pcm_writei(self._pcm, ptr, len(data) - done))
            if rc < 0:
                self._recover(rc, "writei")
                continue
            done += rc

    def close(self) -> None:
        if self._pcm:
            self._lib.snd_pcm_close(self._pcm)
            self._pcm = ctypes.c_void_p()


class AlsaCapture:
    """The capture stream; returns one channel, 10 ms per read."""

    def __init__(self, device: str, *, channels: int = 6, channel: int = 0) -> None:
        self._pcm = _Pcm(
            device,
            SND_PCM_STREAM_CAPTURE,
            channels=channels,
            rate=SAMPLE_RATE,
            period=FRAME_SAMPLES,
            periods=8,
        )
        if self._pcm.rate != SAMPLE_RATE:
            self._pcm.close()
            raise AlsaError(f"capture runs at {self._pcm.rate} Hz; the 16 kHz firmware is required")
        self._channel = min(channel, channels - 1)

    def read(self) -> PCM16 | None:
        frames = self._pcm.readi(FRAME_SAMPLES)
        return np.ascontiguousarray(frames[:, self._channel])

    def close(self) -> None:
        self._pcm.close()


class AlsaPlayback:
    """The playback stream: mono periods in, duplicated to every channel."""

    def __init__(self, device: str, *, channels: int = 2) -> None:
        self._pcm = _Pcm(
            device,
            SND_PCM_STREAM_PLAYBACK,
            channels=channels,
            rate=SAMPLE_RATE,
            period=PERIOD_SAMPLES,
            periods=PLAYBACK_PERIODS,
        )
        self._resampler = (
            Resampler(SAMPLE_RATE, self._pcm.rate, dtype="int16")
            if self._pcm.rate != SAMPLE_RATE
            else None
        )

    def write(self, pcm: PCM16) -> None:
        mono = pcm if self._resampler is None else self._resampler.chunk(pcm)
        frames = np.repeat(np.asarray(mono, dtype=np.int16)[:, None], self._pcm.channels, axis=1)
        self._pcm.writei(frames)

    def close(self) -> None:
        self._pcm.close()


__all__ = ["AlsaCapture", "AlsaError", "AlsaPlayback", "AlsaUnavailableError"]
