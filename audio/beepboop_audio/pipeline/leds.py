"""LED states and the hardware mute button (VOICE_PIPELINE §13).

The XVF3800 has an RGB ring and a mute button driven through its vendor
control interface; the exact path from inside a container is an M0
verification item (**pending on Pi**), so the drivers are a seam:

* :class:`LedDriver` receives one :class:`LedState` per state change.
  :class:`LogLed` (default) only logs; :class:`RecordingLed` keeps the sequence
  for tests. The XVF3800 ring and the single-GPIO fallback implement the same
  Protocol once V3 confirms the control path.
* :class:`MuteButton` is polled every 100 ms by the service; ``None`` means
  "no hardware state available" (:class:`NoMuteButton`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)


class LedState(StrEnum):
    """The §13.1 rows, by name."""

    IDLE = "idle"  # off
    LISTENING = "listening"  # blue, slow breathe
    THINKING = "thinking"  # blue, spinning quarter
    TOOL = "tool"  # blue, quick pulse (ack)
    DONE = "done"  # blue, fade
    SPEAKING = "speaking"  # blue, dim steady
    REPLY_WINDOW = "reply_window"  # blue, dim pulse
    CANCEL = "cancel"  # fades out
    ERROR = "error"  # amber flash x2
    OFFLINE = "offline"  # amber, slow fade
    PIN = "pin"  # amber, steady
    MUTED = "muted"  # red, dim steady
    ALERT = "alert"  # white pulse
    TOO_LONG = "too_long"  # blue flash
    READY = "ready"  # green sweep once
    ENROLL = "enroll"  # blue (the "say it when the ring turns blue" cue)


class LedDriver(Protocol):
    def show(self, state: LedState) -> None: ...


class LogLed:
    def __init__(self) -> None:
        self.state = LedState.IDLE

    def show(self, state: LedState) -> None:
        if state != self.state:
            self.state = state
            log.debug("led.state", state=str(state))


class RecordingLed:
    def __init__(self) -> None:
        self.states: list[LedState] = []

    def show(self, state: LedState) -> None:
        if not self.states or self.states[-1] != state:
            self.states.append(state)


class MuteButton(Protocol):
    def poll(self) -> bool | None:
        """Current hardware mute state, or None if the hardware cannot report it."""
        ...


class NoMuteButton:
    def poll(self) -> bool | None:
        return None


class FakeMuteButton:
    def __init__(self, muted: bool = False) -> None:
        self.muted = muted

    def poll(self) -> bool | None:
        return self.muted


__all__ = [
    "FakeMuteButton",
    "LedDriver",
    "LedState",
    "LogLed",
    "MuteButton",
    "NoMuteButton",
    "RecordingLed",
]
