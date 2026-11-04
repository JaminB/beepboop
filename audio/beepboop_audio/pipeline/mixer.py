"""The playback mixer: one stream, three lanes, a 20 ms period (VOICE_PIPELINE §11.4).

* ``earcon``: state chimes, the PIN prompt tone. Plays over anything, never queued.
* ``speech``: TTS and clips for the active turn. FIFO; ``cancel`` flushes it.
* ``alert``: timers, alarms, announcements. Ducks speech by -12 dB; loops until stopped.

The playback thread calls :meth:`Mixer.next_period` once per period and writes
the result to the device; everything else is called from the event loop.
Speech items may still be growing while they play (TTS streams into them); an
item that runs dry before it is closed plays silence until more PCM arrives.
Playback callbacks (``on_start``/``on_end``) run on the playback thread and
receive the ``time.monotonic_ns()`` at which the period was handed out.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from beepboop_audio.frames import SAMPLE_RATE, samples_to_ms
from beepboop_audio.types import PCM16

PERIOD_SAMPLES = 320  # 20 ms
DUCK_GAIN = 10.0 ** (-12.0 / 20.0)
#: An alert that nobody stops is cut after this long (target; a stuck alarm must not ring forever).
ALERT_MAX_MS = 5 * 60 * 1000

type Clock = Callable[[], int]
type Stamp = Callable[[int], None]


@dataclass(eq=False)
class SpeechItem:
    """One utterance segment on the speech lane (a sentence or a cached clip)."""

    turn_id: str | None
    utterance_id: str
    on_start: Stamp | None = None
    on_end: Callable[[int, int, bool], None] | None = None  # (t_end_ns, samples, cancelled)
    _chunks: deque[PCM16] = field(default_factory=deque[PCM16])
    _offset: int = 0
    closed: bool = False
    started: bool = False
    played: int = 0
    queued: int = 0

    def append(self, pcm: PCM16) -> None:
        if len(pcm):
            self._chunks.append(pcm)
            self.queued += len(pcm)

    def close(self) -> None:
        self.closed = True

    @property
    def drained(self) -> bool:
        return self.closed and not self._chunks

    def take(self, n: int) -> PCM16:
        out: list[PCM16] = []
        need = n
        while need and self._chunks:
            head = self._chunks[0]
            piece = head[self._offset : self._offset + need]
            out.append(piece)
            need -= len(piece)
            self._offset += len(piece)
            if self._offset >= len(head):
                self._chunks.popleft()
                self._offset = 0
        if not out:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(out)

    def wipe(self) -> None:
        for chunk in self._chunks:
            chunk.fill(0)
        self._chunks.clear()
        self._offset = 0


@dataclass(eq=False)
class _Alert:
    pcm: PCM16
    turn_id: str | None
    loop: bool
    pos: int = 0
    played: int = 0


class Mixer:
    def __init__(self, *, clock: Clock = time.monotonic_ns, volume: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._earcons: list[tuple[PCM16, int]] = []
        self._speech: deque[SpeechItem] = deque()
        self._alerts: list[_Alert] = []
        self._clock = clock
        self.volume = volume
        self.alert_floor = 0.5  # alerts never play quieter than this, whatever the volume

    # -- producers (event loop) ------------------------------------------------

    def play_earcon(self, pcm: PCM16) -> None:
        with self._lock:
            self._earcons.append((pcm, 0))

    def enqueue(self, item: SpeechItem) -> None:
        with self._lock:
            self._speech.append(item)

    def play_alert(self, pcm: PCM16, *, turn_id: str | None, loop: bool) -> None:
        with self._lock:
            self._alerts.append(_Alert(pcm, turn_id, loop))

    def flush_speech(self, turn_id: str | None = None) -> list[SpeechItem]:
        """Drop queued speech (all, or one turn's); returns the dropped items."""
        with self._lock:
            keep: deque[SpeechItem] = deque()
            dropped: list[SpeechItem] = []
            for item in self._speech:
                if turn_id is None or item.turn_id == turn_id:
                    dropped.append(item)
                else:
                    keep.append(item)
            self._speech = keep
        now = self._clock()
        for item in dropped:
            item.wipe()
            if item.on_end is not None:
                item.on_end(now, item.played, True)
        return dropped

    def stop_alerts(self, turn_id: str | None = None) -> int:
        with self._lock:
            before = len(self._alerts)
            self._alerts = [a for a in self._alerts if turn_id is not None and a.turn_id != turn_id]
            return before - len(self._alerts)

    # -- state -----------------------------------------------------------------

    @property
    def speech_busy(self) -> bool:
        with self._lock:
            return bool(self._speech)

    @property
    def alert_active(self) -> bool:
        with self._lock:
            return bool(self._alerts)

    def queue_ms(self) -> int:
        with self._lock:
            pending = sum(i.queued - i.played for i in self._speech)
        return samples_to_ms(max(0, pending))

    # -- consumer (playback thread) --------------------------------------------

    def next_period(self, n: int = PERIOD_SAMPLES) -> PCM16:
        """Mix and return the next ``n`` samples (int16)."""
        events: list[Callable[[], None]] = []
        acc: npt.NDArray[np.float32] = np.zeros(n, dtype=np.float32)
        now = self._clock()
        with self._lock:
            alerting = bool(self._alerts)
            self._mix_speech(acc, n, now, events, DUCK_GAIN if alerting else 1.0)
            acc *= self.volume
            alert_gain = max(self.volume, self.alert_floor)
            for alert in list(self._alerts):
                self._mix_alert(acc, alert, n, alert_gain)
            remaining: list[tuple[PCM16, int]] = []
            for pcm, pos in self._earcons:
                piece = pcm[pos : pos + n]
                acc[: len(piece)] += piece.astype(np.float32) * self.volume
                if pos + n < len(pcm):
                    remaining.append((pcm, pos + n))
            self._earcons = remaining
        for fire in events:
            fire()
        return np.clip(acc, -32768, 32767).astype(np.int16)

    def _mix_speech(
        self,
        acc: npt.NDArray[np.float32],
        n: int,
        now: int,
        events: list[Callable[[], None]],
        gain: float,
    ) -> None:
        filled = 0
        while filled < n and self._speech:
            item = self._speech[0]
            piece = item.take(n - filled)
            if len(piece):
                if not item.started:
                    item.started = True
                    if item.on_start is not None:
                        cb = item.on_start
                        t_start = now + (filled * 1_000_000_000) // SAMPLE_RATE
                        events.append(lambda cb=cb, t=t_start: cb(t))
                acc[filled : filled + len(piece)] += piece.astype(np.float32) * gain
                filled += len(piece)
                item.played += len(piece)
            if item.drained:
                self._speech.popleft()
                if item.on_end is not None:
                    end_cb = item.on_end
                    t_end = now + (filled * 1_000_000_000) // SAMPLE_RATE
                    played = item.played
                    events.append(lambda cb=end_cb, t=t_end, p=played: cb(t, p, False))
                continue
            if not len(piece):
                break  # still synthesising: underrun, wait for more PCM

    def _mix_alert(self, acc: npt.NDArray[np.float32], alert: _Alert, n: int, gain: float) -> None:
        filled = 0
        while filled < n:
            piece = alert.pcm[alert.pos : alert.pos + n - filled]
            acc[filled : filled + len(piece)] += piece.astype(np.float32) * gain
            filled += len(piece)
            alert.pos += len(piece)
            alert.played += len(piece)
            if alert.pos >= len(alert.pcm):
                if alert.loop and samples_to_ms(alert.played) < ALERT_MAX_MS:
                    alert.pos = 0
                else:
                    self._alerts.remove(alert)
                    return
            if not len(piece):
                return


__all__ = ["ALERT_MAX_MS", "DUCK_GAIN", "PERIOD_SAMPLES", "Mixer", "SpeechItem"]
