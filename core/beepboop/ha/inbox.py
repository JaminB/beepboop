"""Mirror the entity registry into ``ha_entities``: the new-device inbox (HOME_ASSISTANT §6.1,
IDENTITY_AND_TRUST §6.2).

New entities are **default-deny**: they land as ``status = 'new'``, which no ``ha_area`` /
``ha_config_entry`` grant covers and the resolver never offers, until an admin
dispositions them in the inbox. Two rules refine the insert:

- An entity of an **indirect** service class (scripts, scenes, automations, helpers,
  buttons, selects, template switches; HOME_ASSISTANT §9) starts with
  ``declared_risk = 'critical'``.
- An entity whose config entry already carries an ``ha_config_entry`` grant (a personal
  account, §7.2) inherits it and skips the inbox: it lands ``active``.

Renames keep the UUID, so grants survive; an entity that disappears is marked
``removed_at`` and purged with its grants after 30 days by ``db.retention``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from beepboop.db.repo import grants as grants_repo
from beepboop.db.repo import ha_entities
from beepboop.db.repo.ha_entities import EntityRow, EntityStatus
from beepboop.ha.index import IndexedEntity, RegistryIndex
from beepboop.policy.ha_services import ServiceTable
from beepboop.policy.types import Risk


@dataclass(frozen=True, slots=True)
class SyncResult:
    inserted: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.updated or self.removed)


def is_indirect(item: IndexedEntity, services: ServiceTable) -> bool:
    """Indirect service class: an indirect domain, or a template switch (§9)."""
    return item.ref.domain in services.indirect or item.is_template_switch


def default_declared_risk(item: IndexedEntity, services: ServiceTable) -> Risk | None:
    return "critical" if is_indirect(item, services) else None


def to_row(item: IndexedEntity, status: EntityStatus, declared_risk: Risk | None) -> EntityRow:
    ref = item.ref
    return EntityRow(
        uuid=ref.uuid,
        entity_id=ref.entity_id,
        domain=ref.domain,
        status=status,
        device_class=ref.device_class,
        area_id=ref.area_id,
        device_id=ref.device_id,
        config_entry_id=ref.config_entry_id,
        friendly_name=ref.name,
        aliases=ref.aliases,
        declared_risk=declared_risk,
    )


def _registry_fields(row: EntityRow) -> tuple[object, ...]:
    return (
        row.entity_id,
        row.domain,
        row.device_class,
        row.area_id,
        row.device_id,
        row.config_entry_id,
        row.friendly_name,
        tuple(row.aliases),
    )


def sync_entities(
    conn: sqlite3.Connection, index: RegistryIndex, services: ServiceTable
) -> SyncResult:
    """One write transaction: insert new entities into the inbox, refresh changed ones,
    mark vanished ones removed. Never overwrites ``status`` or ``declared_risk``."""
    existing = {row.uuid: row for row in ha_entities.list_(conn)}
    granted_entries = {
        g.resource_id for g in grants_repo.list_all(conn) if g.resource_type == "ha_config_entry"
    }
    inserted: list[str] = []
    updated: list[str] = []
    for uuid in sorted(index.entities):
        item = index.entities[uuid]
        old = existing.get(uuid)
        if old is None:
            entry = item.ref.config_entry_id
            status: EntityStatus = "active" if entry and entry in granted_entries else "new"
            ha_entities.upsert(conn, to_row(item, status, default_declared_risk(item, services)))
            inserted.append(uuid)
            continue
        fresh = to_row(item, old.status, old.declared_risk)
        if _registry_fields(fresh) != _registry_fields(old) or old.removed_at is not None:
            ha_entities.upsert(conn, fresh)
            updated.append(uuid)
    removed: list[str] = []
    for uuid, row in sorted(existing.items()):
        if uuid not in index.entities and row.removed_at is None:
            ha_entities.mark_removed(conn, uuid)
            removed.append(uuid)
    return SyncResult(tuple(inserted), tuple(updated), tuple(removed))


__all__ = ["SyncResult", "default_declared_risk", "is_indirect", "sync_entities", "to_row"]
