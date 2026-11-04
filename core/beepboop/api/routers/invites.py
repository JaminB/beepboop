"""``/api/invites``: one-time 24 h links that create a login for an existing profile
(WEB_UI §5.1b, §6.1). Only the token's SHA-256 is stored; the link carries the token.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, Field

from beepboop.api.deps import Admin, ApiDeps, Deps, record_audit
from beepboop.api.problem import ApiError
from beepboop.api.routers.auth import (
    Me,
    check_totp_setup,
    me_view,
    seal_totp,
    start_session,
    validate_new_password,
)
from beepboop.api.security import (
    hash_recovery_code,
    new_recovery_codes,
    new_token,
    public_id,
    token_hash,
)
from beepboop.db.repo import logins, profiles

router = APIRouter(prefix="/api/invites", tags=["invites"])

INVITE_TTL = timedelta(hours=24)


class InviteCreate(BaseModel):
    profile_id: str


class InviteCreated(BaseModel):
    id: str
    profile_id: str
    url: str
    token: str
    expires_at: int


class InviteItem(BaseModel):
    id: str
    profile_id: str
    name: str
    kind: str
    expires_at: int


class InvitePreview(BaseModel):
    name: str
    kind: str
    totp_required: bool


class InviteAccept(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(max_length=1024)
    totp_secret: str | None = Field(default=None, max_length=64)
    totp_code: str | None = Field(default=None, max_length=8)


class InviteAccepted(BaseModel):
    me: Me
    recovery_codes: list[str]


async def create_invite(deps: ApiDeps, profile_id: str) -> InviteCreated:
    token = new_token()
    h = token_hash(token)
    expires = deps.now_ms() + int(INVITE_TTL.total_seconds() * 1000)
    await deps.db.write(lambda c: logins.insert_invite(c, h, profile_id, expires), durable=True)
    url = f"{deps.public_url.rstrip('/')}/invite/{token}"
    return InviteCreated(
        id=public_id(h), profile_id=profile_id, url=url, token=token, expires_at=expires
    )


@router.post("", operation_id="invites_create")
async def invites_create(body: InviteCreate, admin: Admin, deps: Deps) -> InviteCreated:
    profile = await deps.db.read(lambda c: profiles.get(c, body.profile_id))
    if profile is None or profile.kind == "guest":
        raise ApiError(404, "not_found")
    if await deps.db.read(lambda c: logins.get(c, body.profile_id)) is not None:
        raise ApiError(409, "already_has_login")
    out = await create_invite(deps, profile.id)
    await record_audit(deps, admin.profile, "invite.created", target=profile.id)
    return out


@router.get("", operation_id="invites_list")
async def invites_list(_admin: Admin, deps: Deps) -> list[InviteItem]:
    now = deps.now_ms()

    def _q(c: sqlite3.Connection) -> list[InviteItem]:
        out: list[InviteItem] = []
        for h, pid, exp in logins.list_invites(c, now):
            p = profiles.get(c, pid)
            if p is not None:
                out.append(
                    InviteItem(
                        id=public_id(h), profile_id=pid, name=p.name, kind=p.kind, expires_at=exp
                    )
                )
        return out

    return await deps.db.read(_q)


@router.delete("/{invite_id}", operation_id="invites_revoke", status_code=204)
async def invites_revoke(invite_id: str, admin: Admin, deps: Deps) -> None:
    now = deps.now_ms()
    found = await deps.db.read(
        lambda c: [h for h, _p, _e in logins.list_invites(c, now) if public_id(h) == invite_id]
    )
    if not found:
        raise ApiError(404, "not_found")
    await deps.db.write(lambda c: logins.delete_invite(c, found[0]), durable=True)
    await record_audit(deps, admin.profile, "invite.revoked", target=invite_id)


@router.get("/{token}", operation_id="invites_preview")
async def invites_preview(token: str, deps: Deps) -> InvitePreview:
    """No session: what the invite page greets with ("Hi Sam, …")."""
    now = deps.now_ms()
    pid = await deps.db.read(lambda c: logins.peek_invite(c, token_hash(token), now))
    profile = None if pid is None else await deps.db.read(lambda c: profiles.get(c, pid))
    if profile is None:
        raise ApiError(404, "invite_invalid")
    return InvitePreview(
        name=profile.name, kind=profile.kind, totp_required=profile.kind in ("admin", "member")
    )


@router.post("/{token}/accept", operation_id="invites_accept")
async def invites_accept(
    token: str,
    body: InviteAccept,
    response: Response,
    deps: Deps,
    user_agent: Annotated[str | None, Header()] = None,
) -> InviteAccepted:
    now = deps.now_ms()
    h = token_hash(token)
    pid = await deps.db.read(lambda c: logins.peek_invite(c, h, now))
    profile = None if pid is None else await deps.db.read(lambda c: profiles.get(c, pid))
    if profile is None:
        raise ApiError(404, "invite_invalid")
    validate_new_password(body.password, body.username)
    need_totp = profile.kind in ("admin", "member")
    step: int | None = None
    if body.totp_secret and body.totp_code:
        step = check_totp_setup(deps, body.totp_secret, body.totp_code)
    elif need_totp:
        raise ApiError(422, "totp_required", fields={"totp_code": "totp_required"})
    if await deps.db.read(lambda c: logins.by_username(c, body.username)) is not None:
        raise ApiError(409, "username_taken", fields={"username": "username_taken"})
    pw_hash = deps.passwords.hash(body.password)
    sealed = None if step is None else await seal_totp(deps, profile.id, body.totp_secret or "")
    codes = new_recovery_codes() if sealed is not None else []

    def _accept(c: sqlite3.Connection) -> None:
        if logins.use_invite(c, h, now) is None:
            raise ApiError(404, "invite_invalid")
        row = logins.insert(c, profile.id, body.username, pw_hash)
        logins.update(
            c,
            replace(
                row,
                totp_secret_enc=sealed,
                totp_last_step=step,
                recovery_hashes_json=json.dumps([hash_recovery_code(x) for x in codes])
                if codes
                else None,
            ),
        )

    await deps.db.write(_accept, durable=True)
    await start_session(deps, response, profile.id, user_agent)
    await deps.refresh_policy()
    await record_audit(deps, profile, "invite.accepted", target=profile.id)
    return InviteAccepted(me=me_view(profile), recovery_codes=codes)


__all__ = ["create_invite", "router"]
