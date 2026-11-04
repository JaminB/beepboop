"""``conversation``: the 90 s window per (profile, device) (DATA_AND_PRIVACY §6.1). Trace writes.

Taint is conversation state (IDENTITY_AND_TRUST §3.2); this row is authoritative.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from beepboop.db.repo._rows import dump_json

CONVERSATION_WINDOW = timedelta(seconds=90)


@dataclass(frozen=True, slots=True)
class ConversationRow:
    profile_id: str
    device: str
    messages: list[dict[str, object]]
    tainted: bool
    expires_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ConversationRow:
        return cls(
            profile_id=str(row["profile_id"]),
            device=str(row["device"]),
            messages=cast(list[dict[str, object]], json.loads(row["messages_json"])),
            tainted=bool(row["tainted"]),
            expires_at=int(row["expires_at"]),
        )


def get(conn: sqlite3.Connection, profile_id: str, device: str, now: int) -> ConversationRow | None:
    """The live window, or ``None`` (an expired row is deleted on read)."""
    row = conn.execute(
        "SELECT * FROM conversation WHERE profile_id = ? AND device = ?", (profile_id, device)
    ).fetchone()
    if row is None:
        return None
    conv = ConversationRow.from_row(row)
    if conv.expires_at <= now:
        delete(conn, profile_id, device)
        return None
    return conv


def put(conn: sqlite3.Connection, conv: ConversationRow) -> None:
    conn.execute(
        "INSERT INTO conversation (profile_id, device, messages_json, tainted, expires_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(profile_id, device) DO UPDATE SET"
        " messages_json = excluded.messages_json, tainted = excluded.tainted,"
        " expires_at = excluded.expires_at",
        (
            conv.profile_id,
            conv.device,
            dump_json(conv.messages),
            int(conv.tainted),
            conv.expires_at,
        ),
    )


def set_tainted(conn: sqlite3.Connection, profile_id: str, device: str) -> None:
    conn.execute(
        "UPDATE conversation SET tainted = 1 WHERE profile_id = ? AND device = ?",
        (profile_id, device),
    )


def delete(conn: sqlite3.Connection, profile_id: str, device: str) -> bool:
    cur = conn.execute(
        "DELETE FROM conversation WHERE profile_id = ? AND device = ?", (profile_id, device)
    )
    return cur.rowcount > 0


def purge_expired(conn: sqlite3.Connection, now: int) -> int:
    return conn.execute("DELETE FROM conversation WHERE expires_at <= ?", (now,)).rowcount
