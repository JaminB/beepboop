"""The capture-thread front end: ring buffer, Silero VAD, wake word (VOICE_PIPELINE §3, §6, §7).

:meth:`FrontEnd.process` runs synchronously on the capture thread for every
10 ms frame and must return within it. It stamps nothing itself: the capture
loop passes the ``time.monotonic_ns()`` taken at the capture callback, and a
wake hit's ``t_wake`` is replaced by that frame stamp.

* ``muted``: nothing runs and the ring buffer stays empty (§13.2).
* ``armed``: wake detection runs only when the pipeline arms it (``IDLE``, and
  during enrollment so the wake-mode quality gate can see hits).
* VAD gate (§6.2): a hit counts only if Silero saw speech in the last 300 ms.
"""

from __future__ import annotations

import dataclasses
import threading
from collections import deque

from beepboop_audio.frames import FRAME_MS, Frame, RingBuffer
from beepboop_audio.pipeline.vad import VadFramer
from beepboop_audio.types import PCM16, WakeProvider

VAD_GATE_MS = 300


class FrontEnd:
    def __init__(
        self,
        wake: WakeProvider,
        vad: VadFramer,
        ring: RingBuffer,
        *,
        vad_gate: bool = True,
    ) -> None:
        self.wake = wake
        self.vad = vad
        self.ring = ring
        self.vad_gate = vad_gate
        self._recent: deque[bool] = deque(maxlen=VAD_GATE_MS // FRAME_MS)
        self._armed = threading.Event()
        self._muted = threading.Event()
        self._lock = threading.Lock()
        self.gated_hits = 0

    # Flags flipped from the event loop, read on the capture thread.

    @property
    def armed(self) -> bool:
        return self._armed.is_set()

    def arm(self, on: bool) -> None:
        if on == self._armed.is_set():
            return
        if on:
            with self._lock:
                self.wake.reset()
            self._armed.set()
        else:
            self._armed.clear()

    @property
    def muted(self) -> bool:
        return self._muted.is_set()

    def set_muted(self, on: bool) -> None:
        if on:
            self._muted.set()
            with self._lock:
                self.ring.clear()
                self.vad.reset()
                self._recent.clear()
        else:
            self._muted.clear()

    def clear_ring(self) -> None:
        with self._lock:
            self.ring.clear()

    def preroll(self, n_samples: int) -> PCM16:
        with self._lock:
            return self.ring.last(n_samples)

    def process(self, pcm: PCM16, t_ns: int) -> Frame | None:
        """One frame; ``None`` while muted (no wake, no VAD, no ring buffer)."""
        if self._muted.is_set():
            return None
        with self._lock:
            self.ring.push(pcm)
            speech = self.vad.push(pcm)
            self._recent.append(speech)
            hit = None
            if self._armed.is_set():
                hit = self.wake.process(pcm)
                if hit is not None:
                    if self.vad_gate and not any(self._recent):
                        self.gated_hits += 1
                        hit = None
                    else:
                        hit = dataclasses.replace(hit, t_wake_ns=t_ns)
        return Frame(pcm=pcm, t_ns=t_ns, speech=speech, wake=hit)


__all__ = ["VAD_GATE_MS", "FrontEnd"]
