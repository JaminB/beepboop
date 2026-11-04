"""Ids and clocks shared by the repositories: ULID ids, Unix-millisecond timestamps."""

from __future__ import annotations

from datetime import UTC, datetime

from beepboop_protocol import new_ulid

__all__ = ["from_ms", "new_ulid", "now_ms", "to_ms"]


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def to_ms(when: datetime) -> int:
    """Aware ``datetime`` → Unix ms. Naive datetimes are a bug (CLAUDE.md §5)."""
    if when.tzinfo is None:
        msg = "datetime must be timezone-aware"
        raise ValueError(msg)
    return int(when.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)
