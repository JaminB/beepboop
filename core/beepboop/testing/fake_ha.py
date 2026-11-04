"""A fake Home Assistant WebSocket for ``HAConnection`` / ``HAService`` and system tests.

Lives in ``beepboop.testing`` so the HA suite (``core/tests/ha``) and the cross-service
system tests (``tests/system``) share one fake; the fixture household is
``beepboop/testing/fixtures/ha_household.json``.

It speaks the §4 surface of HOME_ASSISTANT.md as read from HA 2026.10.3's
``websocket_api``: ``auth_required`` → ``auth`` → ``auth_ok``/``auth_invalid``, then
``id``-matched ``result`` replies, ``pong`` for ``ping``, ``event`` frames carrying the
subscription id, and (after ``supported_features {coalesce_messages: 1}``) event bursts
coalesced into one JSON-array frame. The live surface is pinned by the bridge contract
tests against the HA container (HOME_ASSISTANT §13), which are pending on Pi.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from aiohttp import web
from aiohttp.test_utils import TestServer

HA_VERSION = "2026.10.3"
FIXTURE = Path(__file__).with_name("fixtures") / "ha_household.json"
GOOD_TOKEN = "fake-llat-0123456789abcdef"  # noqa: S105 - the fake HA's test token


def load_household() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


@dataclass
class FakeHAWebSocket:
    """State of the fake; tests mutate it and inspect what core sent."""

    household: dict[str, Any] = field(default_factory=load_household)
    token: str = GOOD_TOKEN
    answer_pings: bool = True
    refuse_get_entries: bool = False
    commands: list[str] = field(default_factory=list[str])
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    auth_attempts: int = 0
    connections: int = 0
    coalesce: bool = False
    _sockets: list[web.WebSocketResponse] = field(default_factory=list[web.WebSocketResponse])
    _subs: dict[int, tuple[web.WebSocketResponse, str]] = field(
        default_factory=dict[int, tuple[web.WebSocketResponse, str]]
    )

    # ------------------------------------------------------------- household --

    @property
    def states(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.household["states"])

    @property
    def entities(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.household["entities"])

    def set_state(self, entity_id: str, state: str, **attributes: Any) -> dict[str, Any]:
        for row in self.states:
            if row["entity_id"] == entity_id:
                row["state"] = state
                row["attributes"] = {**row.get("attributes", {}), **attributes}
                return row
        row = {"entity_id": entity_id, "state": state, "attributes": attributes}
        self.states.append(row)
        return row

    # ------------------------------------------------------------------ server --

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/websocket", self.websocket)
        return app

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        await ws.send_json({"type": "auth_required", "ha_version": HA_VERSION})
        auth = await ws.receive_json()
        self.auth_attempts += 1
        if auth.get("type") != "auth" or auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": HA_VERSION})
        self._sockets.append(ws)
        try:
            async for frame in ws:
                msg = cast(dict[str, Any], json.loads(frame.data))
                await self._handle(ws, msg)
        finally:
            if ws in self._sockets:
                self._sockets.remove(ws)
            for sid in [k for k, (sock, _) in self._subs.items() if sock is ws]:
                del self._subs[sid]
        return ws

    async def _handle(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        mid = msg.get("id")
        kind = str(msg.get("type"))
        self.commands.append(kind)
        h = self.household
        if kind == "ping":
            if self.answer_pings:
                await ws.send_json({"id": mid, "type": "pong"})
            return
        if kind == "supported_features":
            self.coalesce = bool(msg.get("features", {}).get("coalesce_messages"))
            return await self._ok(ws, mid, None)
        if kind == "get_config":
            return await self._ok(
                ws, mid, {"version": HA_VERSION, "unit_system": {"temperature": "°C"}}
            )
        if kind == "config/entity_registry/list":
            return await self._ok(ws, mid, copy.deepcopy(h["entities"]))
        if kind == "config/entity_registry/get_entries":
            if self.refuse_get_entries:
                return await self._error(ws, mid, "unknown_command", "Unknown command.")
            ext = h.get("extended", {})
            wanted = msg.get("entity_ids", [])
            return await self._ok(ws, mid, {e: ext.get(e, {}) for e in wanted})
        if kind == "config/device_registry/list":
            return await self._ok(ws, mid, copy.deepcopy(h["devices"]))
        if kind == "config/area_registry/list":
            return await self._ok(ws, mid, copy.deepcopy(h["areas"]))
        if kind == "config/floor_registry/list":
            return await self._ok(ws, mid, copy.deepcopy(h["floors"]))
        if kind == "config_entries/get":
            return await self._ok(ws, mid, copy.deepcopy(h["config_entries"]))
        if kind == "get_states":
            return await self._ok(ws, mid, copy.deepcopy(h["states"]))
        if kind == "get_services":
            return await self._ok(ws, mid, copy.deepcopy(h["services"]))
        if kind == "subscribe_events":
            self._subs[int(cast(int, mid))] = (ws, str(msg.get("event_type")))
            return await self._ok(ws, mid, None)
        if kind == "config_entries/subscribe":
            self._subs[int(cast(int, mid))] = (ws, "config_entries")
            await self._ok(ws, mid, None)
            initial = [{"type": None, "entry": e} for e in h["config_entries"]]
            await ws.send_json({"id": mid, "type": "event", "event": initial})
            return None
        if kind == "call_service":
            self.calls.append(msg)
            domain, service = msg.get("domain"), msg.get("service")
            if service not in h["services"].get(domain, {}):
                return await self._error(ws, mid, "service_not_found", "Service not found.")
            return await self._ok(
                ws, mid, {"context": {"id": f"ctx-{len(self.calls)}"}, "response": None}
            )
        await self._error(ws, mid, "unknown_command", "Unknown command.")

    async def _ok(self, ws: web.WebSocketResponse, mid: object, result: object) -> None:
        await ws.send_json({"id": mid, "type": "result", "success": True, "result": result})

    async def _error(self, ws: web.WebSocketResponse, mid: object, code: str, message: str) -> None:
        await ws.send_json(
            {
                "id": mid,
                "type": "result",
                "success": False,
                "error": {"code": code, "message": message},
            }
        )

    # ------------------------------------------------------------------ events --

    async def fire(self, event_type: str, data: dict[str, Any]) -> int:
        """Send one event to every subscriber of ``event_type``; returns how many got it."""
        sent = 0
        for sid, (ws, wanted) in list(self._subs.items()):
            if wanted != event_type or ws.closed:
                continue
            frame = {
                "id": sid,
                "type": "event",
                "event": {"event_type": event_type, "data": data, "origin": "LOCAL"},
            }
            await ws.send_json([frame] if self.coalesce else frame)
            sent += 1
        return sent

    async def entries_event(self, change: str, entry: dict[str, Any]) -> None:
        for sid, (ws, wanted) in list(self._subs.items()):
            if wanted == "config_entries" and not ws.closed:
                await ws.send_json(
                    {"id": sid, "type": "event", "event": [{"type": change, "entry": entry}]}
                )

    async def drop(self) -> None:
        """Close every socket (HA restarting)."""
        for ws in list(self._sockets):
            await ws.close()

    def subscribed(self, event_type: str) -> bool:
        return any(t == event_type for _, t in self._subs.values())


async def serve(fake: FakeHAWebSocket) -> AsyncGenerator[str]:
    """Serve ``fake`` on a free loopback port; yields its ``ws://…/api/websocket`` URL."""
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield str(server.make_url("/api/websocket"))
    finally:
        await server.close()


__all__ = ["FIXTURE", "GOOD_TOKEN", "HA_VERSION", "FakeHAWebSocket", "load_household", "serve"]
