"""Session auth and shared dependencies for ``/api`` (docs/WEB_UI.md §6, §8).

Every request resolves to a ``Principal`` with ``via="web"``; tool calls from the web go
through ``registry.dispatch`` like every other door. ``web_scope`` is the one function
that maps ``profiles.kind`` to web visibility (§6.3); :func:`can_view_profile` and
:func:`can_manage_profile` apply it to "whose page is this".
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response

from beepboop.api.claim import ClaimManager
from beepboop.api.events import EventBus
from beepboop.api.ports import (
    BackupRunner,
    EnrollmentChannel,
    LogSource,
    NoAudioEnrollment,
    NoUpdateSource,
    UpdateSource,
)
from beepboop.api.security import ColumnSealer, Passwords
from beepboop.db.database import Database
from beepboop.db.ids import to_ms
from beepboop.db.repo import audit, profiles, sessions
from beepboop.db.repo.profiles import ProfileRow
from beepboop.policy.authorize import PolicyState
from beepboop.policy.types import ALL_SCOPES, Principal
from beepboop.registry.registry import Registry

if TYPE_CHECKING:
    from beepboop.agent.turn import TurnLoop
    from beepboop.api.enroll import EnrollmentHub
    from beepboop.api.ratelimit import LoginThrottle
    from beepboop.health import HealthProbes
    from beepboop.secrets import SecretStore

SESSION_COOKIE = "__Host-bb_session"
CLAIM_COOKIE = "__Host-bb_claim"
WebScope = Literal["admin", "self", "child_readonly"]


async def _no_refresh() -> None:
    return None


@dataclass
class ApiDeps:
    """What the routers need; stored on ``app.state.deps`` by ``create_app``.

    Only ``db``, ``registry`` and ``policy_state`` are required; every other seam has a
    stand-in so the API boots with zero hardware (core/README.md).
    """

    db: Database
    registry: Registry
    policy_state: Callable[[], PolicyState]
    public_url: str = "https://beepboop.local"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    version: str = "0.0.0"
    #: Rebuild the PolicyState snapshot after profiles/grants/entities/logins writes.
    refresh_policy: Callable[[], Awaitable[None]] = _no_refresh
    events: EventBus = field(default_factory=EventBus)
    claim: ClaimManager = field(default_factory=ClaimManager)
    passwords: Passwords = field(default_factory=Passwords)
    #: Seals ``*_enc`` columns (TOTP secret, PIN hash). ``None`` → no KEK: 503 on setup.
    sealer: ColumnSealer | None = None
    secrets: SecretStore | None = None
    loop: TurnLoop | None = None
    enrollment: EnrollmentChannel = field(default_factory=NoAudioEnrollment)
    enroll_hub: EnrollmentHub | None = None
    throttle: LoginThrottle | None = None
    logs: LogSource | None = None
    backups: BackupRunner | None = None
    updates: UpdateSource = field(default_factory=NoUpdateSource)
    data_dir: Path | None = None
    ca_cert: Path | None = None
    #: ``/hostrun/host.json`` from ``beepboop-host.timer`` (OPERATIONS §5); ``None`` reads
    #: ``<data_dir>/host.json`` (dev).
    host_json: Path | None = None
    #: Live probes for the health report (HA connection, audio device, Bedrock, update).
    health_probes: HealthProbes | None = None

    def host_json_path(self) -> Path | None:
        if self.host_json is not None:
            return self.host_json
        return None if self.data_dir is None else self.data_dir / "host.json"

    def __post_init__(self) -> None:
        from beepboop.api.enroll import EnrollmentHub
        from beepboop.api.ratelimit import LoginThrottle

        if self.enroll_hub is None:
            self.enroll_hub = EnrollmentHub(self.events)
        if self.throttle is None:
            self.throttle = LoginThrottle()

    def now_ms(self) -> int:
        return to_ms(self.clock())

    @property
    def secure_cookies(self) -> bool:
        return True


def get_deps(request: Request) -> ApiDeps:
    deps = getattr(request.app.state, "deps", None)
    if not isinstance(deps, ApiDeps):  # pragma: no cover - wiring error
        raise HTTPException(500, "api deps not configured")
    return deps


Deps = Annotated[ApiDeps, Depends(get_deps)]


@dataclass(frozen=True, slots=True)
class WebSession:
    principal: Principal
    profile: ProfileRow
    id_hash: str = ""

    @property
    def is_admin(self) -> bool:
        return self.profile.kind == "admin"


def ua_hash(user_agent: str | None) -> str:
    return hashlib.sha256((user_agent or "").encode()).hexdigest()[:16]


def web_principal(profile: ProfileRow, *, device: str, turn_id: str) -> Principal:
    """The one constructor of a web ``Principal`` (WEB_UI §6.3)."""
    return Principal(
        profile_id=profile.id,
        via="web",
        confidence=None,
        margin=None,
        device=device,
        scopes=ALL_SCOPES,
        tainted=False,
        turn_id=turn_id,
    )


async def current_session(
    deps: Deps,
    request: Request,
    bb_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> WebSession:
    """401 without a valid session. Touches ``last_seen_at`` on use (§6.2)."""
    if not bb_session:
        raise HTTPException(401, "no session")
    id_hash = sessions.hash_token(bb_session)
    row = await deps.db.read(lambda c: sessions.get(c, id_hash))
    now_ms = int(deps.clock().timestamp() * 1000)
    if row is None or row.kind != "web" or not row.is_valid(now_ms):
        raise HTTPException(401, "session expired")
    profile = await deps.db.read(lambda c: profiles.get(c, row.profile_id))
    if profile is None or profile.disabled_at is not None:
        raise HTTPException(401, "profile unavailable")
    await deps.db.write(lambda c: sessions.touch(c, id_hash, now_ms), durable=True)
    # The turn id is always server-made: a client-chosen id could point an approval (and a
    # later "not me") at someone else's turn.
    turn_id = f"web-{id_hash[:12]}-{now_ms}"
    principal = web_principal(profile, device=f"web:{ua_hash(user_agent)}", turn_id=turn_id)
    return WebSession(principal, profile, id_hash)


Session = Annotated[WebSession, Depends(current_session)]


async def require_admin(session: Session) -> WebSession:
    if not session.is_admin:
        raise HTTPException(403, "admin only")
    return session


Admin = Annotated[WebSession, Depends(require_admin)]


@dataclass(frozen=True, slots=True)
class ClaimSession:
    """The ``claim`` session (§5.1, §6.2): may only call ``/api/onboarding/*``."""

    id_hash: str
    mode: Literal["first_admin", "recovery"]


async def current_claim(
    deps: Deps,
    bb_claim: Annotated[str | None, Cookie(alias=CLAIM_COOKIE)] = None,
) -> ClaimSession:
    if not bb_claim:
        raise HTTPException(401, "no claim session")
    id_hash = sessions.hash_token(bb_claim)
    row = await deps.db.read(lambda c: sessions.get(c, id_hash))
    if row is None or row.kind != "claim" or not row.is_valid(deps.now_ms()):
        raise HTTPException(401, "claim expired")
    # After onboarding a claim session only accepts an admin recovery code (§5.1).
    has_admin = await deps.db.read(
        lambda c: c.execute("SELECT 1 FROM profiles WHERE kind = 'admin' LIMIT 1").fetchone()
    )
    return ClaimSession(id_hash, "recovery" if has_admin else "first_admin")


Claim = Annotated[ClaimSession, Depends(current_claim)]


def web_scope(profile: ProfileRow) -> set[WebScope]:
    """WEB_UI §6.3: the role table keyed on ``profiles.kind``."""
    if profile.kind == "admin":
        return {"admin", "self"}
    if profile.kind == "member":
        return {"self"}
    if profile.kind == "child":
        return {"child_readonly"}
    return set()


def can_view_profile(viewer: ProfileRow, target: ProfileRow) -> bool:
    """People pages: admins see everyone; everyone else only their own page (§3)."""
    return viewer.kind == "admin" or viewer.id == target.id


def can_manage_profile(viewer: ProfileRow, target: ProfileRow) -> bool:
    """Edit Profile/Voice fields: admins for anyone; members and children for themselves."""
    return viewer.kind == "admin" or (viewer.id == target.id and viewer.kind != "guest")


def csrf_violation(request: Request, public_url: str) -> str | None:
    """Non-GET ``/api`` needs ``Sec-Fetch-Site: same-origin`` and an exact ``Origin`` (§6.2).

    Returns the stable error code, or ``None`` when the request may proceed.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.headers.get("sec-fetch-site") != "same-origin":
        return "cross_site_request"
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") != public_url.rstrip("/"):
        return "bad_origin"
    return None


def set_cookie(response: Response, name: str, token: str, *, max_age: int, path: str = "/") -> None:
    """``__Host-``: ``Secure; HttpOnly; SameSite=Strict; Path=/``, no ``Domain`` (§6.2)."""
    response.set_cookie(
        name, token, max_age=max_age, path=path, secure=True, httponly=True, samesite="strict"
    )


def clear_cookie(response: Response, name: str, *, path: str = "/") -> None:
    response.delete_cookie(name, path=path, secure=True, httponly=True, samesite="strict")


async def record_audit(
    deps: ApiDeps,
    actor: ProfileRow | None,
    action: str,
    *,
    target: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    """One ``audit`` row for an admin or security action (IDENTITY_AND_TRUST §11)."""
    now = deps.now_ms()
    await deps.db.write(
        lambda c: audit.record(
            c,
            actor_id=None if actor is None else actor.id,
            actor_name="beepboop" if actor is None else actor.name,
            via="web",
            action=action,
            target=target,
            details=details,
            at=now,
        ),
        durable=True,
    )


__all__ = [
    "CLAIM_COOKIE",
    "SESSION_COOKIE",
    "Admin",
    "ApiDeps",
    "Claim",
    "ClaimSession",
    "Deps",
    "Session",
    "WebSession",
    "can_manage_profile",
    "can_view_profile",
    "clear_cookie",
    "csrf_violation",
    "current_claim",
    "current_session",
    "get_deps",
    "record_audit",
    "require_admin",
    "set_cookie",
    "ua_hash",
    "web_principal",
    "web_scope",
]
