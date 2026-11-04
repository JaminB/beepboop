"""``HAConnection``: the one persistent Home Assistant WebSocket (HOME_ASSISTANT §4).

One connection, one reader task. Requests are matched to replies by ``id``; events go to
the handler registered with :meth:`HAConnection.on` for their ``event_type`` (or to the
subscription's own callback). On every successful ``auth_ok`` the ``on_connect``
callbacks run (the §4 startup steps 3-9: registries, config entries, states,
subscriptions, services), so a reconnect is always a full resync. A ``ping`` goes out every
30 s; two missed pongs drop the socket and the run loop reconnects with jittered
exponential backoff 1 s → 30 s. ``auth_invalid`` is terminal: status becomes ``reauth``
and nothing retries against a rejected token (§3).

Protocol details as read from HA 2026.10.3's ``websocket_api`` (``coalesce_messages``
makes HA send a JSON *array* of messages in one frame); the live surface is pinned by the
bridge contract tests (§13), which need the HA container and are pending on Pi.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

import aiohttp
from pydantic import SecretStr
from structlog.typing import FilteringBoundLogger

from beepboop.ha.ws import HAAuthInvalidError, HACommandError, HAWebSocketError, JsonObject
from beepboop.log import get_logger

DEFAULT_TIMEOUT_S = 5.0
PING_INTERVAL_S = 30.0
MAX_MISSED_PONGS = 2
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0

ConnectionStatus = Literal["connecting", "connected", "disconnected", "reauth", "closed"]
EventHandler = Callable[[JsonObject], None]
ConnectHook = Callable[["HAConnection"], Awaitable[None]]


def _log() -> FilteringBoundLogger:
    return get_logger(__name__)


class HAUnavailableError(HAWebSocketError):
    """Not connected (``HA_UNAVAILABLE``): ``home.*`` actions fail fast with this (§4)."""


@dataclass(frozen=True, slots=True)
class CallResult:
    """What ``call_service`` returns: HA's ``context`` id and the optional ``response``."""

    context_id: str | None
    response: object | None


