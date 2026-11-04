"""Boot a real core for the system tests: first-boot seeding, then ``CoreService.start``.

The seed is what the installer and onboarding leave behind: the passphrase-wrapped KEK,
two people (Alex, admin; Sam, member) with web logins and sessions, HA area grants, the
HA long-lived token in the secret store and, optionally, Alex's sealed voiceprint for the
connected speaker model. HA is the shared fake WebSocket; the admin dispositions every
mirrored entity ``active`` (the new-device inbox) before the first turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from beepboop.api.people import create_profile
from beepboop.api.security import DekColumnSealer, column_aad
from beepboop.audio_bridge import pack_centroids
from beepboop.db import Database, repo
from beepboop.db.repo.voiceprints import DEFAULT_CALIB, VoiceprintRow
from beepboop.ha.onboarding import HA_TOKEN_SECRET
from beepboop.policy import Grant
from beepboop.service import CoreConfig, CoreService, audio_client, open_secrets
from beepboop.testing.fake_ha import GOOD_TOKEN, FakeHAWebSocket, serve
from beepboop_protocol import TcpAddress, new_ulid

REPO = Path(__file__).resolve().parents[2]
PUBLIC = "https://beepboop.local"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Origin": PUBLIC}
SESSION_COOKIE = "__Host-bb_session"
WAIT_S = 15.0


@dataclass(frozen=True, slots=True)
class Voiceprint:
    model_id: str
    model_sha: str
    centroid: Sequence[float]


@dataclass
class Booted:
    service: CoreService
    ha: FakeHAWebSocket
    alex: str
    sam: str
    tokens: dict[str, str]

    def ha_calls(self) -> list[tuple[str, str, list[str]]]:
        return [
            (c["domain"], c["service"], sorted(c["target"]["entity_id"])) for c in self.ha.calls
        ]

    async def turn_row(self, turn_id: str) -> repo.turns.TurnRow:
        """The ``turns`` row once the turn has finished (``finish`` writes the stages)."""
        db = self.service.db
        async with asyncio.timeout(WAIT_S):
            while True:
                row = await db.read(lambda c: repo.turns.get_turn(c, turn_id))
                if row is not None and ("fastpath" in row.stages_ms or row.error):
                    return row
                await asyncio.sleep(0.02)

    async def tool_calls(self, turn_id: str) -> list[repo.turns.ToolCallRow]:
        return await self.service.db.read(lambda c: repo.turns.tool_calls_for(c, turn_id))


def seed(conn: sqlite3.Connection) -> tuple[str, str, dict[str, str]]:
    alex = create_profile(conn, "Alex", "admin", created_by=None)
    sam = create_profile(conn, "Sam", "member", created_by=None)
    tokens: dict[str, str] = {}
    for row in (alex, sam):
        repo.logins.insert(conn, row.id, row.name.lower(), "$argon2id$fixture")
        token = f"tok-{row.id}"
        repo.sessions.create(conn, repo.sessions.hash_token(token), row.id)
        tokens[row.id] = token
    repo.grants.insert_many(
        conn,
        [
            Grant(new_ulid(), "group", "household", "ha_area", "kitchen", "act"),
            Grant(new_ulid(), "group", "household", "ha_area", "living_room", "act"),
            Grant(new_ulid(), "group", "adults", "ha_entity", "u-front-door", "critical"),
        ],
    )
    return alex.id, sam.id, tokens


async def until(predicate: Callable[[], Awaitable[bool]], wait_s: float = WAIT_S) -> None:
    async with asyncio.timeout(wait_s):
        while not await predicate():  # noqa: ASYNC110 - polling another task's state
            await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def boot_core(
    tmp_path: Path, audio: TcpAddress, voiceprint: Voiceprint | None
) -> AsyncGenerator[Booted]:
    """Seed, start core against ``audio`` and a fake HA, connect, yield; then shut down."""
    fake_ha = FakeHAWebSocket()
    async with contextlib.asynccontextmanager(serve)(fake_ha) as ha_url:
        passphrase = tmp_path / "kek.passphrase"
        passphrase.write_text("correct horse battery staple\n", encoding="utf-8")
        config = CoreConfig(
            data_dir=tmp_path / "data",
            modules_dir=REPO / "modules",
            public_url=PUBLIC,
            kek_passphrase_file=passphrase,
            audio_socket=f"tcp://{audio.host}:{audio.port}",
            ha_url=ha_url,
        )
        db = await Database.open(config.db_path, backups_dir=config.backups_path)
        secrets = await open_secrets(db, config)
        alex, sam, tokens = await db.write(seed, durable=True)
        await secrets.put(HA_TOKEN_SECRET, SecretStr(GOOD_TOKEN))
        if voiceprint is not None:
            sealed = await DekColumnSealer(db, secrets).seal(
                column_aad("voiceprints", alex, "centroids_enc"),
                pack_centroids([voiceprint.centroid]),
            )
            row = VoiceprintRow(
                profile_id=alex,
                model_id=voiceprint.model_id,
                model_sha=voiceprint.model_sha,
                dim=len(voiceprint.centroid),
                centroids_enc=sealed,
                k=1,
                n_clips=6,
                calib=dict(DEFAULT_CALIB),
                consent_at=1,
                consent_version="v1",
            )
            await db.write(lambda c: repo.voiceprints.upsert(c, row), durable=True)
        await secrets.close()
        await db.close()

        service = await CoreService.start(config)
        client_task: asyncio.Task[None] | None = None
        try:
            ha = service.ha
            assert ha is not None
            db_ = service.db

            async def mirrored() -> bool:
                rows = await db_.read(repo.ha_entities.list_)
                return ha.health == "ok" and len(rows) >= len(fake_ha.entities) - 1

            await until(mirrored)

            def activate(c: sqlite3.Connection) -> None:
                for entity in repo.ha_entities.list_(c):
                    repo.ha_entities.set_status(c, entity.uuid, "active")

            await service.db.write(activate, durable=True)
            await service.refresh_policy()
            client = audio_client(service)
            assert client is not None
            client_task = asyncio.create_task(client.run())
            yield Booted(service, fake_ha, alex, sam, tokens)
        finally:
            if client_task is not None:
                client_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await client_task
            await service.stop()
