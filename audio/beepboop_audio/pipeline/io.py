"""Audio I/O: capture and playback devices and the two real-time threads (VOICE_PIPELINE §2.3).

Devices:

* :class:`~beepboop_audio.pipeline.alsa.AlsaCapture` / ``AlsaPlayback``: raw
  ALSA on the Pi (pending on Pi: XVF3800 channel map, V3/V4).
* :class:`FileCapture`: ``beepboop-audio serve --fake-mic <wav>`` and tests.
  Reads a WAV (any rate/channels; resampled to 16 kHz mono in memory), then
  keeps producing silence so the pipeline can finish its turn.
* :class:`NullPlayback` / :class:`MemoryPlayback`: discard (paced in real time
  or not) or keep the mixed output in RAM for assertions.

Nothing here writes audio anywhere but the playback device (ADR 0018).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np
import structlog

from beepboop_audio._audiofile import read_audio, resample
from beepboop_audio.frames import FRAME_SAMPLES, SAMPLE_RATE, Frame, to_int16
from beepboop_audio.pipeline.mixer import PERIOD_SAMPLES, Mixer
from beepboop_audio.types import PCM16

log = structlog.get_logger(__name__)


class CaptureDevice(Protocol):
    def read(self) -> PCM16 | None:
        """Block for the next 10 ms mono frame (the processed ASR beam); None = device gone."""
        ...

    def close(self) -> None: ...


class PlaybackDevice(Protocol):
    def write(self, pcm: PCM16) -> None:
        """Write one 20 ms mono period; blocks for about one period on a real device."""
        ...

    def close(self) -> None: ...


class FileCapture:
    """A WAV file as the microphone (``--fake-mic``); then silence."""

    def __init__(self, path: Path, *, realtime: bool = True, channel: int = 0) -> None:
        data, rate = read_audio(path)
        mono = resample(
            np.asarray(data[:, min(channel, data.shape[1] - 1)], dtype=np.float32),
            rate,
            SAMPLE_RATE,
        )
        self._pcm = to_int16(mono)
        self._pos = 0
        self._realtime = realtime
        self._next = time.monotonic()
        self._closed = False

    @property
    def exhausted(self) -> bool:
        return self._pos >= len(self._pcm)

    def read(self) -> PCM16 | None:
        if self._closed:
            return None
        if self._realtime:
            self._next += FRAME_SAMPLES / SAMPLE_RATE
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        piece = self._pcm[self._pos : self._pos + FRAME_SAMPLES]
        self._pos += FRAME_SAMPLES
        if len(piece) < FRAME_SAMPLES:
            piece = np.concatenate([piece, np.zeros(FRAME_SAMPLES - len(piece), dtype=np.int16)])
        return piece

    def close(self) -> None:
        self._closed = True
        self._pcm.fill(0)


class SilentCapture:
    """A microphone that hears nothing (``serve`` with no device, and tests)."""

    def __init__(self, *, realtime: bool = True) -> None:
        self._realtime = realtime
        self._closed = False

    def read(self) -> PCM16 | None:
        if self._closed:
            return None
        if self._realtime:
            time.sleep(FRAME_SAMPLES / SAMPLE_RATE)
        return np.zeros(FRAME_SAMPLES, dtype=np.int16)

    def close(self) -> None:
        self._closed = True


class NullPlayback:
    def __init__(self, *, realtime: bool = True) -> None:
        self._realtime = realtime
        self._next = time.monotonic()

    def write(self, pcm: PCM16) -> None:
        if self._realtime:
            self._next += len(pcm) / SAMPLE_RATE
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                self._next = time.monotonic()

    def close(self) -> None:
        return None


class MemoryPlayback:
    """Keeps every written period in RAM (tests only; never written to disk)."""

    def __init__(self) -> None:
        self.periods: list[PCM16] = []

    def write(self, pcm: PCM16) -> None:
        self.periods.append(pcm.copy())

    def close(self) -> None:
        return None

    def audio(self) -> PCM16:
        return np.concatenate(self.periods) if self.periods else np.zeros(0, dtype=np.int16)


def _realtime_priority() -> None:
    """SCHED_FIFO for the capture thread when the container grants it (compose: SYS_NICE)."""
    if sys.platform != "linux":
        return
    with contextlib.suppress(OSError, AttributeError, PermissionError):
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(10))


class CaptureThread(threading.Thread):
    """Reads frames, runs the front end, hands frames to the event loop.

    ``process(pcm, t_ns)`` is :meth:`FrontEnd.process`; ``sink(frame)`` must be
    thread-safe (the service uses ``loop.call_soon_threadsafe``). ``on_lost`` is
    called once if the device disappears (VOICE_PIPELINE §2.3: fatal, the
    container restarts).
    """

    def __init__(
        self,
        device: CaptureDevice,
        process: Callable[[PCM16, int], Frame | None],
        sink: Callable[[Frame], None],
        *,
        on_lost: Callable[[], None] | None = None,
        heartbeat: Callable[[], None] | None = None,
        heartbeat_s: float = 10.0,
    ) -> None:
        super().__init__(name="bb-capture", daemon=True)
        self._device = device
        self._process = process
        self._sink = sink
        self._on_lost = on_lost
        self._heartbeat = heartbeat
        self._heartbeat_ns = int(heartbeat_s * 1e9)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        _realtime_priority()
        last_beat = 0
        while not self._stop_event.is_set():
            try:
                pcm = self._device.read()
            except OSError as exc:
                log.error("capture.device_error", error=str(exc))
                pcm = None
            if pcm is None:
                if not self._stop_event.is_set() and self._on_lost is not None:
                    self._on_lost()
                return
            t_ns = time.monotonic_ns()
            frame = self._process(pcm, t_ns)
            if frame is not None:
                self._sink(frame)
            if self._heartbeat is not None and t_ns - last_beat >= self._heartbeat_ns:
                last_beat = t_ns
                with contextlib.suppress(OSError):
                    self._heartbeat()


class PlaybackThread(threading.Thread):
    """Pulls one 20 ms period from the mixer and writes it, forever."""

    def __init__(self, device: PlaybackDevice, mixer: Mixer) -> None:
        super().__init__(name="bb-playback", daemon=True)
        self._device = device
        self._mixer = mixer
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            pcm = self._mixer.next_period(PERIOD_SAMPLES)
            try:
                self._device.write(pcm)
            except OSError as exc:
                log.error("playback.device_error", error=str(exc))
                return


def heartbeat_touch(path: Path) -> Callable[[], None]:
    """The compose healthcheck's heartbeat: an empty file whose mtime the capture thread bumps."""

    def touch() -> None:
        path.touch()

    return touch


__all__ = [
    "CaptureDevice",
    "CaptureThread",
    "FileCapture",
    "MemoryPlayback",
    "NullPlayback",
    "PlaybackDevice",
    "PlaybackThread",
    "SilentCapture",
    "heartbeat_touch",
]