class HAConnection:
    """Single persistent connection. Reconnects with backoff; resyncs fully after reconnect.

    ``token`` is a callable so the token is read from ``secrets`` at each connect and never
    stored on this object longer than the handshake needs.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: Callable[[], Awaitable[SecretStr]],
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        ping_interval: float = PING_INTERVAL_S,
        backoff_start: float = BACKOFF_START_S,
        backoff_max: float = BACKOFF_MAX_S,
        rng: random.Random | None = None,
    ) -> None:
        self._session = session
        self._url = url
        self._token = token
        self._timeout = timeout
        self._ping_interval = ping_interval
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._rng = rng or random.Random()  # noqa: S311 - backoff jitter, not crypto
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._subscriptions: dict[int, EventHandler] = {}
        self._handlers: dict[str, list[EventHandler]] = {}
        self._on_connect: list[ConnectHook] = []
        self._run_task: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._missed_pongs = 0
        self._closed = False
        self.status: ConnectionStatus = "disconnected"
        self.ha_version: str | None = None
        self.connects = 0

    # ------------------------------------------------------------------ public --

    @property
    def connected(self) -> bool:
        return self.status == "connected"

    def on(self, event_type: str, handler: EventHandler) -> None:
        """Register ``handler`` for events of ``event_type`` from :meth:`subscribe_events`."""
        self._handlers.setdefault(event_type, []).append(handler)

    def on_connect(self, hook: ConnectHook) -> None:
        """Run ``hook`` after every ``auth_ok`` (initial connect and every reconnect)."""
        self._on_connect.append(hook)

    async def connect(self) -> None:
        """Connect once: auth, ``supported_features``, then the ``on_connect`` hooks."""
        self.status = "connecting"
        ws = await self._session.ws_connect(self._url, heartbeat=None)
        self._ws = ws
        try:
            hello = await self._receive_direct()
            if hello.get("type") != "auth_required":
                raise HAWebSocketError(f"expected auth_required, got {hello.get('type')!r}")
            token = await self._token()
            await ws.send_json({"type": "auth", "access_token": token.get_secret_value()})
            del token
            reply = await self._receive_direct()
            if reply.get("type") == "auth_invalid":
                self.status = "reauth"
                raise HAAuthInvalidError(str(reply.get("message", "auth_invalid")))
            if reply.get("type") != "auth_ok":
                raise HAWebSocketError(f"expected auth_ok, got {reply.get('type')!r}")
            version = reply.get("ha_version")
            self.ha_version = version if isinstance(version, str) else None
        except BaseException:
            await ws.close()
            self._ws = None
            if self.status != "reauth":
                self.status = "disconnected"
            raise
        self._reader = asyncio.create_task(self._read_loop(ws), name="ha.ws.reader")
        self._missed_pongs = 0
        self.status = "connected"
        self.connects += 1
        _log().info("ha.ws.auth_ok", ha_version=self.ha_version, connects=self.connects)
        try:
            await self.call("supported_features", features={"coalesce_messages": 1})
            for hook in self._on_connect:
                await hook(self)
        except BaseException:
            await self._drop()
            raise
        self._ready.set()

    def start(self) -> asyncio.Task[None]:
        """Run :meth:`run` in the background (idempotent)."""
        if self._run_task is None or self._run_task.done():
            self._run_task = asyncio.create_task(self.run(), name="ha.ws.run")
        return self._run_task

    async def run(self) -> None:
        """Connect, keep alive, and reconnect with backoff until :meth:`close` or ``reauth``."""
        delay = self._backoff_start
        while not self._closed:
            try:
                await self.connect()
                delay = self._backoff_start
                await self._keepalive()
            except HAAuthInvalidError:
                _log().warning("ha.ws.reauth")
                return
            except asyncio.CancelledError:
                raise
            except (HAWebSocketError, aiohttp.ClientError, OSError, TimeoutError) as exc:
                _log().warning("ha.ws.disconnected", error=type(exc).__name__)
            await self._drop()
            if self._closed:
                return
            jittered = delay * (0.5 + self._rng.random())
            await asyncio.sleep(min(jittered, self._backoff_max))
            delay = min(delay * 2, self._backoff_max)

    async def wait_connected(self, within_s: float) -> bool:
        try:
            async with asyncio.timeout(within_s):
                await self._ready.wait()
        except TimeoutError:
            return False
        return self.connected

    async def call(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per-command reply deadline (§4)
        **payload: Any,
    ) -> Any:
        """Send one command and wait for its ``result``; raises :class:`HACommandError`."""
        reply = await self._request({"type": command, **payload}, timeout=timeout)
        if reply.get("type") == "pong":
            return None
        if reply.get("success") is True:
            return reply.get("result")
        error = reply.get("error")
        details = cast("JsonObject", error) if isinstance(error, dict) else {}
        raise HACommandError(
            command, str(details.get("code", "unknown")), str(details.get("message", ""))
        )

    async def subscribe_events(self, event_type: str) -> int:
        """``subscribe_events`` for one type; events reach the :meth:`on` handlers."""

        def dispatch(message: JsonObject) -> None:
            event = message.get("event")
            if isinstance(event, dict):
                body = cast("JsonObject", event)
                for handler in self._handlers.get(event_type, []):
                    handler(body)

        return await self._subscribe(
            {"type": "subscribe_events", "event_type": event_type}, dispatch
        )

    async def subscribe(self, command: str, handler: EventHandler, **payload: Any) -> int:
        """A command whose later ``event`` frames carry the subscription id
        (``config_entries/subscribe``); ``handler`` gets each frame's ``event``."""

        def dispatch(message: JsonObject) -> None:
            event = message.get("event")
            if isinstance(event, dict):
                handler(cast("JsonObject", event))
            elif isinstance(event, list):
                handler({"items": cast("list[object]", event)})

        return await self._subscribe({"type": command, **payload}, dispatch)

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None,
        target: dict[str, list[str]],
        *,
        return_response: bool = False,
        timeout: float = DEFAULT_TIMEOUT_S,  # noqa: ASYNC109 - HOME_ASSISTANT §4 signature
    ) -> CallResult:
        """``call_service`` with an explicit ``target`` (entity ids resolved by core, §4)."""
        if not self.connected:
            raise HAUnavailableError("not connected to Home Assistant")
        payload: dict[str, Any] = {
            "domain": domain,
            "service": service,
            "service_data": dict(data or {}),
            "target": target,
        }
        if return_response:
            payload["return_response"] = True
        result = await self.call("call_service", timeout=timeout, **payload)
        body = cast("JsonObject", result) if isinstance(result, dict) else {}
        context = body.get("context")
        context_id = None
        if isinstance(context, dict):
            raw_id = cast("JsonObject", context).get("id")
            context_id = raw_id if isinstance(raw_id, str) else None
        return CallResult(context_id, body.get("response"))

    async def close(self) -> None:
        self._closed = True
        self.status = "closed"
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._run_task
        await self._drop()
        self.status = "closed"

    # ---------------------------------------------------------------- internal --

    async def _subscribe(self, message: JsonObject, dispatch: EventHandler) -> int:
        message_id = next(self._ids)
        self._subscriptions[message_id] = dispatch
        try:
            reply = await self._request(message, message_id=message_id)
        except BaseException:
            self._subscriptions.pop(message_id, None)
            raise
        if reply.get("success") is not True:
            self._subscriptions.pop(message_id, None)
            error = reply.get("error")
            details = cast("JsonObject", error) if isinstance(error, dict) else {}
            raise HACommandError(
                str(message.get("type")),
                str(details.get("code", "unknown")),
                str(details.get("message", "")),
            )
        return message_id

    async def _request(
        self,
        message: JsonObject,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - reply deadline
        message_id: int | None = None,
    ) -> JsonObject:
        ws = self._ws
        if ws is None or ws.closed:
            raise HAUnavailableError("not connected to Home Assistant")
        mid = message_id if message_id is not None else next(self._ids)
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending[mid] = future
        try:
            await ws.send_json({"id": mid, **message})
            async with asyncio.timeout(timeout if timeout is not None else self._timeout):
                return await future
        except TimeoutError as exc:
            raise HAWebSocketError(f"{message.get('type')} timed out") from exc
        finally:
            self._pending.pop(mid, None)

    async def _receive_direct(self) -> JsonObject:
        """Read one frame before the reader task exists (the auth handshake)."""
        ws = self._ws
        if ws is None:
            raise HAWebSocketError("not connected")
        try:
            frame = await ws.receive(timeout=self._timeout)
        except TimeoutError as exc:
            raise HAWebSocketError("timed out waiting for Home Assistant") from exc
        if frame.type != aiohttp.WSMsgType.TEXT:
            raise HAWebSocketError(f"websocket closed or non-text frame ({frame.type.name})")
        data: object = frame.json()
        if not isinstance(data, dict):
            raise HAWebSocketError("expected a JSON object from Home Assistant")
        return cast("JsonObject", data)

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for frame in ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    break
                data: object = frame.json()
                items = cast("list[object]", data) if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict):
                        self._route(cast("JsonObject", item))
        finally:
            self._fail_pending(HAUnavailableError("Home Assistant connection lost"))
            if self.status == "connected":
                self.status = "disconnected"
                self._ready.clear()

    def _route(self, message: JsonObject) -> None:
        mid = message.get("id")
        kind = message.get("type")
        if kind == "event" and isinstance(mid, int):
            handler = self._subscriptions.get(mid)
            if handler is not None:
                try:
                    handler(message)
                except Exception:
                    _log().exception("ha.ws.handler_failed")
            return
        if kind == "pong":
            self._missed_pongs = 0
        if isinstance(mid, int):
            future = self._pending.get(mid)
            if future is not None and not future.done():
                future.set_result(message)

    async def _keepalive(self) -> None:
        """Ping every ``ping_interval``; return (to reconnect) after two missed pongs or EOF."""
        reader = self._reader
        if reader is None:
            return
        while self.connected:
            done, _ = await asyncio.wait({reader}, timeout=self._ping_interval)
            if done:
                return
            try:
                await self._request({"type": "ping"}, timeout=self._ping_interval)
                self._missed_pongs = 0
            except HAWebSocketError:
                self._missed_pongs += 1
                _log().warning("ha.ws.ping_missed", missed=self._missed_pongs)
                if self._missed_pongs >= MAX_MISSED_PONGS:
                    return

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        self._subscriptions.clear()

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            await ws.close()
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        self._fail_pending(HAUnavailableError("Home Assistant connection lost"))
        self._ready.clear()
        if self.status not in ("closed", "reauth"):
            self.status = "disconnected"


__all__ = [
    "CallResult",
    "ConnectionStatus",
    "HAAuthInvalidError",
    "HACommandError",
    "HAConnection",
    "HAUnavailableError",
    "HAWebSocketError",
]
