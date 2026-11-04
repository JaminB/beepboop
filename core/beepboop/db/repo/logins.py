"""``logins`` and ``invites`` (DATA_AND_PRIVACY §6.1, WEB_UI §6.1). Durable.

Password hashing (argon2id) is the API builder's; this repo stores the encoded string.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from beepboop.db.ids import now_ms
from beepboop.db.repo._rows import opt_int, opt_str


@dataclass(frozen=True, slots=True)
class LoginRow:
    profile_id: str
    username: str
    pw_hash: str
    totp_secret_enc: bytes | None = None
    totp_last_step: int | None = None
    recovery_hashes_json: str | None = None
    failed_attempts: int = 0
    locked_until: int | None = None
    created_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> LoginRow:
        return cls(
            profile_id=str(row["profile_id"]),
            username=str(row["username"]),
            pw_hash=str(row["pw_hash"]),
            totp_secret_enc=None
            if row["totp_secret_enc"] is None
            else bytes(row["totp_secret_enc"]),
            totp_last_step=opt_int(row, "totp_last_step"),
            recovery_hashes_json=opt_str(row, "recovery_hashes_json"),
            failed_attempts=int(row["failed_attempts"]),
            locked_until=opt_int(row, "locked_until"),
            created_at=int(row["created_at"]),
        )


def get(conn: sqlite3.Connection, profile_id: str) -> LoginRow | None:
    row = conn.execute("SELECT * FROM logins WHERE profile_id = ?", (profile_id,)).fetchone()
    return None if row is None else LoginRow.from_row(row)


def by_username(conn: sqlite3.Connection, username: str) -> LoginRow | None:
    row = conn.execute("SELECT * FROM logins WHERE username = ?", (username,)).fetchone()
    return None if row is None else LoginRow.from_row(row)


def insert(conn: sqlite3.Connection, profile_id: str, username: str, pw_hash: str) -> LoginRow:
    created = now_ms()
    conn.execute(
        "INSERT INTO logins (profile_id, username, pw_hash, created_at) VALUES (?, ?, ?, ?)",
        (profile_id, username, pw_hash, created),
    )
    return LoginRow(profile_id, username, pw_hash, created_at=created)


def update(conn: sqlite3.Connection, row: LoginRow) -> None:
    conn.execute(
        "UPDATE logins SET username = ?, pw_hash = ?, totp_secret_enc = ?, totp_last_step = ?,"
        " recovery_hashes_json = ?, failed_attempts = ?, locked_until = ? WHERE profile_id = ?",
        (
            row.username,
            row.pw_hash,
            row.totp_secret_enc,
            row.totp_last_step,
            row.recovery_hashes_json,
            row.failed_attempts,
            row.locked_until,
            row.profile_id,
        ),
    )


def delete(conn: sqlite3.Connection, profile_id: str) -> bool:
    return conn.execute("DELETE FROM logins WHERE profile_id = ?", (profile_id,)).rowcount > 0


def profiles_with_login(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT profile_id FROM logins").fetchall()
    return frozenset(str(r["profile_id"]) for r in rows)


# ------------------------------------------------------------------ invites --


def insert_invite(
    conn: sqlite3.Connection, token_hash: str, profile_id: str, expires_at: int
) -> None:
    conn.execute(
        "INSERT INTO invites (token_hash, profile_id, expires_at) VALUES (?, ?, ?)",
        (token_hash, profile_id, expires_at),
    )


def use_invite(conn: sqlite3.Connection, token_hash: str, now: int) -> str | None:
    """Mark the invite used and return its ``profile_id``; ``None`` if unknown, used or expired."""
    row = conn.execute(
        "UPDATE invites SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?"
        " RETURNING profile_id",
        (now, token_hash, now),
    ).fetchone()
    return None if row is None else str(row["profile_id"])


def purge_invites(conn: sqlite3.Connection, now: int) -> int:
    return conn.execute(
        "DELETE FROM invites WHERE used_at IS NOT NULL OR expires_at <= ?", (now,)
    ).rowcount


def list_invites(conn: sqlite3.Connection, now: int) -> list[tuple[str, str, int]]:
    """Live invites as ``(token_hash, profile_id, expires_at)``, soonest expiry first."""
    rows = conn.execute(
        "SELECT token_hash, profile_id, expires_at FROM invites"
        " WHERE used_at IS NULL AND expires_at > ? ORDER BY expires_at",
        (now,),
    ).fetchall()
    return [(str(r["token_hash"]), str(r["profile_id"]), int(r["expires_at"])) for r in rows]


def peek_invite(conn: sqlite3.Connection, token_hash: str, now: int) -> str | None:
    """The invite's ``profile_id`` if it is live, without using it."""
    row = conn.execute(
        "SELECT profile_id FROM invites WHERE token_hash = ? AND used_at IS NULL"
        " AND expires_at > ?",
        (token_hash, now),
    ).fetchone()
    return None if row is None else str(row["profile_id"])


def delete_invite(conn: sqlite3.Connection, token_hash: str) -> bool:
    return conn.execute("DELETE FROM invites WHERE token_hash = ?", (token_hash,)).rowcount > 0
