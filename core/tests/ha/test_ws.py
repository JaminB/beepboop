"""HAWebSocket protocol edges not covered by the onboarding run."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import SecretStr

from beepboop.ha.ws import HACommandError, HAWebSocket, HAWebSocketError

Script = Callable[[web.WebSocketResponse], Awaitable[None]]


@pytest.fixture
async def serve() -> AsyncIterator[Callable[[Script], Awaitable[str]]]:
    servers: list[TestServer] = []

    async def start(script: Script) -> str:
        async def handler(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await script(ws)
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/api/websocket", handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return str(server.make_url("/api/websocket"))

    yield start
    for server in servers:
        await server.close()


async def _authed(ws: web.WebSocketResponse) -> None:
    await ws.send_json({"type": "auth_required", "ha_version": "2026.10.3"})
    await ws.receive_json()
    await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.3"})


async def test_error_result_raises_command_error(
    serve: Callable[[Script], Awaitable[str]],
) -> None:
    async def script(ws: web.WebSocketResponse) -> None:
        await _authed(ws)
        msg = await ws.receive_json()
        await ws.send_json({"id": 999, "type": "event", "event": {}})  # unrelated frame: skipped
        await ws.send_json(
            {
                "id": msg["id"],
                "type": "result",
                "success": False,
                "error": {"code": "unauthorized", "message": "nope"},
            }
        )
        await ws.receive()

    url = await serve(script)
    async with aiohttp.ClientSession() as session, HAWebSocket(session, url, SecretStr("t")) as ha:
        assert ha.ha_version == "2026.10.3"
        with pytest.raises(HACommandError) as caught:
            await ha.call("auth/long_lived_access_token", lifespan=1, client_name="x")
    assert caught.value.code == "unauthorized"


async def test_unexpected_hello_is_rejected(serve: Callable[[Script], Awaitable[str]]) -> None:
    async def script(ws: web.WebSocketResponse) -> None:
        await ws.send_json({"type": "something_else"})

    url = await serve(script)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(HAWebSocketError, match="auth_required"):
            await HAWebSocket(session, url, SecretStr("t")).connect()


async def test_server_closing_mid_call_is_an_error(
    serve: Callable[[Script], Awaitable[str]],
) -> None:
    async def script(ws: web.WebSocketResponse) -> None:
        await _authed(ws)
        await ws.receive_json()

    url = await serve(script)
    async with aiohttp.ClientSession() as session, HAWebSocket(session, url, SecretStr("t")) as ha:
        with pytest.raises(HAWebSocketError, match="closed"):
            await ha.call("ping")
