"""A minimal Home Assistant WebSocket session: authenticate, then request/response.

This is the M0 subset used by headless onboarding (HOME_ASSISTANT §3). The
long-lived ``HAConnection`` with reconnects, subscriptions and the state cache
(HOME_ASSISTANT §4) is v0.1 work in ``client.py`` and should build on this.

Protocol (HA ``websocket_api``)::

    server: {"type": "auth_required", "ha_version": "2026.10.3"}
    client: {"type": "auth", "access_token": "<token>"}
    server: {"type": "auth_ok", "ha_version": …} | {"type": "auth_invalid", "message": …}
    client: {"id": 1, "type": "<command>", …payload}
    server: {"id": 1, "type": "result", "success": true, "result": …}
          | {"id": 1, "type": "result", "success": false, "error": {"code": …, "message": …}}

Access tokens are passed as :class:`pydantic.SecretStr` and never logged.
"""

from __future__ import annotations

import asyncio
import itertools
from types import TracebackType
from typing import Self, cast

import aiohttp
from pydantic import SecretStr
from structlog.typing import FilteringBoundLogger

from beepboop.log import get_logger


def _log() -> FilteringBoundLogger:
    """The module logger, looked up at call time (``beepboop.log.get_logger`` is lazy too)."""
    return get_logger(__name__)


DEFAULT_TIMEOUT_S = 5.0
type JsonObject = dict[str, object]


class HAWebSocketError(RuntimeError):
    """The socket closed, timed out or sent something we did not expect."""


class HAAuthInvalidError(HAWebSocketError):
    """HA answered ``auth_invalid``. Never retry in a loop (HOME_ASSISTANT §3)."""


class HACommandError(HAWebSocketError):
    """A command returned ``success: false``."""

    def __init__(self, command: str, code: str, message: str) -> None:
        super().__init__(f"{command} failed: {code}: {message}")
        self.command = command
        self.code = code


class HAWebSocket:
    """One authenticated WebSocket session. Use as ``async with HAWebSocket(...) as ws``."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        access_token: SecretStr,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._session = session
        self._url = url
        self._token = access_token
        self._timeout = timeout
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self.ha_version: str | None = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the socket and run the auth handshake."""
        self._ws = await self._session.ws_connect(self._url)
        hello = await self._receive()
        if hello.get("type") != "auth_required":
            msg = f"expected auth_required, got {hello.get('type')!r}"
            raise HAWebSocketError(msg)
        await self._send({"type": "auth", "access_token": self._token.get_secret_value()})
        reply = await self._receive()
        if reply.get("type") == "auth_invalid":
            await self.close()
            raise HAAuthInvalidError(str(reply.get("message", "auth_invalid")))
        if reply.get("type") != "auth_ok":
            msg = f"expected auth_ok, got {reply.get('type')!r}"
            raise HAWebSocketError(msg)
        version = reply.get("ha_version")
        self.ha_version = version if isinstance(version, str) else None
        _log().info("ha.ws.auth_ok", ha_version=self.ha_version)

    async def call(self, command: str, **payload: object) -> object:
        """Send one command and wait for its ``result`` (other frames are ignored)."""
        message_id = next(self._ids)
        await self._send({"id": message_id, "type": command, **payload})
        async with asyncio.timeout(self._timeout):
            while True:
                reply = await self._receive()
                if reply.get("id") != message_id or reply.get("type") != "result":
                    continue
                if reply.get("success") is True:
                    return reply.get("result")
                error = reply.get("error")
                details = cast("JsonObject", error) if isinstance(error, dict) else {}
                raise HACommandError(
                    command, str(details.get("code", "unknown")), str(details.get("message", ""))
                )

    async def close(self) -> None:
        """Close the socket if open."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def _send(self, message: JsonObject) -> None:
        if self._ws is None:
            msg = "not connected"
            raise HAWebSocketError(msg)
        await self._ws.send_json(message)

    async def _receive(self) -> JsonObject:
        if self._ws is None:
            msg = "not connected"
            raise HAWebSocketError(msg)
        try:
            frame = await self._ws.receive(timeout=self._timeout)
        except TimeoutError as exc:
            msg = "timed out waiting for Home Assistant"
            raise HAWebSocketError(msg) from exc
        if frame.type != aiohttp.WSMsgType.TEXT:
            msg = f"websocket closed or non-text frame ({frame.type.name})"
            raise HAWebSocketError(msg)
        data: object = frame.json()
        if not isinstance(data, dict):
            msg = "expected a JSON object from Home Assistant"
            raise HAWebSocketError(msg)
        return cast("JsonObject", data)
