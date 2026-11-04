"""``integrations``: a module's connection for a scope (DATA_AND_PRIVACY §6.1, MODULES §4.3).
Durable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal, cast

from beepboop.db.ids import new_ulid, now_ms
from beepboop.db.repo._rows import dump_json, json_obj, opt_str

Scope = Literal["household", "profile"]
IntegrationStatus = Literal["ok", "reauth", "error", "off"]


@dataclass(frozen=True, slots=True)
class IntegrationRow:
    id: str
    module_id: str
    scope: Scope
    profile_id: str | None = None
    settings: dict[str, object] = field(default_factory=dict[str, object])
    ha_config_entry_id: str | None = None
    status: IntegrationStatus = "off"
    last_error: str | None = None
    updated_at: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> IntegrationRow:
        return cls(
            id=str(row["id"]),
            module_id=str(row["module_id"]),
            scope=cast(Scope, row["scope"]),
            profile_id=opt_str(row, "profile_id"),
            settings=json_obj(str(row["settings_json"])),
            ha_config_entry_id=opt_str(row, "ha_config_entry_id"),
            status=cast(IntegrationStatus, row["status"]),
            last_error=opt_str(row, "last_error"),
            updated_at=int(row["updated_at"]),
        )


def get(conn: sqlite3.Connection, module_id: str, profile_id: str | None) -> IntegrationRow | None:
    row = conn.execute(
        "SELECT * FROM integrations WHERE module_id = ? AND COALESCE(profile_id, '') = ?",
        (module_id, profile_id or ""),
    ).fetchone()
    return None if row is None else IntegrationRow.from_row(row)


def list_all(conn: sqlite3.Connection) -> list[IntegrationRow]:
    """Every integration row (core's settings cache for ``ctx.settings``)."""
    rows = conn.execute(
        "SELECT * FROM integrations ORDER BY module_id, COALESCE(profile_id, '')"
    ).fetchall()
    return [IntegrationRow.from_row(r) for r in rows]


def list_for_module(conn: sqlite3.Connection, module_id: str) -> list[IntegrationRow]:
    rows = conn.execute(
        "SELECT * FROM integrations WHERE module_id = ? ORDER BY COALESCE(profile_id, '')",
        (module_id,),
    ).fetchall()
    return [IntegrationRow.from_row(r) for r in rows]


def upsert(
    conn: sqlite3.Connection,
    module_id: str,
    scope: Scope,
    profile_id: str | None,
    *,
    settings: dict[str, object] | None = None,
    status: IntegrationStatus | None = None,
    last_error: str | None = None,
    ha_config_entry_id: str | None = None,
) -> IntegrationRow:
    """Create or update the row for ``(module_id, profile_id)``. ``None`` keeps a column."""
    existing = get(conn, module_id, profile_id)
    row = IntegrationRow(
        id=existing.id if existing else new_ulid(),
        module_id=module_id,
        scope=scope,
        profile_id=profile_id,
        settings=settings if settings is not None else (existing.settings if existing else {}),
        ha_config_entry_id=ha_config_entry_id
        if ha_config_entry_id is not None
        else (existing.ha_config_entry_id if existing else None),
        status=status if status is not None else (existing.status if existing else "off"),
        last_error=last_error,
        updated_at=now_ms(),
    )
    conn.execute(
        "INSERT INTO integrations (id, module_id, scope, profile_id, settings_json,"
        " ha_config_entry_id, status, last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(module_id, COALESCE(profile_id, '')) DO UPDATE SET"
        " settings_json = excluded.settings_json, ha_config_entry_id = excluded.ha_config_entry_id,"
        " status = excluded.status, last_error = excluded.last_error, updated_at ="
        " excluded.updated_at",
        (
            row.id,
            row.module_id,
            row.scope,
            row.profile_id,
            dump_json(row.settings),
            row.ha_config_entry_id,
            row.status,
            row.last_error,
            row.updated_at,
        ),
    )
    return row


def delete(conn: sqlite3.Connection, module_id: str, profile_id: str | None) -> bool:
    cur = conn.execute(
        "DELETE FROM integrations WHERE module_id = ? AND COALESCE(profile_id, '') = ?",
        (module_id, profile_id or ""),
    )
    return cur.rowcount > 0
