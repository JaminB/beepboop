"""A full HA stack over the fake WebSocket: database, policy snapshot, ``HAService``,
registry with the real ``ha`` module loaded from ``modules/ha``.

Profiles (ids double as ``fake_context`` default profile names): ``admin`` (Alex),
``member`` (Sam), ``child`` (Robin) and the built-in ``guest``, each with its preset.
Grants: ``group:household`` gets the kitchen, living room and hall areas at ``act``;
``group:adults`` gets the front door and garage at ``critical`` and the scene and script at
``critical``; Sam alone gets the bedroom lamp and sensor. Everything is dispositioned
``active`` except the kids-room light, which stays in the inbox (``new``).
"""

from __future__ import annotations

import asyncio
import itertools
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from pydantic import SecretStr

from beepboop.db import Database, repo
from beepboop.db.repo.policy_view import load_policy_state
from beepboop.ha.client import HAConnection
from beepboop.ha.service import HAService
from beepboop.module.context import ToolContext
from beepboop.policy import (
    ALL_SCOPES,
    ApprovalService,
    Grant,
    MemoryApprovalStore,
    PolicyState,
    Principal,
    apply_preset,
)
from beepboop.policy.types import Approval
from beepboop.registry import RegisteredTool, Registry, load_manifest
from beepboop.registry.loader import import_module_file
from beepboop.registry.registry import ListAuditSink
from beepboop.testing import fake_context

from .fake_ws import GOOD_TOKEN, FakeHAWebSocket

REPO = Path(__file__).resolve().parents[3]
HA_MODULE = REPO / "modules" / "ha"
STILL_NEW = frozenset({"u-kids-light"})


def seed(conn: sqlite3.Connection) -> None:
    ids = (f"g{i}" for i in itertools.count())
    people = (("Alex", "admin"), ("Sam", "member"), ("Robin", "child"))
    for name, kind in people:
        ceiling = "act" if kind == "child" else "critical"
        repo.profiles.insert(conn, name, kind, ceiling, profile_id=kind)  # type: ignore[arg-type]
    for pid, preset in (
        ("admin", "admin"),
        ("member", "member"),
        ("child", "child"),
        ("guest", "guest"),
    ):
        rows = apply_preset(preset, pid, lambda: next(ids))
        for g in rows.group_ids:
            repo.profiles.add_to_group(conn, pid, g)
        repo.grants.insert_many(conn, rows.grants)
    extra = [
        Grant(next(ids), "group", "household", "ha_area", "kitchen", "act"),
        Grant(next(ids), "group", "household", "ha_area", "living_room", "act"),
        Grant(next(ids), "group", "household", "ha_area", "hall", "act"),
        Grant(next(ids), "group", "adults", "ha_entity", "u-front-door", "critical"),
        Grant(next(ids), "group", "adults", "ha_entity", "u-garage", "critical"),
        Grant(next(ids), "group", "adults", "ha_entity", "u-movie", "critical"),
        Grant(next(ids), "group", "adults", "ha_entity", "u-away-script", "critical"),
        Grant(next(ids), "profile", "member", "ha_entity", "u-bedroom-lamp", "act"),
        Grant(next(ids), "profile", "member", "ha_entity", "u-bedroom-temp", "read"),
        Grant(next(ids), "profile", "child", "ha_entity", "u-kids-light", "act"),
    ]
    repo.grants.insert_many(conn, extra)
    for pid in ("admin", "member"):  # a web login is what offers the "web" second factor
        repo.logins.insert(conn, pid, pid, "$argon2id$fixture")


def principal(pid: str, *, tainted: bool = False, confidence: float = 0.95) -> Principal:
    return Principal(pid, "voice", confidence, 0.3, "main", ALL_SCOPES, tainted, f"T-{pid}")


@dataclass
class Rig:
    db: Database
    fake: FakeHAWebSocket
    ha: HAService
    conn: HAConnection
    registry: Registry
    audit: ListAuditSink
    box: list[PolicyState] = field(default_factory=list[PolicyState])

    async def refresh(self) -> None:
        self.box[0] = await self.db.read(lambda c: load_policy_state(c, tz=UTC))

    async def disposition(self, uuids: Iterable[str], status: str = "active") -> None:
        wanted = list(uuids)

        def write(c: sqlite3.Connection) -> None:
            for u in wanted:
                repo.ha_entities.set_status(c, u, status)  # type: ignore[arg-type]

        await self.db.write(write, durable=True)
        await self.refresh()

    def state(self) -> PolicyState:
        return self.box[0]


class RigContexts:
    """``ContextFactory``: a dry-run fake context whose ``ha`` is the real policy wrapper."""

    def __init__(self, ha: HAService) -> None:
        self.ha = ha

    def for_call(
        self,
        tool: RegisteredTool,
        principal: Principal,
        *,
        deadline: float | None,
        approval: Approval | None,
    ) -> ToolContext:
        ctx = fake_context(HA_MODULE, profile=principal.profile_id)
        return replace(
            ctx,
            principal=principal,
            approval=approval,
            deadline=deadline,
            ha=self.ha.client_for_call(principal, tool, approval=approval, deadline=deadline),
        )


async def build_rig(tmp_path: Path, url: str, fake: FakeHAWebSocket) -> AsyncIterator[Rig]:
    db = await Database.open(tmp_path / "bb.db", backups_dir=tmp_path / "backups")
    await db.write(seed, durable=True)
    box: list[PolicyState] = [await db.read(lambda c: load_policy_state(c, tz=UTC))]

    async def refresh() -> None:
        box[0] = await db.read(lambda c: load_policy_state(c, tz=UTC))

    ha = HAService(db, lambda: box[0], refresh, debounce_s=0.01)
    session = aiohttp.ClientSession()

    async def token() -> SecretStr:
        return SecretStr(GOOD_TOKEN)

    conn = HAConnection(session, url, token, backoff_start=0.01, backoff_max=0.05)
    ha.attach(conn)
    await conn.connect()
    audit = ListAuditSink()
    ids = (f"ap{i}" for i in itertools.count())
    registry = Registry(
        policy_state=lambda: box[0],
        approvals=ApprovalService(MemoryApprovalStore(), lambda: next(ids)),
        context_factory=RigContexts(ha),
        audit=audit,
        targets=ha.extractor,
        clock=lambda: datetime.now(UTC),
    )
    module_class = import_module_file(HA_MODULE)
    registry.register_module(
        load_manifest(HA_MODULE), module_class(), (HA_MODULE / "AGENT.md").read_text("utf-8")
    )
    rig = Rig(db, fake, ha, conn, registry, audit, box)
    all_uuids = [str(e["id"]) for e in fake.entities]
    await rig.disposition(u for u in all_uuids if u not in STILL_NEW)
    try:
        yield rig
    finally:
        await ha.stop()
        await session.close()
        await db.close()


def calls(fake: FakeHAWebSocket) -> list[tuple[str, str, list[str], dict[str, Any]]]:
    return [
        (c["domain"], c["service"], sorted(c["target"]["entity_id"]), c.get("service_data", {}))
        for c in fake.calls
    ]


async def until(predicate: Callable[[], bool], within_s: float = 3.0) -> None:
    """Poll ``predicate`` (events arrive on the reader task; there is nothing to await)."""
    async with asyncio.timeout(within_s):
        while not predicate():  # noqa: ASYNC110 - polling fake state, not a coordination point
            await asyncio.sleep(0.01)
