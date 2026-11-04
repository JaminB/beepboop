"""``/api/onboarding``: claim code → first admin → house → home → done (WEB_UI §5.1).

The claim code is the only way to create the first admin. Its session (cookie
``__Host-bb_claim``) can call nothing but these endpoints. After onboarding a reopened
claim session only accepts an admin recovery code (``/recover``), never creates or
resets an admin by itself.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, Field

from beepboop.api.claim import CLAIM_TTL
from beepboop.api.deps import (
    CLAIM_COOKIE,
    Admin,
    ApiDeps,
    Claim,
    Deps,
    clear_cookie,
    record_audit,
    set_cookie,
)
from beepboop.api.people import admin_count, create_profile, recommended_home
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
    consume_recovery_code,
    hash_recovery_code,
    new_recovery_codes,
    new_token,
)
from beepboop.db.repo import household, logins, profiles, sessions
from beepboop.policy.types import GUEST_PROFILE_ID

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

STEPS: tuple[str, ...] = (
    "claim",
    "account",
    "house",
    "llm",
    "home",
    "voice",
    "household",
    "finish",
)


class OnboardingState(BaseModel):
    claimed: bool
    claim_open: bool
    claim_mode: Literal["first_admin", "recovery"] | None
    done: bool
    step: str


class ClaimBody(BaseModel):
    code: str = Field(min_length=6, max_length=12)


class ClaimResultView(BaseModel):
    mode: Literal["first_admin", "recovery"]


class AdminBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(max_length=1024)
    totp_secret: str = Field(min_length=16, max_length=64)
    totp_code: str = Field(min_length=6, max_length=8)


class AdminCreated(BaseModel):
    me: Me
    recovery_codes: list[str]


class RecoverBody(BaseModel):
    username: str
    recovery_code: str


class HouseBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    locale: Literal["en-US"] = "en-US"
    timezone: str = "UTC"
    units: Literal["metric", "imperial"] = "metric"


class HomeBody(BaseModel):
    mode: Literal["recommended", "one_by_one"]


class HomeResult(BaseModel):
    grants_written: int
    inbox: int


class StepBody(BaseModel):
    step: str


async def open_first_claim(deps: ApiDeps) -> str | None:
    """At boot: issue a claim code if nobody has claimed the device yet (OPERATIONS §6).

    The ``on_code`` callback (the audio bridge) speaks it; the code is never logged.
    """
    if await deps.db.read(admin_count) > 0:
        return None
    return deps.claim.issue(deps.clock(), "first_admin")


@router.get("/state", operation_id="onboarding_state")
async def onboarding_state(deps: Deps) -> OnboardingState:
    n_admins = await deps.db.read(admin_count)
    done = bool(await deps.db.read(lambda c: household.get(c, "onboarding_done")))
    step = await deps.db.read(lambda c: household.get(c, "onboarding_step"))
    mode = deps.claim.mode(deps.clock())
    return OnboardingState(
        claimed=n_admins > 0,
        claim_open=mode is not None,
        claim_mode=mode,
        done=done,
        step=str(step) if step is not None else ("house" if n_admins else "claim"),
    )


@router.post("/claim", operation_id="onboarding_claim")
async def onboarding_claim(
    body: ClaimBody,
    response: Response,
    deps: Deps,
) -> ClaimResultView:
    result, mode = deps.claim.check(body.code, deps.clock())
    match result:
        case "none":
            raise ApiError(409, "no_claim_code")
        case "expired":
            raise ApiError(410, "claim_expired")
        case "exhausted":
            raise ApiError(429, "claim_exhausted")
        case "wrong":
            raise ApiError(401, "claim_wrong")
        case "ok":
            pass
    assert mode is not None  # noqa: S101 - "ok" always carries the mode
    token = new_token()
    await deps.db.write(
        lambda c: sessions.create(c, sessions.hash_token(token), GUEST_PROFILE_ID, kind="claim"),
        durable=True,
    )
    set_cookie(response, CLAIM_COOKIE, token, max_age=int(CLAIM_TTL.total_seconds()))
    await record_audit(deps, None, "claim.accepted", details={"mode": mode})
    return ClaimResultView(mode=mode)


@router.post("/admin", operation_id="onboarding_admin")
async def onboarding_admin(
    body: AdminBody,
    claim: Claim,
    response: Response,
    deps: Deps,
    user_agent: Annotated[str | None, Header()] = None,
) -> AdminCreated:
    """Step 1: the first admin (password + TOTP), then the recovery codes, once."""
    if claim.mode != "first_admin":
        raise ApiError(403, "claim_recovery_only")
    validate_new_password(body.password, body.username)
    step = check_totp_setup(deps, body.totp_secret, body.totp_code)
    pw_hash = deps.passwords.hash(body.password)
    if await deps.db.read(lambda c: logins.by_username(c, body.username)) is not None:
        raise ApiError(409, "username_taken", fields={"username": "username_taken"})
    if await deps.db.read(lambda c: profiles.by_name(c, body.name)) is not None:
        raise ApiError(409, "name_taken", fields={"name": "name_taken"})
    if deps.sealer is None:
        raise ApiError(503, "secrets_unavailable")

    def _create(conn: sqlite3.Connection) -> str:
        if admin_count(conn) > 0:
            raise ApiError(409, "already_claimed")
        row = create_profile(conn, body.name, "admin", created_by=None)
        logins.insert(conn, row.id, body.username, pw_hash)
        return row.id

    profile_id = await deps.db.write(_create, durable=True)
    sealed = await seal_totp(deps, profile_id, body.totp_secret)
    codes = new_recovery_codes()
    login = await deps.db.read(lambda c: logins.get(c, profile_id))
    assert login is not None  # noqa: S101 - just inserted
    final = replace(
        login,
        totp_secret_enc=sealed,
        totp_last_step=step,
        recovery_hashes_json=json.dumps([hash_recovery_code(c) for c in codes]),
    )
    await deps.db.write(lambda c: logins.update(c, final), durable=True)
    await deps.db.write(lambda c: sessions.delete(c, claim.id_hash), durable=True)
    await deps.db.write(lambda c: household.set_(c, "onboarding_step", "house"), durable=True)
    deps.claim.clear()
    clear_cookie(response, CLAIM_COOKIE)
    await start_session(deps, response, profile_id, user_agent)
    await deps.refresh_policy()
    profile = await deps.db.read(lambda c: profiles.get(c, profile_id))
    assert profile is not None  # noqa: S101
    await record_audit(deps, profile, "onboarding.admin_created", target=profile_id)
    return AdminCreated(me=me_view(profile), recovery_codes=codes)


@router.post("/recover", operation_id="onboarding_recover")
async def onboarding_recover(
    body: RecoverBody,
    claim: Claim,
    response: Response,
    deps: Deps,
    user_agent: Annotated[str | None, Header()] = None,
) -> Me:
    """Post-onboarding claim mode: an admin recovery code signs that admin in (§5.1)."""
    login = await deps.db.read(lambda c: logins.by_username(c, body.username))
    profile = (
        None if login is None else await deps.db.read(lambda c: profiles.get(c, login.profile_id))
    )
    if login is None or profile is None or profile.kind != "admin":
        raise ApiError(401, "bad_recovery_code")
    remaining = consume_recovery_code(
        cast(list[str], json.loads(login.recovery_hashes_json or "[]")), body.recovery_code
    )
    if remaining is None:
        raise ApiError(401, "bad_recovery_code")
    updated = replace(login, recovery_hashes_json=json.dumps(remaining))
    await deps.db.write(lambda c: logins.update(c, updated), durable=True)
    await deps.db.write(lambda c: sessions.delete(c, claim.id_hash), durable=True)
    clear_cookie(response, CLAIM_COOKIE)
    deps.claim.clear()
    await start_session(deps, response, profile.id, user_agent)
    await record_audit(deps, profile, "claim.recovered", target=profile.id)
    return me_view(profile)


@router.put("/house", operation_id="onboarding_house")
async def onboarding_house(body: HouseBody, admin: Admin, deps: Deps) -> HouseBody:
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ApiError(422, "bad_timezone", fields={"timezone": "bad_timezone"}) from exc

    def _save(conn: sqlite3.Connection) -> None:
        household.set_(conn, "name", body.name)
        household.set_(conn, "locale", body.locale)
        household.set_(conn, "timezone", body.timezone)
        household.set_(conn, "units", body.units)
        household.set_(conn, "onboarding_step", "llm")

    await deps.db.write(_save, durable=True)
    await record_audit(deps, admin.profile, "household.updated")
    return body


@router.post("/home", operation_id="onboarding_home")
async def onboarding_home(body: HomeBody, admin: Admin, deps: Deps) -> HomeResult:
    """Step 4: recommended grants, or leave every device in the inbox (§5.1)."""
    written = 0
    if body.mode == "recommended":
        written = await deps.db.write(
            lambda c: recommended_home(c, created_by=admin.profile.id), durable=True
        )
    await deps.db.write(lambda c: household.set_(c, "onboarding_step", "voice"), durable=True)
    await deps.refresh_policy()
    inbox = await deps.db.read(
        lambda c: c.execute("SELECT COUNT(*) AS n FROM ha_entities WHERE status = 'new'").fetchone()
    )
    await record_audit(deps, admin.profile, "onboarding.home", details={"mode": body.mode})
    return HomeResult(grants_written=written, inbox=0 if inbox is None else int(inbox["n"]))


@router.put("/step", operation_id="onboarding_step")
async def onboarding_step(body: StepBody, _admin: Admin, deps: Deps) -> OnboardingState:
    """Remember where the wizard is (it is resumable)."""
    if body.step not in STEPS and body.step != "done":
        raise ApiError(422, "bad_step")

    def _save(conn: sqlite3.Connection) -> None:
        household.set_(conn, "onboarding_step", body.step)
        if body.step == "done":
            household.set_(conn, "onboarding_done", True)

    await deps.db.write(_save, durable=True)
    return await onboarding_state(deps)


__all__ = ["open_first_claim", "router"]
