"""``CoreService.start`` boots on x86 with a scaffolded module and no hardware."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from beepboop.agent import Utterance
from beepboop.api.people import create_profile
from beepboop.api.testing import build_api, register_frontdoor, voice_principal
from beepboop.db import repo
from beepboop.policy import ALL_SCOPES, Principal
from beepboop.policy.errors import NeedsConfirmation
from beepboop.registry.cli import scaffold
from beepboop.service import CoreConfig, CoreService, expire_approvals


async def test_service_boots_discovers_modules_and_serves_api(tmp_path: Path) -> None:
    modules = tmp_path / "modules"
    scaffold("parcel_tracker", modules_dir=modules)
    (modules / "broken").mkdir()
    (modules / "broken" / "manifest.yaml").write_text("id: broken\n", encoding="utf-8")
    config = CoreConfig(
        data_dir=tmp_path / "data", modules_dir=modules, public_url="https://bb.local"
    )
    service = await CoreService.start(config)
    try:
        assert [m.id for m, _ in service.registry.modules()] == ["parcel_tracker"]
        assert "broken" in service.modules_errors
        names = {t.name for t in service.registry.tools()}
        assert {"find_tools", "read_guide", "parcel_tracker.track"} <= names
        assert service.index.search("where is my package")[0].name == "parcel_tracker.track"
        async with AsyncClient(
            transport=ASGITransport(app=service.app), base_url="https://bb.local"
        ) as http:
            r = await http.get("/api/health")
            assert r.status_code == 200
        guest = Principal("guest", "voice", 0.3, 0.0, "main", ALL_SCOPES, False, "T1")
        result = await service.loop.handle(Utterance("what time is it", guest))
        assert result.path == "fast"
        assert result.speech[0].text is not None
        offline = await service.loop.handle(Utterance("tell me a joke", guest))
        assert offline.path == "offline"
    finally:
        await service.stop()


def test_config_from_env(tmp_path: Path) -> None:
    cfg = CoreConfig.from_env(
        {"BB_DATA_DIR": str(tmp_path), "BB_BIND_PORT": "9000", "BB_TZ": "Europe/Berlin"}
    )
    assert cfg.db_path == tmp_path / "beepboop.db"
    assert cfg.bind_port == 9000
    assert str(cfg.tz) == "Europe/Berlin"


async def test_expiry_sweep_marks_and_publishes_late_approvals(tmp_path: Path) -> None:
    deps, db, clock, _app = await build_api(tmp_path)
    try:
        register_frontdoor(deps.registry)

        def seed(c: sqlite3.Connection) -> str:
            pid = create_profile(c, "Alex", "admin", created_by=None).id
            repo.logins.insert(c, pid, "alex", "$argon2id$fixture")  # the web factor
            return pid

        alex = await db.write(seed, durable=True)
        await deps.refresh_policy()
        with pytest.raises(NeedsConfirmation) as raised:
            await deps.registry.dispatch(voice_principal(alex), "frontdoor.unlock", {})
        approval_id = raised.value.approval.id
        waiter = asyncio.create_task(deps.events.wait_decision(approval_id, 5))
        clock.advance(61)
        sweep = asyncio.create_task(expire_approvals(deps, every_s=0.01))
        decided = await asyncio.wait_for(waiter, 5)
        sweep.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep
        assert decided is not None
        assert decided.decision == "expired"
    finally:
        await db.close()


async def test_saved_integration_settings_reach_ctx_settings(tmp_path: Path) -> None:
    modules = tmp_path / "modules"
    scaffold("parcel_tracker", modules_dir=modules)
    service = await CoreService.start(CoreConfig(data_dir=tmp_path / "d", modules_dir=modules))
    try:
        sam = await service.db.write(
            lambda c: create_profile(c, "Sam", "member", created_by=None).id, durable=True
        )

        def save(c: sqlite3.Connection) -> None:
            repo.integrations.upsert(
                c, "parcel_tracker", "household", None, settings={"region": "eu", "n": 1}
            )
            repo.integrations.upsert(c, "parcel_tracker", "profile", sam, settings={"n": 2})

        await service.db.write(save, durable=True)
        assert "region" not in service.contexts.for_hook("parcel_tracker").settings
        await service.refresh_policy()  # what the integrations router calls after a save
        assert service.contexts.for_hook("parcel_tracker").settings["region"] == "eu"
        mine = service.contexts.for_hook("parcel_tracker", sam).settings
        assert (mine["region"], mine["n"]) == ("eu", 2)
    finally:
        await service.stop()


def test_serve_options_keep_the_rest_of_the_environment(tmp_path: Path) -> None:
    from beepboop.cli import serve_config

    env = {
        "BB_AUDIO_SOCKET": "tcp://127.0.0.1:7000",
        "BB_HA_URL": "ws://127.0.0.1:9999/api/websocket",
        "BB_HOST_JSON": "/hostrun/host.json",
        "BB_SPA_DIR": "/app/spa",
        "BB_BACKUPS_DIR": "/backups",
    }
    cfg = serve_config(tmp_path, None, "127.0.0.1", 8081, env)
    assert (cfg.data_dir, cfg.bind_port) == (tmp_path, 8081)
    assert cfg.audio_socket == "tcp://127.0.0.1:7000"
    assert cfg.ha_url.endswith(":9999/api/websocket")
    assert cfg.host_json == Path("/hostrun/host.json")
    assert cfg.spa_dir == Path("/app/spa")
    assert cfg.backups_dir == Path("/backups")
    assert cfg.audio_address is not None


def test_should_flag_follows_identity_11() -> None:
    """IDENTITY_AND_TRUST §11 ``flagged``: personal+ below τ_strict, ceiling/impersonation
    denials and tainted non-read executions; nothing else."""
    from beepboop.policy import (
        ALL_SCOPES,
        Decision,
        GrantIndex,
        PolicyState,
        Principal,
        ProfileView,
    )
    from beepboop.registry.registry import ToolCallAudit
    from beepboop.service import should_flag

    state = PolicyState(
        profiles={"alex": ProfileView("alex", "admin", "critical")}, grants=GrantIndex()
    )

    def entry(
        decision: Decision,
        *,
        conf: float | None = 0.95,
        tainted: bool = False,
        status: str = "ok",
    ) -> ToolCallAudit:
        p = Principal("alex", "voice", conf, 0.3, "main", ALL_SCOPES, tainted, "T1")
        return ToolCallAudit("T1", p, "caldav.events", "caldav", {}, [], decision, status, False)

    assert should_flag(entry(Decision("allow", "personal"), conf=0.80), state) is True
    assert should_flag(entry(Decision("allow", "personal"), conf=0.95), state) is False
    assert should_flag(entry(Decision("allow", "act"), conf=0.50), state) is False
    assert should_flag(entry(Decision("deny", "critical", reason="ceiling")), state) is True
    assert should_flag(entry(Decision("deny", "act", reason="impersonation")), state) is True
    assert should_flag(entry(Decision("deny", "act", reason="no_grant")), state) is False
    assert should_flag(entry(Decision("allow", "act"), tainted=True), state) is True
    assert should_flag(entry(Decision("allow", "read"), tainted=True), state) is False
    denied = entry(Decision("allow", "act"), tainted=True, status="denied")
    assert should_flag(denied, state) is False
