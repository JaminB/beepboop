"""``/api/auth``: password + TOTP login, sessions, credential changes (docs/WEB_UI.md §6).

Login is one request: username + password + (TOTP code or a recovery code). A correct
password without a second factor answers ``401 totp_required`` so the UI can show the
TOTP step; every other failure is ``401 bad_credentials`` (never "no such user").
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Annotated, cast

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field

from beepboop.api.deps import (
    SESSION_COOKIE,
    ApiDeps,
    Deps,
    Session,
    clear_cookie,
    record_audit,
    set_cookie,
    web_scope,
)
from beepboop.api.problem import ApiError
from beepboop.api.routers.approvals import extend_pending_for
from beepboop.api.security import (
    PasswordPolicyError,
    SealerUnavailableError,
    check_password_policy,
    column_aad,
    consume_recovery_code,
    hash_recovery_code,
    new_recovery_codes,
    new_token,
    new_totp_secret,
    provisioning_uri,
    public_id,
    verify_totp,
)
from beepboop.db.repo import logins, profiles, sessions
from beepboop.db.repo.logins import LoginRow
from beepboop.db.repo.profiles import ProfileRow
from beepboop.db.repo.sessions import ABSOLUTE_TIMEOUT

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_MAX_AGE = int(ABSOLUTE_TIMEOUT.total_seconds())


class Me(BaseModel):
    profile_id: str
    name: str
    kind: str
    locale: str
    scopes: list[str]


def me_view(p: ProfileRow) -> Me:
    return Me(
        profile_id=p.id, name=p.name, kind=p.kind, locale=p.locale, scopes=sorted(web_scope(p))
    )


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    totp: str | None = Field(default=None, max_length=16)
    recovery_code: str | None = Field(default=None, max_length=32)


class TotpSetup(BaseModel):
    secret: str
    uri: str


class TotpConfirmBody(BaseModel):
    secret: str = Field(min_length=16, max_length=64)
    code: str = Field(min_length=6, max_length=8)
    #: Re-authentication (WEB_UI §6.2): the password plus the current TOTP or a recovery
    #: code, so a stolen or unattended session cannot swap the second factor.
    current_password: str = Field(min_length=1, max_length=1024)
    current_totp: str | None = Field(default=None, max_length=16)
    recovery_code: str | None = Field(default=None, max_length=32)


class RecoveryCodes(BaseModel):
    recovery_codes: list[str]


class PasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(max_length=1024)


class SessionItem(BaseModel):
    id: str
    created_at: int
    last_seen_at: int
    ua: str | None
    current: bool


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def requires_totp(profile: ProfileRow, login: LoginRow) -> bool:
    """TOTP is required for admin and member logins; optional for a child (§6.1)."""
    return profile.kind in ("admin", "member") or login.totp_secret_enc is not None


async def start_session(
    deps: ApiDeps, response: Response, profile_id: str, user_agent: str | None
) -> None:
    token = new_token()
    await deps.db.write(
        lambda c: sessions.create(c, sessions.hash_token(token), profile_id, ua=user_agent),
        durable=True,
    )
    set_cookie(response, SESSION_COOKIE, token, max_age=SESSION_MAX_AGE)


async def seal_totp(deps: ApiDeps, profile_id: str, secret: str) -> bytes:
    if deps.sealer is None:
        raise ApiError(503, "secrets_unavailable")
    try:
        return await deps.sealer.seal(
            column_aad("logins", profile_id, "totp_secret_enc"), secret.encode()
        )
    except SealerUnavailableError as exc:
        raise ApiError(503, "secrets_unavailable") from exc


async def open_totp(deps: ApiDeps, login: LoginRow) -> str:
    if deps.sealer is None or login.totp_secret_enc is None:
        raise ApiError(503, "secrets_unavailable")
    raw = await deps.sealer.open(
        column_aad("logins", login.profile_id, "totp_secret_enc"), login.totp_secret_enc
    )
    return raw.decode()


def validate_new_password(password: str, username: str | None) -> None:
    try:
        check_password_policy(password, username=username)
    except PasswordPolicyError as exc:
        raise ApiError(422, exc.code, fields={"password": exc.code}) from exc


def check_totp_setup(deps: ApiDeps, secret: str, code: str) -> int:
    step = verify_totp(secret, code, deps.clock(), None)
    if step is None:
        raise ApiError(422, "bad_totp", fields={"totp_code": "bad_totp"})
    return step


@router.post("/login", operation_id="auth_login")
async def auth_login(
    body: LoginBody,
    request: Request,
    response: Response,
    deps: Deps,
    user_agent: Annotated[str | None, Header()] = None,
) -> Me:
    now = deps.clock()
    ip = client_ip(request)
    throttle = deps.throttle
    assert throttle is not None  # noqa: S101 - set in ApiDeps.__post_init__
    wait = throttle.retry_after(body.username, ip, now)
    if wait > 0:
        raise ApiError(429, "too_many_attempts", retry_after=wait)
    login = await deps.db.read(lambda c: logins.by_username(c, body.username))
    if login is None:
        deps.passwords.dummy_verify(body.password)
        throttle.password_failed(body.username, ip, now)
        raise ApiError(401, "bad_credentials")
    profile = await deps.db.read(lambda c: profiles.get(c, login.profile_id))
    if not deps.passwords.verify(login.pw_hash, body.password) or profile is None:
        if throttle.password_failed(body.username, ip, now) and profile is not None:
            await record_audit(deps, profile, "login.locked", target=profile.id)
        raise ApiError(401, "bad_credentials")
    if profile.disabled_at is not None:
        raise ApiError(401, "bad_credentials")

    updated = login
    if requires_totp(profile, login):
        if body.recovery_code:
            remaining = consume_recovery_code(
                cast(list[str], json.loads(login.recovery_hashes_json or "[]")), body.recovery_code
            )
            if remaining is None:
                throttle.totp_failed(body.username, now)
                raise ApiError(401, "bad_recovery_code")
            updated = replace(login, recovery_hashes_json=json.dumps(remaining))
            await record_audit(deps, profile, "login.recovery_code", target=profile.id)
        elif body.totp:
            if login.totp_secret_enc is None:
                raise ApiError(401, "totp_not_set_up")
            secret = await open_totp(deps, login)
            step = verify_totp(secret, body.totp, now, login.totp_last_step)
            if step is None:
                if throttle.totp_failed(body.username, now):
                    await record_audit(deps, profile, "login.totp_locked", target=profile.id)
                raise ApiError(401, "bad_totp")
            updated = replace(login, totp_last_step=step)
        else:
            raise ApiError(401, "totp_required")
    throttle.succeeded(body.username)
    final = updated
    await deps.db.write(lambda c: logins.update(c, final), durable=True)
    await start_session(deps, response, profile.id, user_agent)
    await extend_pending_for(deps, profile.id)
    return me_view(profile)


@router.post("/logout", operation_id="auth_logout", status_code=204)
async def auth_logout(session: Session, deps: Deps, response: Response) -> None:
    await deps.db.write(lambda c: sessions.delete(c, session.id_hash), durable=True)
    clear_cookie(response, SESSION_COOKIE)


@router.get("/sessions", operation_id="auth_sessions")
async def auth_sessions(session: Session, deps: Deps) -> list[SessionItem]:
    rows = await deps.db.read(
        lambda c: c.execute(
            "SELECT * FROM sessions WHERE profile_id = ? AND kind = 'web'"
            " ORDER BY last_seen_at DESC",
            (session.profile.id,),
        ).fetchall()
    )
    out: list[SessionItem] = []
    for r in rows:
        row = sessions.SessionRow.from_row(r)
        out.append(
            SessionItem(
                id=public_id(row.id_hash),
                created_at=row.created_at,
                last_seen_at=row.last_seen_at,
                ua=row.ua,
                current=row.id_hash == session.id_hash,
            )
        )
    return out


@router.post("/password", operation_id="auth_password_change", status_code=204)
async def auth_password_change(body: PasswordBody, session: Session, deps: Deps) -> None:
    login = await deps.db.read(lambda c: logins.get(c, session.profile.id))
    if login is None or not deps.passwords.verify(login.pw_hash, body.current_password):
        raise ApiError(403, "bad_credentials")
    validate_new_password(body.new_password, login.username)
    new = replace(login, pw_hash=deps.passwords.hash(body.new_password))
    pid, keep = session.profile.id, session.id_hash

    def _write(c: sqlite3.Connection) -> None:
        logins.update(c, new)
        # Every other session is signed out (WEB_UI §6.2); this one stays.
        c.execute("DELETE FROM sessions WHERE profile_id = ? AND id_hash != ?", (pid, keep))

    await deps.db.write(_write, durable=True)
    await record_audit(deps, session.profile, "login.password_changed", target=session.profile.id)


@router.get("/totp/new", operation_id="auth_totp_new")
async def auth_totp_new(username: str = "beepboop") -> TotpSetup:
    """A fresh TOTP secret for an enrolment screen; nothing is stored until confirmed."""
    secret = new_totp_secret()
    return TotpSetup(secret=secret, uri=provisioning_uri(secret, username))


@router.post("/totp", operation_id="auth_totp_replace")
async def auth_totp_replace(body: TotpConfirmBody, session: Session, deps: Deps) -> RecoveryCodes:
    """Set or replace this login's TOTP (Profile > Sign-in > Manage); new recovery codes."""
    login = await deps.db.read(lambda c: logins.get(c, session.profile.id))
    if login is None:
        raise ApiError(404, "no_login")
    if not deps.passwords.verify(login.pw_hash, body.current_password):
        raise ApiError(403, "bad_credentials")
    if login.totp_secret_enc is not None:
        # Replacing an existing factor needs the existing factor (or a recovery code).
        if body.recovery_code:
            remaining = consume_recovery_code(
                cast(list[str], json.loads(login.recovery_hashes_json or "[]")), body.recovery_code
            )
            if remaining is None:
                raise ApiError(403, "bad_recovery_code")
        elif body.current_totp:
            secret = await open_totp(deps, login)
            if verify_totp(secret, body.current_totp, deps.clock(), login.totp_last_step) is None:
                raise ApiError(403, "bad_totp")
        else:
            raise ApiError(403, "totp_required")
    step = check_totp_setup(deps, body.secret, body.code)
    sealed = await seal_totp(deps, login.profile_id, body.secret)
    codes = new_recovery_codes()
    new = replace(
        login,
        totp_secret_enc=sealed,
        totp_last_step=step,
        recovery_hashes_json=json.dumps([hash_recovery_code(c) for c in codes]),
    )
    await deps.db.write(lambda c: logins.update(c, new), durable=True)
    await record_audit(deps, session.profile, "login.totp_replaced", target=session.profile.id)
    return RecoveryCodes(recovery_codes=codes)


__all__ = [
    "Me",
    "check_totp_setup",
    "me_view",
    "router",
    "seal_totp",
    "start_session",
    "validate_new_password",
]
