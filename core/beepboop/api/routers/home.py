"""``/api/home``, ``/api/household`` and ``/api/ca`` (WEB_UI §5.2, §5.1 step 2, §7).

Home is a status page: the device line, *Needs attention* and the last few turns visible
to this profile. ``/api/ca`` works without a session (the ``/ca`` helper runs over the
untrusted connection before the CA is installed).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from beepboop.api.deps import Admin, Deps, Session, record_audit
from beepboop.api.problem import ApiError
from beepboop.api.routers.system import ca_fingerprint
from beepboop.api.routers.turns import TurnView, turns_list
from beepboop.db.repo import grants, household
from beepboop.policy.types import GUEST_PROFILE_ID

home_router = APIRouter(prefix="/api/home", tags=["home"])
household_router = APIRouter(prefix="/api/household", tags=["household"])
ca_router = APIRouter(prefix="/api/ca", tags=["ca"])


class Attention(BaseModel):
    kind: Literal["inbox", "integration", "guest_access", "update", "backup", "voice", "secrets"]
    count: int = 1
    detail: str = ""
    link: str


class HomeView(BaseModel):
    name: str
    household: str
    device: str
    pending_approvals: int
    attention: list[Attention]
    recent: list[TurnView]


@home_router.get("", operation_id="home_read")
async def home_read(session: Session, deps: Deps) -> HomeView:
    p = session.profile
    now = deps.clock()

    def _q(c: sqlite3.Connection) -> tuple[str, list[Attention]]:
        name = str(household.get(c, "name") or "Home")
        out: list[Attention] = []
        if p.kind == "admin":
            n = int(
                c.execute(
                    "SELECT COUNT(*) FROM ha_entities WHERE status = 'new' AND removed_at IS NULL"
                ).fetchone()[0]
            )
            if n:
                out.append(Attention(kind="inbox", count=n, link="/integrations/ha/inbox"))
            for r in c.execute(
                "SELECT module_id, profile_id, status FROM integrations"
                " WHERE status IN ('reauth', 'error')"
            ).fetchall():
                if r["profile_id"] in (None, p.id):
                    out.append(
                        Attention(
                            kind="integration",
                            detail=f"{r['module_id']}:{r['status']}",
                            link=f"/integrations/{r['module_id']}",
                        )
                    )
        elif p.kind == "member":
            for row in integrations_for(c, p.id):
                out.append(row)
        if p.kind in ("admin", "member"):
            g = [
                x
                for x in grants.list_for_subject(c, "profile", GUEST_PROFILE_ID)
                if x.source == "guest_access"
                and x.schedule
                and x.schedule.expires_at
                and x.schedule.expires_at > now
            ]
            if g:
                until = g[0].schedule.expires_at if g[0].schedule else None
                out.append(
                    Attention(
                        kind="guest_access",
                        detail="" if until is None else until.isoformat(),
                        link="/people/guest",
                    )
                )
        if p.kind != "guest":
            has_voice = c.execute(
                "SELECT 1 FROM voiceprints WHERE profile_id = ? LIMIT 1", (p.id,)
            ).fetchone()
            if has_voice is None:
                out.append(Attention(kind="voice", link=f"/people/{p.id}/voice"))
        if p.kind == "admin" and deps.sealer is None:
            out.append(Attention(kind="secrets", link="/system"))
        return name, out

    name, attention = await deps.db.read(_q)
    if p.kind == "admin" and deps.backups is not None:
        attention.append(Attention(kind="backup", detail="only_local", link="/system/backups"))
    pending = await deps.registry.approvals.store.pending_for(p.id, now)
    page = await turns_list(session, deps, who="me", outcome=None, q=None, limit=5, cursor=None)
    return HomeView(
        name=p.name,
        household=name,
        device="offline" if not deps.enrollment.available else "idle",
        pending_approvals=len(pending),
        attention=attention,
        recent=page.items,
    )


def integrations_for(conn: sqlite3.Connection, profile_id: str) -> list[Attention]:
    rows = conn.execute(
        "SELECT module_id, status FROM integrations WHERE profile_id = ?"
        " AND status IN ('reauth', 'error')",
        (profile_id,),
    ).fetchall()
    return [
        Attention(
            kind="integration",
            detail=f"{r['module_id']}:{r['status']}",
            link=f"/integrations/{r['module_id']}",
        )
        for r in rows
    ]


# ---------------------------------------------------------------- household --


class HouseholdView(BaseModel):
    name: str
    locale: str
    timezone: str
    units: str
    transcript_retention_days: int
    #: DATA_AND_PRIVACY §7: guest turns 0-30 days, audit 90-730 days (admin-editable).
    guest_turn_retention_days: int
    audit_retention_days: int


class HouseholdPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    timezone: str | None = None
    units: Literal["metric", "imperial"] | None = None
    #: 0 keeps the rows without transcript or reply (DATA_AND_PRIVACY §7).
    transcript_retention_days: int | None = Field(default=None, ge=0, le=365)
    guest_turn_retention_days: int | None = Field(default=None, ge=0, le=30)
    audit_retention_days: int | None = Field(default=None, ge=90, le=730)


def _household(values: dict[str, object]) -> HouseholdView:
    return HouseholdView(
        name=str(values.get("name", "Home")),
        locale=str(values.get("locale", "en-US")),
        timezone=str(values.get("timezone", "UTC")),
        units=str(values.get("units", "metric")),
        transcript_retention_days=int(cast(Any, values.get("transcript_retention_days", 30))),
        guest_turn_retention_days=int(cast(Any, values.get("guest_turn_retention_days", 7))),
        audit_retention_days=int(cast(Any, values.get("audit_retention_days", 365))),
    )


@household_router.get("", operation_id="household_read")
async def household_read(_session: Session, deps: Deps) -> HouseholdView:
    return _household(await deps.db.read(household.all_))


@household_router.patch("", operation_id="household_update")
async def household_update(body: HouseholdPatch, admin: Admin, deps: Deps) -> HouseholdView:
    patch = body.model_dump(exclude_unset=True, exclude_none=True)
    if "timezone" in patch:
        try:
            ZoneInfo(str(patch["timezone"]))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ApiError(422, "bad_timezone", fields={"timezone": "bad_timezone"}) from exc

    def _w(c: sqlite3.Connection) -> None:
        for k, v in patch.items():
            household.set_(c, k, v)

    await deps.db.write(_w, durable=True)
    await record_audit(deps, admin.profile, "household.updated", details={"keys": sorted(patch)})
    return _household(await deps.db.read(household.all_))


# ----------------------------------------------------------------------- CA --


class CaView(BaseModel):
    fingerprint: str | None
    canonical_url: str
    download: str | None


@ca_router.get("", operation_id="ca_read")
async def ca_read(deps: Deps) -> CaView:
    fp = ca_fingerprint(deps)
    return CaView(
        fingerprint=fp,
        canonical_url=deps.public_url,
        download=None if fp is None else "/api/ca/root.crt",
    )


@ca_router.get("/root.crt", operation_id="ca_download", response_class=Response)
async def ca_download(deps: Deps) -> Response:
    if deps.ca_cert is None or not deps.ca_cert.is_file():
        raise ApiError(404, "no_ca")
    return Response(
        deps.ca_cert.read_bytes(),
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="beepboop-root.crt"'},
    )


__all__ = ["ca_router", "home_router", "household_router"]
