"""``HAConnection`` (HOME_ASSISTANT §4) against the fake HA WebSocket."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest
import structlog
from pydantic import SecretStr

from beepboop.ha.client import HAConnection, HAUnavailableError
from beepboop.ha.ws import HACommandError, JsonObject

from .fake_ws import GOOD_TOKEN, FakeHAWebSocket
from .rig import until


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


def _conn(
    session: aiohttp.ClientSession, url: str, token: str = GOOD_TOKEN, **kw: float
) -> HAConnection:
    async def get_token() -> SecretStr:
        return SecretStr(token)

    return HAConnection(
        session,
        url,
        get_token,
        backoff_start=kw.get("backoff", 0.01),
        backoff_max=0.05,
        ping_interval=kw.get("ping", 30.0),
    )


async def test_connect_auths_negotiates_coalescing_and_runs_hooks(
    session: aiohttp.ClientSession, ws_url: str, fake_ws: FakeHAWebSocket
) -> None:
    conn = _conn(session, ws_url)
    seen: list[str] = []

    async def hook(c: HAConnection) -> None:
        seen.append(str((await c.call("get_config"))["version"]))

    conn.on_connect(hook)
    await conn.connect()
    try:
        assert conn.connected
        assert conn.ha_version == "2026.10.3"
        assert fake_ws.commands[:2] == ["supported_features", "get_config"]
        assert fake_ws.coalesce is True
        assert seen == ["2026.10.3"]
    finally:
        await conn.close()
    assert conn.status == "closed"


async def test_events_reach_handlers_including_coalesced_frames(
    session: aiohttp.ClientSession, ws_url: str, fake_ws: FakeHAWebSocket
) -> None:
    conn = _conn(session, ws_url)
    got: list[JsonObject] = []
    conn.on("state_changed", got.append)
    await conn.connect()
    try:
        await conn.subscribe_events("state_changed")
        assert fake_ws.coalesce  # frames arrive as JSON arrays
        await fake_ws.fire("state_changed", {"entity_id": "light.x", "new_state": None})
        await until(lambda: len(got) == 1)
        assert got[0]["data"] == {"entity_id": "light.x", "new_state": None}
    finally:
        await conn.close()


async def test_command_error_and_not_connected(session: aiohttp.ClientSession, ws_url: str) -> None:
    conn = _conn(session, ws_url)
    with pytest.raises(HAUnavailableError):
        await conn.call("get_states")
    with pytest.raises(HAUnavailableError):
        await conn.call_service("light", "turn_on", None, {"entity_id": ["light.x"]})
    await conn.connect()
    try:
        with pytest.raises(HACommandError) as err:
            await conn.call("no/such_command")
        assert err.value.code == "unknown_command"
        result = await conn.call_service(
            "light", "turn_on", {}, {"entity_id": ["light.kitchen_ceiling"]}
        )
        assert result.context_id == "ctx-1"
    finally:
        await conn.close()


async def test_auth_invalid_is_terminal_never_a_retry_loop(
    session: aiohttp.ClientSession, ws_url: str, fake_ws: FakeHAWebSocket
) -> None:
    conn = _conn(session, ws_url, token="revoked")
    await asyncio.wait_for(conn.run(), timeout=3)
    assert conn.status == "reauth"
    assert fake_ws.auth_attempts == 1


async def test_reconnects_with_backoff_and_resyncs(
    session: aiohttp.ClientSession, ws_url: str, fake_ws: FakeHAWebSocket
) -> None:
    conn = _conn(session, ws_url)
    hooks: list[int] = []

    async def hook(c: HAConnection) -> None:
        hooks.append(c.connects)
        await c.subscribe_events("state_changed")

    conn.on_connect(hook)
    conn.start()
    try:
        assert await conn.wait_connected(3)
        await fake_ws.drop()
        await until(lambda: len(hooks) == 2 and fake_ws.subscribed("state_changed"))
        assert hooks == [1, 2]  # full resync after the reconnect
    finally:
        await conn.close()


async def test_two_missed_pongs_drop_the_socket(
    session: aiohttp.ClientSession, ws_url: str, fake_ws: FakeHAWebSocket
) -> None:
    fake_ws.answer_pings = False
    conn = _conn(session, ws_url, ping=0.05)
    conn.start()
    try:
        assert await conn.wait_connected(3)
        await until(lambda: conn.connects >= 2, within_s=5)
        assert fake_ws.commands.count("ping") >= 2
    finally:
        await conn.close()


async def test_token_is_never_logged(
    session: aiohttp.ClientSession, ws_url: str, fake_ws: FakeHAWebSocket
) -> None:
    with structlog.testing.capture_logs() as logs:
        conn = _conn(session, ws_url)
        await conn.connect()
        await fake_ws.drop()
        await until(lambda: not conn.connected)
        await conn.close()
    assert GOOD_TOKEN not in repr(logs)
    assert any(entry["event"] == "ha.ws.auth_ok" for entry in logs)
