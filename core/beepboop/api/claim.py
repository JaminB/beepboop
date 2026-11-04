"""The first-boot claim code (docs/WEB_UI.md §5.1, §6.1; OPERATIONS §6).

Six digits, spoken and blinked by the device, single use, 15 minutes, 5 attempts. It is
the only way to create the first admin. After onboarding, re-opening claim mode
(``beepboop claim --reopen``) issues a code that only lets an admin recovery code in.

The code lives in memory only: it is never written to the database or a log. The device
learns it through the ``on_code`` callback (the audio bridge speaks it; a dev server may
show it on its console).
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

CLAIM_TTL = timedelta(minutes=15)
CLAIM_ATTEMPTS = 5
CLAIM_DIGITS = 6

ClaimMode = Literal["first_admin", "recovery"]
ClaimResult = Literal["ok", "wrong", "expired", "exhausted", "none"]


@dataclass
class _Code:
    code: str
    mode: ClaimMode
    issued: datetime
    attempts: int = 0
    used: bool = False


@dataclass
class ClaimManager:
    """One live claim code at a time."""

    on_code: Callable[[str, ClaimMode], None] = lambda _code, _mode: None
    _current: _Code | None = field(default=None, repr=False)

    def issue(self, now: datetime, mode: ClaimMode = "first_admin") -> str:
        code = "".join(secrets.choice("0123456789") for _ in range(CLAIM_DIGITS))
        self._current = _Code(code, mode, now)
        self.on_code(code, mode)
        return code

    def mode(self, now: datetime) -> ClaimMode | None:
        c = self._current
        if c is None or c.used or now - c.issued >= CLAIM_TTL or c.attempts >= CLAIM_ATTEMPTS:
            return None
        return c.mode

    def peek(self) -> str | None:
        """The live code for the device to speak (memory only; never logged), or ``None``."""
        c = self._current
        return None if c is None or c.used else c.code

    def check(self, code: str, now: datetime) -> tuple[ClaimResult, ClaimMode | None]:
        c = self._current
        if c is None or c.used:
            return "none", None
        if now - c.issued >= CLAIM_TTL:
            return "expired", c.mode
        if c.attempts >= CLAIM_ATTEMPTS:
            return "exhausted", c.mode
        c.attempts += 1
        if not hmac.compare_digest(c.code, code.strip().replace(" ", "")):
            return ("exhausted" if c.attempts >= CLAIM_ATTEMPTS else "wrong"), c.mode
        c.used = True
        return "ok", c.mode

    def clear(self) -> None:
        self._current = None


__all__ = ["CLAIM_ATTEMPTS", "CLAIM_TTL", "ClaimManager", "ClaimMode", "ClaimResult"]
