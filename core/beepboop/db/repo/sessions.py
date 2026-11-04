"""``sessions`` (DATA_AND_PRIVACY §6.1, WEB_UI §6.2). Durable.

The cookie token never reaches the database: callers pass ``sha256(token)``.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, cast

from beepboop.db.ids import now_ms
from beepboop.db.repo._rows import opt_str

SessionKind = Literal["web", "claim"]
IDLE_TIMEOUT = timedelta(days=30)
ABSOLUTE_TIMEOUT = timedelta(days=90)
CLAIM_TIMEOUT = timedelta(minutes=15)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionRow:
    id_hash: str
    profile_id: str
    kind: SessionKind
    created_at: int
    last_seen_at: int
    ua: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SessionRow:
        return cls(
            id_hash=str(row["id_hash"]),
            profile_id=str(row["profile_id"]),
            kind=cast(SessionKind, row["kind"]),
            created_at=int(row["created_at"]),
            last_seen_at=int(row["last_seen_at"]),
            ua=opt_str(row, "ua"),
        )

    def is_valid(self, now: int) -> bool:
        if self.kind == "claim":
            return now - self.created_at < CLAIM_TIMEOUT.total_seconds() * 1000
        idle_ok = now - self.last_seen_at < IDLE_TIMEOUT.total_seconds() * 1000
        absolute_ok = now - self.created_at < ABSOLUTE_TIMEOUT.total_seconds() * 1000
        return idle_ok and absolute_ok


def create(
    conn: sqlite3.Connection,
    id_hash: str,
    profile_id: str,
    *,
    kind: SessionKind = "web",
    ua: str | None = None,
) -> SessionRow:
    now = now_ms()
    conn.execute(
        "INSERT INTO sessions (id_hash, profile_id, kind, created_at, last_seen_at, ua)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (id_hash, profile_id, kind, now, now, ua),
    )
    return SessionRow(id_hash, profile_id, kind, now, now, ua)


def get(conn: sqlite3.Connection, id_hash: str) -> SessionRow | None:
    row = conn.execute("SELECT * FROM sessions WHERE id_hash = ?", (id_hash,)).fetchone()
    return None if row is None else SessionRow.from_row(row)


def touch(conn: sqlite3.Connection, id_hash: str, now: int | None = None) -> None:
    """Renew the idle timeout on use."""
    conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE id_hash = ?", (now or now_ms(), id_hash)
    )


def delete(conn: sqlite3.Connection, id_hash: str) -> bool:
    return conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,)).rowcount > 0


def delete_for_profile(conn: sqlite3.Connection, profile_id: str) -> int:
    """ "Sign out everywhere"."""
    return conn.execute("DELETE FROM sessions WHERE profile_id = ?", (profile_id,)).rowcount


def purge_expired(conn: sqlite3.Connection, now: int) -> int:
    idle = int(IDLE_TIMEOUT.total_seconds() * 1000)
    absolute = int(ABSOLUTE_TIMEOUT.total_seconds() * 1000)
    claim = int(CLAIM_TIMEOUT.total_seconds() * 1000)
    return conn.execute(
        "DELETE FROM sessions WHERE (kind = 'web' AND (? - last_seen_at >= ? OR ? - created_at >="
        " ?))"
        " OR (kind = 'claim' AND ? - created_at >= ?)",
        (now, idle, now, absolute, now, claim),
    ).rowcount
