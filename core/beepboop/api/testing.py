"""Test and dev harness for ``/api``: a real SQLite file, a real envelope SecretStore,
a movable clock and helpers to create people (used by ``core/tests/api`` and by the fake
core the UI's Playwright tests run against, ``beepboop.api.devserver``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from beepboop.api.app import create_app
from beepboop.api.deps import SESSION_COOKIE, ApiDeps
from beepboop.api.events import EventBus, PublishingApprovalService
from beepboop.api.people import CreatablePreset, create_profile
from beepboop.api.security import DekColumnSealer, Passwords, totp_code, totp_step
from beepboop.db import Database, repo
from beepboop.db.repo.approvals import DbApprovalStore
from beepboop.db.repo.policy_view import load_policy_state
from beepboop.module import Module, ToolContext, tool
from beepboop.policy import Principal
from beepboop.policy.authorize import PolicyState
from beepboop.policy.types import ALL_SCOPES
from beepboop.registry import RegisteredTool, Registry
from beepboop.registry.manifest import Manifest
from beepboop.secrets import Kek, SecretStore
from beepboop.testing import fake_context
from beepboop_protocol import new_ulid

PUBLIC = "https://beepboop.local"
TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "docs" / "module-template"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Origin": PUBLIC}
CHEAP = Passwords(memory_kib=8, time_cost=1, parallelism=1)
PASSWORD = "correct horse battery"  # noqa: S105 - a test fixture


class FakeContextFactory:
    def for_call(
        self, tool: RegisteredTool, principal: Principal, *, deadline: float | None, approval: Any
    ) -> ToolContext:
        # A live-looking context: ``dry_run`` is decided by the caller, not the fake.
        ctx = fake_context(TEMPLATE_DIR, profile=None, in_tool_call=True, principal=principal)
        return replace(ctx, dry_run=False)


@dataclass
class Clock:
    """Frozen at ``fixed`` (tests) or real time (``fixed=None``, the dev server), plus an
    offset that :meth:`advance` moves (to expire approvals without waiting a minute)."""

    fixed: datetime | None = field(default_factory=lambda: datetime.now(UTC))
    offset: timedelta = field(default_factory=timedelta)

    def __call__(self) -> datetime:
        return (self.fixed or datetime.now(UTC)) + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@dataclass
class Api:
    http: AsyncClient
    db: Database
    deps: ApiDeps
    clock: Clock
    data_dir: Path

    async def post(self, url: str, **kw: Any) -> Any:
        return await self.http.post(url, headers=SAME_ORIGIN, **kw)

    async def put(self, url: str, **kw: Any) -> Any:
        return await self.http.put(url, headers=SAME_ORIGIN, **kw)

    async def patch(self, url: str, **kw: Any) -> Any:
        return await self.http.patch(url, headers=SAME_ORIGIN, **kw)

    async def delete(self, url: str, **kw: Any) -> Any:
        return await self.http.delete(url, headers=SAME_ORIGIN, **kw)

    def use(self, token: str | None) -> None:
        self.http.cookies.clear()
        if token is not None:
            self.http.cookies.set(SESSION_COOKIE, token)

    async def person(
        self, name: str, preset: CreatablePreset = "member", *, login: bool = True
    ) -> tuple[str, str]:
        """Create a profile (+ login and a session); returns ``(profile_id, session_token)``."""
        row = await self.db.write(
            lambda c: create_profile(c, name, preset, created_by=None),
            durable=True,
        )
        token = f"tok-{row.id}"
        if login:
            await self.db.write(
                lambda c: repo.logins.insert(c, row.id, name.lower(), CHEAP.hash(PASSWORD)),
                durable=True,
            )
        await self.db.write(
            lambda c: repo.sessions.create(c, repo.sessions.hash_token(token), row.id),
            durable=True,
        )
        await self.deps.refresh_policy()
        return row.id, token

    def totp_now(self, secret: str, offset_steps: int = 0) -> str:
        return totp_code(secret, totp_step(self.clock()) + offset_steps)


async def build_api(
    data_dir: Path,
    *,
    public_url: str = PUBLIC,
    context_factory: Any = None,
    spa_dir: Path | None = None,
    loop: Any = None,
    frozen: bool = True,
    docs: bool = False,
) -> tuple[ApiDeps, Database, Clock, FastAPI]:
    """Open a database in ``data_dir`` and build the app with cheap password hashing."""
    db = await Database.open(data_dir / "beepboop.db", batch_window_s=0.0)
    store = await SecretStore.attach(db, Kek(kind="passphrase", version=1, key=b"k" * 32))
    clock = Clock() if frozen else Clock(fixed=None)
    box: list[PolicyState] = [await db.read(lambda c: load_policy_state(c, tz=UTC))]

    async def refresh() -> None:
        box[0] = await db.read(lambda c: load_policy_state(c, tz=UTC))

    bus = EventBus(clock=clock)
    approvals = PublishingApprovalService(DbApprovalStore(db), new_ulid, bus=bus)
    registry = Registry(
        lambda: box[0], approvals, context_factory or FakeContextFactory(), clock=clock
    )
    deps = ApiDeps(
        db=db,
        registry=registry,
        policy_state=lambda: box[0],
        public_url=public_url,
        clock=clock,
        version="test",
        refresh_policy=refresh,
        events=bus,
        passwords=CHEAP,
        sealer=DekColumnSealer(db, store),
        secrets=store,
        data_dir=data_dir,
        loop=loop,
    )
    return deps, db, clock, create_app(deps, spa_dir=spa_dir, docs=docs)


# --------------------------------------------------------------- fake modules --

FRONTDOOR_MANIFEST: dict[str, Any] = {
    "id": "frontdoor",
    "api": 1,
    "version": "0.1.0",
    "summary": "A pretend front-door lock for tests and the UI's fake core.",
    "scope": "household",
    "category": "home",
    "settings": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "title": "Door name", "default": "Front door"},
            "auto_lock_minutes": {
                "type": "integer", "title": "Auto-lock after", "minimum": 1, "maximum": 60,
                "default": 5, "x-advanced": True,
            },
        },
        "required": ["label"],
    },
    "secrets": [{"name": "api_key", "title": "API key"}],
    "tools": [
        {
            "name": "frontdoor.unlock",
            "description": "Unlock the front door.",
            "risk": "critical",
            "untrusted_output": False,
            "args": {"type": "object", "properties": {}, "additionalProperties": False},
            "output": {"type": "object", "properties": {"speak": {"type": "string"}},
                       "required": ["speak"]},
        },
        {
            "name": "frontdoor.status",
            "description": "Is the front door locked?",
            "risk": "read",
            "untrusted_output": False,
            "args": {"type": "object", "properties": {}, "additionalProperties": False},
            "output": {"type": "object", "properties": {"speak": {"type": "string"}},
                       "required": ["speak"]},
        },
    ],
}  # fmt: skip


class FrontDoor(Module):
    """Records what it did, so tests can assert "executed once"."""

    def __init__(self) -> None:
        self.unlocked = 0
        self.dry_runs: list[bool] = []

    @tool("frontdoor.unlock")
    async def unlock(self, ctx: ToolContext) -> dict[str, Any]:
        self.dry_runs.append(ctx.dry_run)
        if not ctx.dry_run:
            self.unlocked += 1
        return {"speak": "Unlocked."}

    @tool("frontdoor.status")
    async def status(self, ctx: ToolContext) -> dict[str, Any]:
        self.dry_runs.append(ctx.dry_run)
        return {"speak": "Locked." if self.unlocked == 0 else "Unlocked."}


def register_frontdoor(registry: Registry) -> FrontDoor:
    module = FrontDoor()
    registry.register_module(Manifest.model_validate(FRONTDOOR_MANIFEST), module, "# frontdoor\n")
    return module


def voice_principal(
    profile_id: str, *, confidence: float = 0.95, turn_id: str | None = None
) -> Principal:
    """A confident voice principal, as the audio bridge would build it."""
    return Principal(
        profile_id=profile_id,
        via="voice",
        confidence=confidence,
        margin=0.3,
        device="main",
        scopes=ALL_SCOPES,
        tainted=False,
        turn_id=turn_id or new_ulid(),
    )
