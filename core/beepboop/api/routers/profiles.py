"""``/api/profiles``: People, Profile, Voice metadata, PIN, Access (WEB_UI §5.3-§5.5).

Visibility is the §3 table: admins manage everyone; members and children see their own
page; Access is read-only for non-admins. Every write that changes what policy sees ends
with ``refresh_policy()``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from beepboop.api.deps import (
    Admin,
    ApiDeps,
    Deps,
    Session,
    WebSession,
    can_manage_profile,
    can_view_profile,
    record_audit,
    web_principal,
)
from beepboop.api.people import CreatablePreset, admin_count, create_profile
from beepboop.api.problem import ApiError
from beepboop.api.routers.invites import InviteCreated, create_invite
from beepboop.api.security import SealerUnavailableError, column_aad
from beepboop.db.repo import grants, ha_entities, logins, profiles, sessions, voiceprints
from beepboop.db.repo.profiles import ProfileRow, Verbosity
from beepboop.db.retention import LastAdminError, delete_profile
from beepboop.policy.pin import PinFormatError, hash_pin
from beepboop.policy.types import GUEST_PROFILE_ID, Risk

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class VoiceSummary(BaseModel):
    enrolled: bool
    model_ids: list[str]
    n_clips: int
    updated_at: int | None
    consent_version: str | None


class ProfileView(BaseModel):
    id: str
    name: str
    kind: str
    risk_ceiling: str
    locale: str
    tts_voice: str | None
    speech_rate: float
    verbosity: str
    transcript_retention_days: int | None
    disabled: bool
    has_login: bool
    username: str | None
    totp_on: bool
    pin_set: bool
    pin_locked: bool
    groups: list[str]
    voice: VoiceSummary
    created_at: int


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    preset: CreatablePreset
    quiet_hours: bool = False
    invite: bool = True


class ProfileCreated(BaseModel):
    profile: ProfileView
    invite: InviteCreated | None


class ProfilePatch(BaseModel):
    """JSON Merge Patch of the editable fields (WEB_UI §8); admin-only fields are marked."""

    name: str | None = Field(default=None, min_length=1, max_length=64)  # admin
    risk_ceiling: Risk | None = None  # admin
    disabled: bool | None = None  # admin
    locale: Literal["en-US"] | None = None
    tts_voice: str | None = Field(default=None, max_length=64)
    speech_rate: float | None = Field(default=None, ge=0.5, le=2.0)
    verbosity: Verbosity | None = None
    #: 0 keeps the rows without transcript or reply (DATA_AND_PRIVACY §7).
    transcript_retention_days: int | None = Field(default=None, ge=0, le=365)


class PinBody(BaseModel):
    pin: str = Field(min_length=4, max_length=8)


class GrantView(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    inherited: bool
    resource_type: str
    resource_id: str
    label: str
    max_risk: str
    schedule: str | None
    source: str


class ToolAccess(BaseModel):
    name: str
    risk: str
    allowed: bool


class ModuleAccess(BaseModel):
    module_id: str
    summary: str
    tools: list[ToolAccess]


class AccessView(BaseModel):
    profile_id: str
    risk_ceiling: str
    editable: bool
    grants: list[GrantView]
    modules: list[ModuleAccess]


def _voice(rows: list[voiceprints.VoiceprintRow]) -> VoiceSummary:
    if not rows:
        return VoiceSummary(
            enrolled=False, model_ids=[], n_clips=0, updated_at=None, consent_version=None
        )
    latest = max(rows, key=lambda r: r.updated_at)
    return VoiceSummary(
        enrolled=True,
        model_ids=sorted(r.model_id for r in rows),
        n_clips=latest.n_clips,
        updated_at=latest.updated_at,
        consent_version=latest.consent_version,
    )


def _voiceprints_for(conn: sqlite3.Connection, profile_id: str) -> list[voiceprints.VoiceprintRow]:
    rows = conn.execute("SELECT * FROM voiceprints WHERE profile_id = ?", (profile_id,)).fetchall()
    return [voiceprints.VoiceprintRow.from_row(r) for r in rows]


def profile_view(conn: sqlite3.Connection, p: ProfileRow, now_ms: int) -> ProfileView:
    login = logins.get(conn, p.id)
    return ProfileView(
        id=p.id,
        name=p.name,
        kind=p.kind,
        risk_ceiling=p.risk_ceiling,
        locale=p.locale,
        tts_voice=p.tts_voice,
        speech_rate=p.speech_rate,
        verbosity=p.verbosity,
        transcript_retention_days=p.transcript_retention_days,
        disabled=p.disabled_at is not None,
        has_login=login is not None,
        username=None if login is None else login.username,
        totp_on=login is not None and login.totp_secret_enc is not None,
        pin_set=p.pin_hash_enc is not None,
        pin_locked=p.pin_locked_until is not None and p.pin_locked_until > now_ms,
        groups=sorted(profiles.groups_for(conn, p.id)),
        voice=_voice(_voiceprints_for(conn, p.id)),
        created_at=p.created_at,
    )


async def load_visible(deps: ApiDeps, session: WebSession, profile_id: str) -> ProfileRow:
    """The profile, or 404 when this session may not see it (§8: same as not existing)."""
    target = await deps.db.read(lambda c: profiles.get(c, profile_id))
    if target is None or not can_view_profile(session.profile, target):
        raise ApiError(404, "not_found")
    return target


@router.get("", operation_id="profiles_list")
async def profiles_list(session: Session, deps: Deps) -> list[ProfileView]:
    now = deps.now_ms()

    def _q(c: sqlite3.Connection) -> list[ProfileView]:
        rows = profiles.list_(c) if session.is_admin else [session.profile]
        return [profile_view(c, p, now) for p in rows]

    return await deps.db.read(_q)


@router.post("", operation_id="profiles_create", status_code=201)
async def profiles_create(body: ProfileCreate, admin: Admin, deps: Deps) -> ProfileCreated:
    """Add person → preset → name → invite link for Admin/Member (§5.3)."""
    if await deps.db.read(lambda c: profiles.by_name(c, body.name)) is not None:
        raise ApiError(409, "name_taken", fields={"name": "name_taken"})
    row = await deps.db.write(
        lambda c: create_profile(
            c, body.name, body.preset, created_by=admin.profile.id, quiet_hours=body.quiet_hours
        ),
        durable=True,
    )
    await deps.refresh_policy()
    invite = None
    if body.invite and body.preset in ("admin", "member"):
        invite = await create_invite(deps, row.id)
    await record_audit(
        deps, admin.profile, "profile.created", target=row.id, details={"preset": body.preset}
    )
    now = deps.now_ms()
    view = await deps.db.read(lambda c: profile_view(c, row, now))
    return ProfileCreated(profile=view, invite=invite)


@router.get("/{profile_id}", operation_id="profiles_read")
async def profiles_read(profile_id: str, session: Session, deps: Deps) -> ProfileView:
    target = await load_visible(deps, session, profile_id)
    now = deps.now_ms()
    return await deps.db.read(lambda c: profile_view(c, target, now))


@router.patch("/{profile_id}", operation_id="profiles_update")
async def profiles_update(
    profile_id: str, body: ProfilePatch, session: Session, deps: Deps
) -> ProfileView:
    target = await load_visible(deps, session, profile_id)
    if not can_manage_profile(session.profile, target):
        raise ApiError(403, "forbidden")
    patch = body.model_dump(exclude_unset=True)
    admin_fields = {"name", "risk_ceiling", "disabled"}
    if admin_fields & patch.keys() and not session.is_admin:
        raise ApiError(
            403, "admin_only", fields=dict.fromkeys(admin_fields & patch.keys(), "admin_only")
        )
    if target.id == GUEST_PROFILE_ID and ({"name", "disabled"} & patch.keys()):
        raise ApiError(409, "guest_fixed")
    if patch.get("disabled") and target.kind == "admin":
        n = await deps.db.read(admin_count)
        if n <= 1:
            raise ApiError(409, "last_admin")
    new = target
    for key in (
        "name",
        "risk_ceiling",
        "locale",
        "tts_voice",
        "speech_rate",
        "verbosity",
        "transcript_retention_days",
    ):
        if key in patch:
            new = replace(new, **{key: patch[key]})
    if "disabled" in patch:
        new = replace(new, disabled_at=deps.now_ms() if patch["disabled"] else None)
    final = new
    await deps.db.write(lambda c: profiles.update(c, final), durable=True)
    if admin_fields & patch.keys():
        await deps.refresh_policy()
        await record_audit(
            deps,
            session.profile,
            "profile.updated",
            target=profile_id,
            details={"fields": sorted(admin_fields & patch.keys())},
        )
    now = deps.now_ms()
    return await deps.db.read(lambda c: profile_view(c, final, now))


@router.delete("/{profile_id}", operation_id="profiles_delete", status_code=204)
async def profiles_delete(
    profile_id: str,
    session: Session,
    deps: Deps,
    confirm: Annotated[str, Query(description="The profile's name, typed")],
) -> None:
    """ "Delete me": the DATA_AND_PRIVACY §9.3 cascade; refused for the last admin."""
    target = await load_visible(deps, session, profile_id)
    if not (session.is_admin or session.profile.id == target.id):
        raise ApiError(403, "forbidden")
    if target.id == GUEST_PROFILE_ID:
        raise ApiError(409, "guest_fixed")
    if confirm.strip() != target.name:
        raise ApiError(422, "confirm_mismatch", fields={"confirm": "confirm_mismatch"})
    now = deps.clock()
    actor = session.profile.id if session.profile.id != target.id else None
    try:
        await deps.db.write(
            lambda c: delete_profile(c, target.id, acting_admin=actor, now=now), durable=True
        )
    except LastAdminError as exc:
        raise ApiError(409, "last_admin") from exc
    await deps.refresh_policy()


# ---------------------------------------------------------------------- PIN --


@router.put("/{profile_id}/pin", operation_id="profiles_pin_set", status_code=204)
async def profiles_pin_set(profile_id: str, body: PinBody, session: Session, deps: Deps) -> None:
    """Set the spoken-PIN fallback (IDENTITY_AND_TRUST §8.4). Web only, never by voice."""
    target = await load_visible(deps, session, profile_id)
    if not can_manage_profile(session.profile, target) or target.kind in ("child", "guest"):
        raise ApiError(403, "forbidden")
    try:
        encoded = hash_pin(body.pin)
    except PinFormatError as exc:
        raise ApiError(422, "bad_pin", fields={"pin": "bad_pin"}) from exc
    if deps.sealer is None:
        raise ApiError(503, "secrets_unavailable")
    try:
        sealed = await deps.sealer.seal(
            column_aad("profiles", target.id, "pin_hash_enc"), encoded.encode()
        )
    except SealerUnavailableError as exc:
        raise ApiError(503, "secrets_unavailable") from exc
    await deps.db.write(lambda c: profiles.set_pin(c, target.id, sealed), durable=True)
    await deps.refresh_policy()
    await record_audit(deps, session.profile, "pin.set", target=target.id)


@router.delete("/{profile_id}/pin", operation_id="profiles_pin_clear", status_code=204)
async def profiles_pin_clear(profile_id: str, session: Session, deps: Deps) -> None:
    target = await load_visible(deps, session, profile_id)
    if not can_manage_profile(session.profile, target):
        raise ApiError(403, "forbidden")
    await deps.db.write(lambda c: profiles.set_pin(c, target.id, None), durable=True)
    await deps.refresh_policy()
    await record_audit(deps, session.profile, "pin.cleared", target=target.id)


# -------------------------------------------------------------------- voice --


@router.delete("/{profile_id}/voice", operation_id="profiles_voice_delete", status_code=204)
async def profiles_voice_delete(profile_id: str, session: Session, deps: Deps) -> None:
    """*Delete voiceprint*: consent withdrawal (DATA_AND_PRIVACY §9.1)."""
    target = await load_visible(deps, session, profile_id)
    if not can_manage_profile(session.profile, target):
        raise ApiError(403, "forbidden")
    await deps.db.write(lambda c: voiceprints.delete(c, target.id), durable=True)
    await deps.refresh_policy()
    await record_audit(deps, session.profile, "voiceprint.deleted", target=target.id)


# ----------------------------------------------------------------- sessions --


@router.post(
    "/{profile_id}/sign-out-everywhere",
    operation_id="profiles_sign_out_everywhere",
    status_code=204,
)
async def profiles_sign_out_everywhere(profile_id: str, session: Session, deps: Deps) -> None:
    target = await load_visible(deps, session, profile_id)
    await deps.db.write(lambda c: sessions.delete_for_profile(c, target.id), durable=True)
    await record_audit(deps, session.profile, "sessions.revoked_all", target=target.id)


# ------------------------------------------------------------------- access --


def _label(conn: sqlite3.Connection, resource_type: str, resource_id: str) -> str:
    if resource_type == "ha_entity":
        ent = ha_entities.get(conn, resource_id)
        if ent is not None:
            return ent.friendly_name or ent.entity_id
    if resource_type == "tool_glob":
        return resource_id
    return resource_id.replace("_", " ").capitalize()


@router.get("/{profile_id}/access", operation_id="profiles_access")
async def profiles_access(profile_id: str, session: Session, deps: Deps) -> AccessView:
    """Own grants plus inherited group grants (lock icon), and what each module allows."""
    target = await load_visible(deps, session, profile_id)

    def _q(c: sqlite3.Connection) -> list[GrantView]:
        out: list[GrantView] = []
        own = grants.list_for_subject(c, "profile", target.id)
        inherited = [
            g
            for gid in sorted(profiles.groups_for(c, target.id))
            for g in grants.list_for_subject(c, "group", gid)
        ]
        for g, inh in [(g, False) for g in own] + [(g, True) for g in inherited]:
            out.append(
                GrantView(
                    id=g.id,
                    subject_type=g.subject_type,
                    subject_id=g.subject_id,
                    inherited=inh,
                    resource_type=g.resource_type,
                    resource_id=g.resource_id,
                    label=_label(c, g.resource_type, g.resource_id),
                    max_risk=g.max_risk,
                    schedule=None if g.schedule is None else g.schedule.to_json(),
                    source=g.source,
                )
            )
        return out

    rows = await deps.db.read(_q)
    principal = web_principal(target, device="web:access", turn_id="access-view")
    allowed = {t.name for t in deps.registry.callable_by(principal)}
    modules: list[ModuleAccess] = []
    for manifest, _module in deps.registry.modules():
        modules.append(
            ModuleAccess(
                module_id=manifest.id,
                summary=manifest.summary,
                tools=[
                    ToolAccess(name=t.name, risk=t.risk, allowed=t.name in allowed)
                    for t in manifest.tools
                ],
            )
        )
    return AccessView(
        profile_id=target.id,
        risk_ceiling=target.risk_ceiling,
        editable=session.is_admin,
        grants=rows,
        modules=modules,
    )


__all__ = ["ProfileView", "load_visible", "profile_view", "router"]
