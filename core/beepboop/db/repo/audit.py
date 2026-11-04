"""``audit``: the security and accountability log (DATA_AND_PRIVACY §6.1, IDENTITY_AND_TRUST §11).
Durable. Rows are never deleted early; actors are pseudonymised on profile delete.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, cast

from beepboop.db.ids import new_ulid, now_ms
from beepboop.db.repo._rows import dump_json, opt_str

AuditVia = Literal["voice", "web", "oauth", "pat", "system"]
DELETED_USER = "deleted user"
DEFAULT_RETENTION = timedelta(days=365)


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: str
    at: int
    actor_id: str | None
    actor_name: str
    via: AuditVia
    action: str
    target: str | None = None
    details: dict[str, object] | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AuditRow:
        details_raw = opt_str(row, "details_json")
        return cls(
            id=str(row["id"]),
            at=int(row["at"]),
            actor_id=opt_str(row, "actor_id"),
            actor_name=str(row["actor_name"]),
            via=cast(AuditVia, row["via"]),
            action=str(row["action"]),
            target=opt_str(row, "target"),
            details=None
            if details_raw is None
            else cast(dict[str, object], json.loads(details_raw)),
        )


def record(
    conn: sqlite3.Connection,
    *,
    actor_id: str | None,
    actor_name: str,
    via: AuditVia,
    action: str,
    target: str | None = None,
    details: dict[str, object] | None = None,
    at: int | None = None,
) -> AuditRow:
    row = AuditRow(new_ulid(), at or now_ms(), actor_id, actor_name, via, action, target, details)
    conn.execute(
        "INSERT INTO audit (id, at, actor_id, actor_name, via, action, target, details_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row.id,
            row.at,
            row.actor_id,
            row.actor_name,
            row.via,
            row.action,
            row.target,
            None if details is None else dump_json(details),
        ),
    )
    return row


def list_(
    conn: sqlite3.Connection, *, limit: int = 100, before: int | None = None
) -> list[AuditRow]:
    rows = conn.execute(
        "SELECT * FROM audit WHERE (? IS NULL OR at < ?) ORDER BY at DESC LIMIT ?",
        (before, before, limit),
    ).fetchall()
    return [AuditRow.from_row(r) for r in rows]


def pseudonymise_actor(conn: sqlite3.Connection, profile_id: str) -> int:
    """§9.3 step 8: keep the rows, drop the name."""
    return conn.execute(
        "UPDATE audit SET actor_id = NULL, actor_name = ? WHERE actor_id = ?",
        (DELETED_USER, profile_id),
    ).rowcount


def purge_older_than(conn: sqlite3.Connection, cutoff: int) -> int:
    return conn.execute("DELETE FROM audit WHERE at < ?", (cutoff,)).rowcount
