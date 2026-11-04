"""Spoken-PIN hook (IDENTITY_AND_TRUST §8.4): hashing and the lockout state machine.

The PIN itself is verified inside the audio service against the argon2id hash core
sends in ``capture_pin``; core only sees the outcome. This module owns the rules in the
§8.4 table so they exist in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

PinOutcome = Literal["ok", "mismatch", "timeout", "cancelled"]

PIN_MIN_LEN = 4
PIN_MAX_LEN = 8
PIN_CAPTURE_TIMEOUT_MS = 10_000
LOCK_AFTER_FAILURES = 3
LOCK_WINDOW = timedelta(minutes=15)
LOCK_DURATION = timedelta(minutes=5)
DISABLE_AFTER_FAILURES = 10
DISABLE_WINDOW = timedelta(hours=24)

_hasher = PasswordHasher()


class PinFormatError(ValueError):
    """Not 4-8 digits."""


def validate_pin(pin: str) -> None:
    if not (PIN_MIN_LEN <= len(pin) <= PIN_MAX_LEN and pin.isdigit()):
        msg = f"PIN must be {PIN_MIN_LEN}-{PIN_MAX_LEN} digits"
        raise PinFormatError(msg)


def hash_pin(pin: str) -> str:
    """argon2id PHC string; stored DEK-wrapped in ``profiles.pin_hash_enc``."""
    validate_pin(pin)
    return _hasher.hash(pin)


def verify_pin(pin_hash: str, pin: str) -> bool:
    """Used by the audio service's test double and by tests; production verification is in audio."""
    try:
        return _hasher.verify(pin_hash, pin)
    except VerificationError:
        return False


@dataclass(frozen=True, slots=True)
class PinState:
    """The PIN columns of a ``profiles`` row."""

    enabled: bool
    failures: int = 0
    locked_until: datetime | None = None
    window_started_at: datetime | None = None
    failures_24h: int = 0
    window_24h_started_at: datetime | None = None

    def is_locked(self, now: datetime) -> bool:
        return self.locked_until is not None and now < self.locked_until


@dataclass(frozen=True, slots=True)
class PinTransition:
    state: PinState
    event: Literal["ok", "mismatch", "locked", "disabled", "ignored"]


def record_outcome(state: PinState, outcome: PinOutcome, now: datetime) -> PinTransition:
    """Apply one ``pin.result`` to the profile's PIN state (§8.4 lockout table).

    * ``ok`` resets failures.
    * ``mismatch`` counts; 3 within 15 min locks for 5 min; 10 within 24 h disables the PIN.
    * ``timeout`` / ``cancelled`` change nothing.
    """
    if outcome == "ok":
        return PinTransition(
            replace(state, failures=0, locked_until=None, window_started_at=None), "ok"
        )
    if outcome != "mismatch":
        return PinTransition(state, "ignored")

    window_start = state.window_started_at
    failures = state.failures
    if window_start is None or now - window_start > LOCK_WINDOW:
        window_start, failures = now, 0
    failures += 1

    window24 = state.window_24h_started_at
    failures24 = state.failures_24h
    if window24 is None or now - window24 > DISABLE_WINDOW:
        window24, failures24 = now, 0
    failures24 += 1

    new = replace(
        state,
        failures=failures,
        window_started_at=window_start,
        failures_24h=failures24,
        window_24h_started_at=window24,
    )
    if failures24 >= DISABLE_AFTER_FAILURES:
        return PinTransition(replace(new, enabled=False, locked_until=None), "disabled")
    if failures >= LOCK_AFTER_FAILURES:
        return PinTransition(replace(new, locked_until=now + LOCK_DURATION), "locked")
    return PinTransition(new, "mismatch")
