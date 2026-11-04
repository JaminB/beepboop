"""``ha_entities``: the entity-registry mirror and new-device inbox (DATA_AND_PRIVACY §6.1,
IDENTITY_AND_TRUST §6). Durable (it feeds grants).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, cast

from beepboop.db.ids import now_ms
from beepboop.db.repo._rows import opt_str
from beepboop.policy.types import EntityInfo, Risk

EntityStatus = Literal["new", "active", "hidden"]
REMOVED_KEEP = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class EntityRow:
    uuid: str
    entity_id: str
    domain: str
    status: EntityStatus = "new"
    device_class: str | None = None
    area_id: str | None = None
    device_id: str | None = None
    config_entry_id: str | None = None
    friendly_name: str | None = None
    aliases: tuple[str, ...] = ()
    declared_risk: Risk | None = None
    first_seen_at: int = 0
    last_seen_at: int = 0
    removed_at: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> EntityRow:
        aliases_raw = opt_str(row, "aliases_json")
        aliases: tuple[str, ...] = ()
        if aliases_raw:
            aliases = tuple(str(a) for a in cast(list[object], json.loads(aliases_raw)))
        return cls(
            uuid=str(row["uuid"]),
            entity_id=str(row["entity_id"]),
            domain=str(row["domain"]),
            status=cast(EntityStatus, row["status"]),
            device_class=opt_str(row, "device_class"),
            area_id=opt_str(row, "area_id"),
            device_id=opt_str(row, "device_id"),
            config_entry_id=opt_str(row, "config_entry_id"),
            friendly_name=opt_str(row, "friendly_name"),
            aliases=aliases,
            declared_risk=cast(Risk | None, row["declared_risk"]),
            first_seen_at=int(row["first_seen_at"]),
            last_seen_at=int(row["last_seen_at"]),
            removed_at=None if row["removed_at"] is None else int(row["removed_at"]),
        )

    def to_policy(self) -> EntityInfo:
        return EntityInfo(
            self.uuid, self.status, self.area_id, self.config_entry_id, self.declared_risk
        )


def upsert(conn: sqlite3.Connection, row: EntityRow) -> None:
    """Insert a new entity as ``status='new'`` (the inbox) or refresh an existing one's
    registry fields; ``status`` and ``declared_risk`` are never overwritten here."""
    now = now_ms()
    conn.execute(
        "INSERT INTO ha_entities (uuid, entity_id, domain, device_class, area_id, device_id,"
        " config_entry_id, friendly_name, aliases_json, status, declared_risk, first_seen_at,"
        " last_seen_at, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)"
        " ON CONFLICT(uuid) DO UPDATE SET entity_id = excluded.entity_id, domain = excluded.domain,"
        " device_class = excluded.device_class, area_id = excluded.area_id,"
        " device_id = excluded.device_id, config_entry_id = excluded.config_entry_id,"
        " friendly_name = excluded.friendly_name, aliases_json = excluded.aliases_json,"
        " last_seen_at = excluded.last_seen_at, removed_at = NULL",
        (
            row.uuid,
            row.entity_id,
            row.domain,
            row.device_class,
            row.area_id,
            row.device_id,
            row.config_entry_id,
            row.friendly_name,
            json.dumps(list(row.aliases)),
            row.status,
            row.declared_risk,
            now,
            now,
        ),
    )


def get(conn: sqlite3.Connection, uuid: str) -> EntityRow | None:
    row = conn.execute("SELECT * FROM ha_entities WHERE uuid = ?", (uuid,)).fetchone()
    return None if row is None else EntityRow.from_row(row)


def list_(conn: sqlite3.Connection, status: EntityStatus | None = None) -> list[EntityRow]:
    if status is None:
        rows = conn.execute("SELECT * FROM ha_entities ORDER BY entity_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ha_entities WHERE status = ? ORDER BY entity_id", (status,)
        ).fetchall()
    return [EntityRow.from_row(r) for r in rows]


def set_status(
    conn: sqlite3.Connection, uuid: str, status: EntityStatus, declared_risk: Risk | None = None
) -> None:
    """Inbox disposition (IDENTITY_AND_TRUST §6.2). ``declared_risk`` only for indirect services."""
    conn.execute(
        "UPDATE ha_entities SET status = ?, declared_risk = COALESCE(?, declared_risk) WHERE uuid"
        " ="
        " ?",
        (status, declared_risk, uuid),
    )


def mark_removed(conn: sqlite3.Connection, uuid: str) -> None:
    conn.execute(
        "UPDATE ha_entities SET removed_at = ? WHERE uuid = ? AND removed_at IS NULL",
        (now_ms(), uuid),
    )


def purge_removed(conn: sqlite3.Connection, now: int) -> int:
    """Delete rows (and their grants) removed more than 30 days ago (§6.1)."""
    cutoff = now - int(REMOVED_KEEP.total_seconds() * 1000)
    uuids = [
        str(r["uuid"])
        for r in conn.execute("SELECT uuid FROM ha_entities WHERE removed_at <= ?", (cutoff,))
    ]
    for uuid in uuids:
        conn.execute(
            "DELETE FROM grants WHERE resource_type = 'ha_entity' AND resource_id = ?", (uuid,)
        )
        conn.execute("DELETE FROM ha_entities WHERE uuid = ?", (uuid,))
    return len(uuids)
