"""Login rate limits (docs/WEB_UI.md §6.1).

* 5 failed logins per username per 15 min → exponential backoff (and an ``audit`` row,
  written by the router);
* 10 failed logins per client IP per 15 min;
* 5 wrong TOTP codes → 5 min lock.

In memory: a restart forgets counters, which only ever helps the legitimate user; the
argon2id cost is the real brake on guessing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

WINDOW = timedelta(minutes=15)
USER_FAILURES = 5
IP_FAILURES = 10
TOTP_FAILURES = 5
TOTP_LOCK = timedelta(minutes=5)
BASE_BACKOFF = timedelta(minutes=1)
MAX_BACKOFF = timedelta(hours=1)


@dataclass
class LoginThrottle:
    _user: dict[str, deque[datetime]] = field(default_factory=dict[str, deque[datetime]])
    _ip: dict[str, deque[datetime]] = field(default_factory=dict[str, deque[datetime]])
    _totp: dict[str, deque[datetime]] = field(default_factory=dict[str, deque[datetime]])
    _locked_until: dict[str, datetime] = field(default_factory=dict[str, datetime])

    @staticmethod
    def _recent(q: deque[datetime], now: datetime) -> int:
        while q and now - q[0] >= WINDOW:
            q.popleft()
        return len(q)

    def retry_after(self, username: str, ip: str, now: datetime) -> int:
        """Seconds until a login attempt is allowed (0 = now)."""
        key = username.lower()
        until = max(self._locked_until.get(key, now), self._locked_until.get(f"totp:{key}", now))
        wait = max(0.0, (until - now).total_seconds())
        q = self._ip.get(ip)
        if q is not None and self._recent(q, now) >= IP_FAILURES:
            wait = max(wait, (q[0] + WINDOW - now).total_seconds())
        return int(wait + 0.999)

    def password_failed(self, username: str, ip: str, now: datetime) -> bool:
        """Record a failure; ``True`` when this failure started a username lockout."""
        key = username.lower()
        uq = self._user.setdefault(key, deque())
        uq.append(now)
        self._ip.setdefault(ip, deque()).append(now)
        n = self._recent(uq, now)
        if n >= USER_FAILURES:
            backoff = min(BASE_BACKOFF * (2 ** (n - USER_FAILURES)), MAX_BACKOFF)
            self._locked_until[key] = now + backoff
            return True
        return False

    def totp_failed(self, username: str, now: datetime) -> bool:
        key = username.lower()
        q = self._totp.setdefault(key, deque())
        q.append(now)
        if self._recent(q, now) >= TOTP_FAILURES:
            self._locked_until[f"totp:{key}"] = now + TOTP_LOCK
            q.clear()
            return True
        return False

    def succeeded(self, username: str) -> None:
        key = username.lower()
        self._user.pop(key, None)
        self._totp.pop(key, None)
        self._locked_until.pop(key, None)
        self._locked_until.pop(f"totp:{key}", None)


__all__ = ["IP_FAILURES", "TOTP_FAILURES", "USER_FAILURES", "LoginThrottle"]
