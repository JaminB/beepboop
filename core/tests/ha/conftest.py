"""A fake Home Assistant for onboarding tests (no Docker on dev boxes or in unit CI).

It implements only the onboarding surface, and each check mirrors HA 2026.10.3
(``components/onboarding/views.py``, ``components/auth/__init__.py``,
``components/websocket_api/auth.py``): the step list and order, the required
``users`` fields, single-use auth codes bound to ``client_id``, Bearer auth on
the later steps, the integration step's redirect-URI check, and the WS auth
handshake plus ``auth/long_lived_access_token``, and refresh-token revocation
(``/auth/token`` ``action=revoke``). The real container is checked
by V2 on the Pi and by the v0.1 bridge contract tests (HOME_ASSISTANT §13).
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import aiohttp
import pytest
import structlog
from aiohttp import web
from aiohttp.test_utils import TestServer

from beepboop.secrets import Argon2Params, SecretStore, create_passphrase_kek

from .fake_ws import FakeHAWebSocket, serve
from .rig import Rig, build_rig

STEPS = ["user", "core_config", "analytics", "integration"]
HA_VERSION = "2026.10.3"


@dataclass
class FakeHA:
    """State of the fake. Tests inspect it after a run."""

    done: list[str] = field(default_factory=list[str])
    users: dict[str, str] = field(default_factory=dict[str, str])  # username -> password
    auth_codes: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    access_tokens: set[str] = field(default_factory=set[str])
    refresh_tokens: set[str] = field(default_factory=set[str])
    llats: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    revoked_refresh_tokens: set[str] = field(default_factory=set[str])
    requests: list[str] = field(default_factory=list[str])
    #: Answer 503 to this many ``GET /api/onboarding`` calls first (HA still starting).
    not_ready_polls: int = 0
    #: Make ``/auth/token`` reject every code.
    reject_codes: bool = False
    #: Make WS auth answer ``auth_invalid``.
    reject_ws_auth: bool = False
    #: Finish the user step just before our POST arrives (a racing human).
    race_user_step: bool = False
    #: Refuse ``auth/long_lived_access_token`` on a connection authenticated with an LLAT.
    refuse_llat_on_llat: bool = False
    #: Answer 500 to ``/auth/token`` ``action=revoke``.
    fail_revoke: bool = False
    _access_by_refresh: dict[str, set[str]] = field(default_factory=dict[str, set[str]])

    def issued_secrets(self) -> list[str]:
        """Every secret value the fake handed out or received."""
        values = [*self.users.values(), *self.access_tokens, *self.refresh_tokens]
        values += [*self.revoked_refresh_tokens]
        values += [code for (_client, code) in self.auth_codes]
        values += [str(llat["token"]) for llat in self.llats]
        return values

    # ------------------------------------------------------------- views --

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/onboarding", self.status)
        app.router.add_post("/api/onboarding/users", self.users_step)
        app.router.add_post("/auth/token", self.token)
        app.router.add_post("/api/onboarding/core_config", self.simple_step("core_config"))
        app.router.add_post("/api/onboarding/analytics", self.simple_step("analytics"))
        app.router.add_post("/api/onboarding/integration", self.integration_step)
        app.router.add_get("/api/websocket", self.websocket)
        return app

    async def status(self, request: web.Request) -> web.Response:
        self.requests.append("GET /api/onboarding")
        if self.not_ready_polls > 0:
            self.not_ready_polls -= 1
            return web.Response(status=503, text="starting")
        return web.json_response([{"step": s, "done": s in self.done} for s in STEPS])

    async def users_step(self, request: web.Request) -> web.Response:
        self.requests.append("POST /api/onboarding/users")
        data = cast("dict[str, object]", await request.json())
        required = {"name", "username", "password", "client_id", "language"}
        if set(data) != required or not all(isinstance(v, str) for v in data.values()):
            return web.json_response({"message": "invalid body"}, status=400)
        if self.race_user_step or "user" in self.done:
            return web.json_response({"message": "User step already done"}, status=403)
        username = str(data["username"])
        if username != username.strip().casefold():
            return web.json_response({"message": "invalid username"}, status=400)
        self.users[username] = str(data["password"])
        self.done.append("user")
        code = secrets.token_hex(16)
        self.auth_codes[(str(data["client_id"]), code)] = username
        return web.json_response({"auth_code": code})

    async def token(self, request: web.Request) -> web.Response:
        self.requests.append("POST /auth/token")
        form = await request.post()
        if form.get("action") == "revoke" and self.fail_revoke:
            return web.Response(status=500, text="500: Internal Server Error")
        if form.get("action") == "revoke":
            # HA: always 200; a known refresh token is removed with its access tokens.
            token = str(form.get("token", ""))
            if token in self.refresh_tokens:
                self.refresh_tokens.discard(token)
                self.revoked_refresh_tokens.add(token)
                self.access_tokens -= self._access_by_refresh.pop(token, set())
            return web.Response(status=200)
        client_id = str(form.get("client_id", ""))
        if form.get("grant_type") != "authorization_code":
            return web.json_response({"error": "unsupported_grant_type"}, status=400)
        if urlsplit(client_id).scheme not in ("http", "https"):
            return web.json_response(
                {"error": "invalid_request", "error_description": "Invalid client id"}, status=400
            )
        key = (client_id, str(form.get("code", "")))
        if self.reject_codes or key not in self.auth_codes:
            return web.json_response(
                {"error": "invalid_request", "error_description": "Invalid code"}, status=400
            )
        self.auth_codes.pop(key)  # single use, like HA
        self.auth_codes[("used", key[1])] = "used"  # keep for the leak checks
        access, refresh = secrets.token_hex(32), secrets.token_hex(32)
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        self._access_by_refresh[refresh] = {access}
        return web.json_response(
            {
                "access_token": access,
                "token_type": "Bearer",
                "refresh_token": refresh,
                "expires_in": 1800,
                "ha_auth_provider": "homeassistant",
            }
        )

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        return header.startswith("Bearer ") and header[7:] in self.access_tokens

    def simple_step(self, step: str):
        async def handler(request: web.Request) -> web.Response:
            self.requests.append(f"POST /api/onboarding/{step}")
            if not self._authorized(request):
                return web.Response(status=401, text="401: Unauthorized")
            if step in self.done:
                return web.json_response({"message": f"{step} already done"}, status=403)
            self.done.append(step)
            return web.json_response({})

        return handler

    async def integration_step(self, request: web.Request) -> web.Response:
        self.requests.append("POST /api/onboarding/integration")
        if not self._authorized(request):
            return web.Response(status=401, text="401: Unauthorized")
        data = cast("dict[str, object]", await request.json())
        if "integration" in self.done:
            return web.json_response({"message": "Integration step already done"}, status=403)
        self.done.append("integration")  # HA marks it done before validating, too
        client = urlsplit(str(data.get("client_id", "")))
        redirect = urlsplit(str(data.get("redirect_uri", "")))
        if (client.scheme, client.netloc) != (redirect.scheme, redirect.netloc):
            return web.json_response({"message": "invalid client id or redirect uri"}, status=400)
        return web.json_response({"auth_code": secrets.token_hex(16)})

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        self.requests.append("WS /api/websocket")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": HA_VERSION})
        auth = cast("dict[str, object]", await ws.receive_json())
        if self.reject_ws_auth or auth.get("access_token") not in self.access_tokens:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": HA_VERSION})
        on_llat = any(llat["token"] == auth.get("access_token") for llat in self.llats)
        async for frame in ws:
            if frame.type != aiohttp.WSMsgType.TEXT:
                break
            msg = cast("dict[str, object]", frame.json())
            self.requests.append(f"WS {msg['type']}")
            await ws.send_json(self._command(msg, on_llat=on_llat))
        return ws

    def _command(self, msg: dict[str, object], *, on_llat: bool) -> dict[str, object]:
        msg_id = msg["id"]
        if msg["type"] == "auth/long_lived_access_token":
            lifespan, name = msg.get("lifespan"), msg.get("client_name")
            if not isinstance(lifespan, int) or not isinstance(name, str):
                return _ws_error(msg_id, "invalid_format", "lifespan and client_name required")
            if on_llat and self.refuse_llat_on_llat:
                return _ws_error(msg_id, "unauthorized", "not on a long-lived session")
            token = _fake_jwt(lifespan)
            self.access_tokens.add(token)
            self.llats.append({"token": token, "lifespan": lifespan, "client_name": name})
            return {"id": msg_id, "type": "result", "success": True, "result": token}
        if msg["type"] == "auth/refresh_tokens":
            listing = [
                {
                    "id": f"rt{index}",
                    "type": "long_lived_access_token",
                    "client_name": llat["client_name"],
                }
                for index, llat in enumerate(self.llats)
            ]
            return {"id": msg_id, "type": "result", "success": True, "result": listing}
        if msg["type"] == "auth/delete_refresh_token":
            index = int(str(msg["refresh_token_id"]).removeprefix("rt"))
            self.access_tokens.discard(str(self.llats[index]["token"]))
            self.llats[index] = {**self.llats[index], "client_name": "<deleted>"}
            return {"id": msg_id, "type": "result", "success": True, "result": {}}
        return _ws_error(msg_id, "unknown_command", "Unknown command.")


def _fake_jwt(lifespan_days: int) -> str:
    """An HS256-shaped token whose payload carries ``iat``/``exp`` like HA's."""

    def b64(data: Mapping[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    iat = 1_700_000_000
    payload = {"iss": secrets.token_hex(16), "iat": iat, "exp": iat + lifespan_days * 86400}
    return f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(payload)}.{secrets.token_hex(16)}"


def _ws_error(msg_id: object, code: str, message: str) -> dict[str, object]:
    return {
        "id": msg_id,
        "type": "result",
        "success": False,
        "error": {"code": code, "message": message},
    }


# ---------------------------------------------------------------- fixtures --


@pytest.fixture(autouse=True)
def _isolate_structlog() -> Iterator[None]:
    """Other suites configure structlog globally (sometimes onto a stream that pytest
    later closes); start and end every test here from structlog's defaults."""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


@pytest.fixture
def fake_ha() -> FakeHA:
    return FakeHA()


@pytest.fixture
async def ha_url(fake_ha: FakeHA) -> AsyncIterator[str]:
    server = TestServer(fake_ha.app())
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


@pytest.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SecretStore]:
    salt = secrets.token_bytes(32)
    cheap = Argon2Params(time_cost=1, memory_cost_kib=64, parallelism=1)
    kek = create_passphrase_kek(tmp_path / "kek", "test passphrase", salt, params=cheap)
    secret_store = await SecretStore.open(tmp_path / "secrets.db", kek)
    try:
        yield secret_store
    finally:
        await secret_store.close()


# ------------------------------------------------- v0.1: the WebSocket stack --


@pytest.fixture
def fake_ws() -> FakeHAWebSocket:
    return FakeHAWebSocket()


@pytest.fixture
async def ws_url(fake_ws: FakeHAWebSocket) -> AsyncIterator[str]:
    async for url in serve(fake_ws):
        yield url


@pytest.fixture
async def rig(tmp_path: Path, ws_url: str, fake_ws: FakeHAWebSocket) -> AsyncIterator[Rig]:
    async for built in build_rig(tmp_path, ws_url, fake_ws):
        yield built
