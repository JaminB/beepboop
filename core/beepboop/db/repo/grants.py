"""``grants`` (DATA_AND_PRIVACY §6.1, IDENTITY_AND_TRUST §5). Durable.

Every change here must be followed by a ``PolicyState`` rebuild (``policy_view``).
"""

from __future__ import annotations

import sqlite3
from typing import cast

from beepboop.db.ids import new_ulid, now_ms
from beepboop.db.repo._rows import opt_str
from beepboop.policy.grants import Grant, ResourceType, Schedule, SubjectType
from beepboop.policy.types import Risk


def _to_grant(row: sqlite3.Row) -> Grant:
    return Grant(
        id=str(row["id"]),
        subject_type=cast(SubjectType, row["subject_type"]),
        subject_id=str(row["subject_id"]),
        resource_type=cast(ResourceType, row["resource_type"]),
        resource_id=str(row["resource_id"]),
        max_risk=cast(Risk, row["max_risk"]),
        schedule=Schedule.from_json(opt_str(row, "schedule_json")),
        source=str(row["source"]),
    )


def insert(conn: sqlite3.Connection, grant: Grant, *, created_by: str | None = None) -> Grant:
    """Insert (or replace on the unique subject/resource key, keeping the new id)."""
    conn.execute(
        "INSERT INTO grants (id, subject_type, subject_id, resource_type, resource_id, max_risk,"
        " schedule_json, source, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(subject_type, subject_id, resource_type, resource_id) DO UPDATE SET"
        " id = excluded.id, max_risk = excluded.max_risk, schedule_json = excluded.schedule_json,"
        " source = excluded.source, created_by = excluded.created_by, created_at ="
        " excluded.created_at",
        (
            grant.id or new_ulid(),
            grant.subject_type,
            grant.subject_id,
            grant.resource_type,
            grant.resource_id,
            grant.max_risk,
            None if grant.schedule is None else grant.schedule.to_json(),
            grant.source,
            created_by,
            now_ms(),
        ),
    )
    return grant


def insert_many(
    conn: sqlite3.Connection,
    grants: tuple[Grant, ...] | list[Grant],
    *,
    created_by: str | None = None,
) -> None:
    for g in grants:
        insert(conn, g, created_by=created_by)


def get(conn: sqlite3.Connection, grant_id: str) -> Grant | None:
    row = conn.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
    return None if row is None else _to_grant(row)


def delete(conn: sqlite3.Connection, grant_id: str) -> bool:
    return conn.execute("DELETE FROM grants WHERE id = ?", (grant_id,)).rowcount > 0


def list_all(conn: sqlite3.Connection) -> list[Grant]:
    rows = conn.execute("SELECT * FROM grants ORDER BY created_at, id").fetchall()
    return [_to_grant(r) for r in rows]


def list_for_subject(
    conn: sqlite3.Connection, subject_type: SubjectType, subject_id: str
) -> list[Grant]:
    rows = conn.execute(
        "SELECT * FROM grants WHERE subject_type = ? AND subject_id = ? ORDER BY created_at, id",
        (subject_type, subject_id),
    ).fetchall()
    return [_to_grant(r) for r in rows]


def delete_for_subject(conn: sqlite3.Connection, subject_type: SubjectType, subject_id: str) -> int:
    return conn.execute(
        "DELETE FROM grants WHERE subject_type = ? AND subject_id = ?", (subject_type, subject_id)
    ).rowcount


def delete_for_resource(
    conn: sqlite3.Connection, resource_type: ResourceType, resource_id: str
) -> int:
    return conn.execute(
        "DELETE FROM grants WHERE resource_type = ? AND resource_id = ?",
        (resource_type, resource_id),
    ).rowcount


def purge_expired(conn: sqlite3.Connection, now_iso: str) -> int:
    """Delete grants whose ``schedule.expires_at`` is in the past (nightly, §5.1).

    ``now_iso`` is an ISO-8601 UTC string; ``expires_at`` values are compared after
    normalising to UTC in Python because SQLite does not parse offsets.
    """
    from datetime import datetime

    now = datetime.fromisoformat(now_iso)
    expired: list[str] = []
    for g in list_all(conn):
        if (
            g.schedule is not None
            and g.schedule.expires_at is not None
            and g.schedule.expires_at <= now
        ):
            expired.append(g.id)
    for gid in expired:
        delete(conn, gid)
    return len(expired)
