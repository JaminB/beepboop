"""Profile and grant writes shared by onboarding, People and the inbox.

Each function is a plain ``fn(conn)`` run inside one ``db.write(..., durable=True)``; the
caller then calls ``deps.refresh_policy()`` (core/README.md: policy state is a snapshot).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Literal

from beepboop.db.ids import new_ulid
from beepboop.db.repo import grants, logins, profiles
from beepboop.db.repo.profiles import ProfileRow
from beepboop.policy.grants import Grant, Schedule
from beepboop.policy.ha_services import ServiceTable, load_service_table
from beepboop.policy.presets import apply_preset
from beepboop.policy.types import GUEST_PROFILE_ID, Risk, max_tier

CreatablePreset = Literal["admin", "member", "child"]
QUIET_HOURS = {"start": "07:00", "end": "20:30"}
GUEST_ACCESS_SOURCE = "guest_access"
INBOX_SOURCE = "inbox"
ONBOARDING_SOURCE = "onboarding"


def create_profile(
    conn: sqlite3.Connection,
    name: str,
    preset: CreatablePreset,
    *,
    created_by: str | None,
    locale: str = "en-US",
    quiet_hours: bool = False,
) -> ProfileRow:
    """Insert a profile and apply its preset once (IDENTITY_AND_TRUST §9)."""
    pid = new_ulid()
    rows = apply_preset(preset, pid, new_ulid)
    row = profiles.insert(conn, name, rows.kind, rows.risk_ceiling, locale=locale, profile_id=pid)
    for g in rows.group_ids:
        profiles.add_to_group(conn, pid, g)
    out: list[Grant] = []
    for g in rows.grants:
        if quiet_hours and preset == "child" and g.resource_id == "media.*":
            g = Grant(
                g.id,
                g.subject_type,
                g.subject_id,
                g.resource_type,
                g.resource_id,
                g.max_risk,
                Schedule.from_json(_quiet_json()),
                g.source,
            )
        out.append(g)
    grants.insert_many(conn, out, created_by=created_by)
    return row


def _quiet_json() -> str:
    import json

    return json.dumps({"start": QUIET_HOURS["start"], "end": QUIET_HOURS["end"]})


def admin_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM profiles WHERE kind = 'admin'").fetchone()
    return 0 if row is None else int(row["n"])


def has_login(conn: sqlite3.Connection, profile_id: str) -> bool:
    return logins.get(conn, profile_id) is not None


# ------------------------------------------------------------------ guest access --


def default_guest_until(now: datetime, tz: tzinfo) -> datetime:
    """Tonight 23:30 in the household timezone (or tomorrow's, if that has passed)."""
    local = now.astimezone(tz)
    until = datetime.combine(local.date(), time(23, 30), tzinfo=tz)
    if until <= local:
        until += timedelta(days=1)
    return until


def guest_access_grants(area_ids: list[str], until: datetime) -> list[Grant]:
    """The IDENTITY_AND_TRUST §9 list: tool globs at ``act``, ``list.read``, one grant per area."""
    sched = Schedule.from_json(_expiry_json(until))
    spec: list[tuple[Literal["tool_glob", "ha_area"], str, Risk]] = [
        ("tool_glob", "home.*", "act"),
        ("tool_glob", "media.*", "act"),
        ("tool_glob", "timer.*", "act"),
        ("tool_glob", "list.read", "read"),
    ]
    for a in area_ids:
        spec.append(("ha_area", a, "act"))
    return [
        Grant(new_ulid(), "profile", GUEST_PROFILE_ID, rt, rid, risk, sched, GUEST_ACCESS_SOURCE)
        for rt, rid, risk in spec
    ]


def _expiry_json(until: datetime) -> str:
    import json

    return json.dumps({"expires_at": until.isoformat()})


def end_guest_access(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "DELETE FROM grants WHERE subject_type = 'profile' AND subject_id = ? AND source = ?",
        (GUEST_PROFILE_ID, GUEST_ACCESS_SOURCE),
    ).rowcount


# ------------------------------------------------------------------------ inbox --


def entity_default_risk(domain: str, device_class: str | None, table: ServiceTable) -> Risk:
    """The highest tier any service of the domain reaches (the inbox risk chip, §6.2)."""
    if domain in table.indirect:
        return "critical"
    services = table.direct.get(domain)
    if services is None:
        return "read"  # sensors and other state-only domains
    risk: Risk = "read"
    for r in services.values():
        risk = max_tier(risk, r)
    floor = table.device_class_floor.get(domain, {})
    if device_class is not None and device_class in floor:
        risk = max_tier(risk, floor[device_class])
    return risk


InboxChoice = Literal["everyone", "adults", "only", "ignore"]


@dataclass(frozen=True, slots=True)
class InboxDecision:
    uuid: str
    choice: InboxChoice
    profile_ids: tuple[str, ...] = ()
    risk: Risk | None = None


def apply_inbox(
    conn: sqlite3.Connection,
    decisions: list[InboxDecision],
    *,
    created_by: str | None,
    table: ServiceTable | None = None,
) -> list[str]:
    """Disposition new entities: grant to a group or people at the entity's risk, or hide.

    The risk may be raised; it may be lowered only for indirect services (§6.3). Returns
    the ids of the grants written (for *Undo*).
    """
    from beepboop.db.repo import ha_entities

    tab = table or load_service_table()
    written: list[str] = []
    for d in decisions:
        ent = ha_entities.get(conn, d.uuid)
        if ent is None:
            continue
        default = entity_default_risk(ent.domain, ent.device_class, tab)
        risk = default
        if d.risk is not None:
            lowered = max_tier(d.risk, default) != d.risk
            if lowered and ent.domain not in tab.indirect:
                raise ValueError(f"risk_not_lowerable:{d.uuid}")
            risk = d.risk
        if d.choice == "ignore":
            ha_entities.set_status(conn, d.uuid, "hidden")
            continue
        declared = risk if ent.domain in tab.indirect else None
        ha_entities.set_status(conn, d.uuid, "active", declared)
        subjects: list[tuple[Literal["profile", "group"], str]]
        if d.choice == "everyone":
            subjects = [("group", "household")]
        elif d.choice == "adults":
            subjects = [("group", "adults")]
        else:
            subjects = [("profile", pid) for pid in d.profile_ids]
        for st, sid in subjects:
            gid = new_ulid()
            grants.insert(
                conn,
                Grant(gid, st, sid, "ha_entity", d.uuid, risk, None, INBOX_SOURCE),
                created_by=created_by,
            )
            written.append(gid)
    return written


def undo_inbox(conn: sqlite3.Connection, uuids: list[str]) -> int:
    """Put entities back in the inbox and drop the grants the inbox wrote for them."""
    from beepboop.db.repo import ha_entities

    n = 0
    for uuid in uuids:
        n += conn.execute(
            "DELETE FROM grants WHERE resource_type = 'ha_entity' AND resource_id = ?"
            " AND source = ?",
            (uuid, INBOX_SOURCE),
        ).rowcount
        ha_entities.set_status(conn, uuid, "new")
    return n


def recommended_home(
    conn: sqlite3.Connection, *, created_by: str | None, table: ServiceTable | None = None
) -> int:
    """Onboarding step 4 "recommended": household ``act`` per area, critical to adults."""
    from beepboop.db.repo import ha_entities

    tab = table or load_service_table()
    areas: set[str] = set()
    n = 0
    for ent in ha_entities.list_(conn, "new"):
        risk = entity_default_risk(ent.domain, ent.device_class, tab)
        if risk == "critical":
            decision = InboxDecision(ent.uuid, "adults")
            n += len(apply_inbox(conn, [decision], created_by=created_by, table=tab))
            continue
        ha_entities.set_status(conn, ent.uuid, "active")
        if ent.area_id is not None:
            areas.add(ent.area_id)
        else:
            grants.insert(
                conn,
                Grant(
                    new_ulid(),
                    "group",
                    "household",
                    "ha_entity",
                    ent.uuid,
                    risk,
                    None,
                    ONBOARDING_SOURCE,
                ),
                created_by=created_by,
            )
            n += 1
    for area in sorted(areas):
        grants.insert(
            conn,
            Grant(
                new_ulid(), "group", "household", "ha_area", area, "act", None, ONBOARDING_SOURCE
            ),
            created_by=created_by,
        )
        n += 1
    return n


__all__ = [
    "GUEST_ACCESS_SOURCE",
    "INBOX_SOURCE",
    "CreatablePreset",
    "InboxChoice",
    "InboxDecision",
    "admin_count",
    "apply_inbox",
    "create_profile",
    "default_guest_until",
    "end_guest_access",
    "entity_default_risk",
    "guest_access_grants",
    "has_login",
    "recommended_home",
    "undo_inbox",
]
