"""The one rate limiter (IDENTITY_AND_TRUST §7.2): keyed ``(profile, device, tool)``.

Every door goes through it; none has its own. Sliding one-minute windows in memory.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from beepboop.policy.types import Risk

#: Calls per minute per tier (§7.2).
LIMITS_PER_MINUTE: dict[Risk, int] = {"read": 30, "act": 20, "personal": 10, "critical": 5}
#: Confirms pending per profile (§7.2, §8.2).
MAX_PENDING_CONFIRMS = 3
WINDOW = timedelta(minutes=1)


@dataclass
class RateLimiter:
    """Sliding-window counter. ``exceeded`` is read-only; ``record`` consumes a slot."""

    limits: dict[Risk, int] = field(default_factory=lambda: dict(LIMITS_PER_MINUTE))
    _hits: dict[tuple[str, str, str], deque[datetime]] = field(
        default_factory=dict[tuple[str, str, str], deque[datetime]]
    )

    def _bucket(self, profile_id: str, device: str, tool: str) -> deque[datetime]:
        return self._hits.setdefault((profile_id, device, tool), deque())

    def _prune(self, bucket: deque[datetime], now: datetime) -> None:
        cutoff = now - WINDOW
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def exceeded(self, profile_id: str, device: str, tool: str, risk: Risk, now: datetime) -> bool:
        """True when the next call would exceed the tier's per-minute limit."""
        bucket = self._bucket(profile_id, device, tool)
        self._prune(bucket, now)
        return len(bucket) >= self.limits[risk]

    def record(self, profile_id: str, device: str, tool: str, now: datetime) -> None:
        """Count one call (the registry calls this after an ``allow`` or ``confirm``)."""
        bucket = self._bucket(profile_id, device, tool)
        self._prune(bucket, now)
        bucket.append(now)

    def reset(self) -> None:
        self._hits.clear()
