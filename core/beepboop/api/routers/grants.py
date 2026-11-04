"""``/api/grants`` and ``/api/guest-access`` (WEB_UI §5.3, §5.5; IDENTITY_AND_TRUST §5, §9).

Every Access toggle writes or deletes one ``grants`` row. Guest access is ordinary
scheduled grants for the built-in ``guest`` profile, nothing more.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from beepboop.api.deps import Admin, Deps, Session, record_audit
from beepboop.api.people import (
    GUEST_ACCESS_SOURCE,
    default_guest_until,
    end_guest_access,
    guest_access_grants,
)
from beepboop.api.problem import ApiError
from beepboop.api.routers.profiles import load_visible
from beepboop.db.ids import new_ulid
from beepboop.db.repo import grants, household, profiles
from beepboop.policy.grants import Grant, Schedule, ScheduleError
from beepboop.policy.types import GUEST_PROFILE_ID, Risk

router = APIRouter(prefix="/api/grants", tags=["grants"])
guest_router = APIRouter(prefix="/api/guest-access", tags=["guest_access"])

GROUPS = ("household", "adults", "children")


class GrantCreate(BaseModel):
    subject_type: Literal["profile", "group"]
    subject_id: str
    resource_type: Literal["tool_glob", "ha_entity", "ha_area", "ha_config_entry"]
    resource_id: str = Field(min_length=1, max_length=200)
    max_risk: Risk
    schedule: dict[str, object] | None = None


class GrantOut(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    resource_type: str
    resource_id: str
    max_risk: str
    schedule: str | None
    source: str


def grant_out(g: Grant) -> GrantOut:
    return GrantOut(
        id=g.id,
        subject_type=g.subject_type,
        subject_id=g.subject_id,
        resource_type=g.resource_type,
        resource_id=g.resource_id,
        max_risk=g.max_risk,
        schedule=None if g.schedule is None else g.schedule.to_json(),
        source=g.source,
    )


@router.post("", operation_id="grants_create", status_code=201)
async def grants_create(body: GrantCreate, admin: Admin, deps: Deps) -> GrantOut:
    import json

    if body.subject_type == "group" and body.subject_id not in GROUPS:
        raise ApiError(422, "unknown_group", fields={"subject_id": "unknown_group"})
    if body.subject_type == "profile" and (
        await deps.db.read(lambda c: profiles.get(c, body.subject_id)) is None
    ):
        raise ApiError(422, "unknown_profile", fields={"subject_id": "unknown_profile"})
    try:
        schedule = None if body.schedule is None else Schedule.from_json(json.dumps(body.schedule))
    except ScheduleError as exc:
        raise ApiError(422, "bad_schedule", fields={"schedule": str(exc)}) from exc
    g = Grant(
        new_ulid(),
        body.subject_type,
        body.subject_id,
        body.resource_type,
        body.resource_id,
        body.max_risk,
        schedule,
        "web",
    )
    await deps.db.write(lambda c: grants.insert(c, g, created_by=admin.profile.id), durable=True)
    await deps.refresh_policy()
    await record_audit(
        deps,
        admin.profile,
        "grant.created",
        target=g.id,
        details={
            "subject": f"{g.subject_type}:{g.subject_id}",
            "resource": f"{g.resource_type}:{g.resource_id}",
            "max_risk": g.max_risk,
        },
    )
    stored = await deps.db.read(
        lambda c: c.execute(
            "SELECT id FROM grants WHERE subject_type = ? AND subject_id = ? AND resource_type = ?"
            " AND resource_id = ?",
            (g.subject_type, g.subject_id, g.resource_type, g.resource_id),
        ).fetchone()
    )
    return grant_out(replace(g, id=str(stored["id"])) if stored else g)


@router.delete("/{grant_id}", operation_id="grants_delete", status_code=204)
async def grants_delete(grant_id: str, admin: Admin, deps: Deps) -> None:
    if not await deps.db.write(lambda c: grants.delete(c, grant_id), durable=True):
        raise ApiError(404, "not_found")
    await deps.refresh_policy()
    await record_audit(deps, admin.profile, "grant.deleted", target=grant_id)


@router.get("/export/{profile_id}", operation_id="grants_export", response_class=PlainTextResponse)
async def grants_export(profile_id: str, session: Session, deps: Deps) -> str:
    """Advanced > YAML export: the effective policy in the IDENTITY_AND_TRUST §5.2 form."""
    target = await load_visible(deps, session, profile_id)

    def _q(c: sqlite3.Connection) -> dict[str, object]:
        groups = sorted(profiles.groups_for(c, target.id))
        rows: list[dict[str, object]] = []
        for st, sid in [("profile", target.id)] + [("group", g) for g in groups]:
            for g in grants.list_for_subject(c, "profile" if st == "profile" else "group", sid):
                item: dict[str, object] = {
                    "subject": f"{st}:{sid}",
                    "resource": f"{g.resource_type}:{g.resource_id}",
                    "max_risk": g.max_risk,
                }
                if g.schedule is not None:
                    item["schedule"] = g.schedule.to_json()
                rows.append(item)
        return {
            "profile": target.name,
            "kind": target.kind,
            "risk_ceiling": target.risk_ceiling,
            "groups": groups,
            "grants": rows,
        }

    return yaml.safe_dump(await deps.db.read(_q), sort_keys=False)


# ------------------------------------------------------------- guest access --


class GuestAccessView(BaseModel):
    active: bool
    until: str | None
    area_ids: list[str]
    default_until: str
    known_areas: list[str]


class GuestAccessBody(BaseModel):
    area_ids: list[str] = Field(default_factory=list[str], max_length=64)
    until: datetime | None = None


@guest_router.get("", operation_id="guest_access_read")
async def guest_access_read(session: Session, deps: Deps) -> GuestAccessView:
    if session.profile.kind not in ("admin", "member"):
        raise ApiError(404, "not_found")
    now = deps.clock()
    rows = await deps.db.read(lambda c: grants.list_for_subject(c, "profile", GUEST_PROFILE_ID))
    mine = [g for g in rows if g.source == GUEST_ACCESS_SOURCE]
    until = next(
        (g.schedule.expires_at for g in mine if g.schedule and g.schedule.expires_at), None
    )
    active = bool(mine) and until is not None and until > now
    tz = await _household_tz(deps)
    known = await deps.db.read(
        lambda c: [
            str(r[0])
            for r in c.execute(
                "SELECT DISTINCT area_id FROM ha_entities WHERE area_id IS NOT NULL"
                " AND removed_at IS NULL ORDER BY area_id"
            ).fetchall()
        ]
    )
    return GuestAccessView(
        known_areas=known,
        active=active,
        until=None if until is None else until.isoformat(),
        area_ids=sorted(g.resource_id for g in mine if g.resource_type == "ha_area"),
        default_until=default_guest_until(now, tz).isoformat(),
    )


async def _household_tz(deps: Deps) -> tzinfo:
    name = await deps.db.read(lambda c: household.get(c, "timezone"))
    try:
        return ZoneInfo(str(name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@guest_router.put("", operation_id="guest_access_start")
async def guest_access_start(body: GuestAccessBody, admin: Admin, deps: Deps) -> GuestAccessView:
    now = deps.clock()
    tz = await _household_tz(deps)
    until = body.until or default_guest_until(now, tz)
    if until.tzinfo is None:
        until = until.replace(tzinfo=tz)
    if until <= now:
        raise ApiError(422, "until_in_past", fields={"until": "until_in_past"})
    rows = guest_access_grants(body.area_ids, until)

    def _w(c: sqlite3.Connection) -> None:
        end_guest_access(c)
        grants.insert_many(c, rows, created_by=admin.profile.id)

    await deps.db.write(_w, durable=True)
    await deps.refresh_policy()
    await record_audit(
        deps,
        admin.profile,
        "guest_access.started",
        details={"until": until.isoformat(), "areas": body.area_ids},
    )
    return await guest_access_read(admin, deps)


@guest_router.delete("", operation_id="guest_access_end", status_code=204)
async def guest_access_end(admin: Admin, deps: Deps) -> None:
    await deps.db.write(end_guest_access, durable=True)
    await deps.refresh_policy()
    await record_audit(deps, admin.profile, "guest_access.ended")


__all__ = ["guest_router", "router"]
