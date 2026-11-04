"""``HAService``: owns the connection, state cache, registry index and the inbox mirror,
and hands out policy-wrapped clients (HOME_ASSISTANT §4-§8).

Startup (§4 steps 3-9) runs as the connection's ``on_connect`` hook, so a reconnect is a
full resync: ``get_config`` → the four registries (+ ``get_entries`` for aliases and
device classes) → ``config_entries/get`` → ``get_states`` → event subscriptions →
``config_entries/subscribe`` → ``get_services``. Registry events schedule a debounced
registry resync; ``state_changed`` updates the cache in place. Every registry build is
mirrored into ``ha_entities`` in one durable write (:func:`~beepboop.ha.inbox.sync_entities`)
and, when anything changed, the policy snapshot is rebuilt.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

import aiohttp
from pydantic import SecretStr

from beepboop.db import Database
from beepboop.db.repo import ha_entities
from beepboop.ha.cache import StateCache
from beepboop.ha.client import HAConnection
from beepboop.ha.inbox import SyncResult, sync_entities
from beepboop.ha.index import HAEntityIndex, RegistryIndex
from beepboop.ha.onboarding import HA_TOKEN_SECRET
from beepboop.ha.plan import HATargetExtractor
from beepboop.ha.wrapper import HARuntime, PolicyHAClient, ServiceCatalog
from beepboop.ha.ws import HACommandError, JsonObject
from beepboop.log import get_logger
from beepboop.policy.authorize import PolicyState
from beepboop.policy.types import Approval, Principal, ToolLike
from beepboop.secrets import SecretStore

DEFAULT_WS_URL = "ws://127.0.0.1:8123/api/websocket"
REGISTRY_EVENTS = (
    "entity_registry_updated",
    "device_registry_updated",
    "area_registry_updated",
    "floor_registry_updated",
)
HAHealth = Literal["ok", "unavailable", "reauth", "needs_login"]


@dataclass
class ConfigEntry:
    entry_id: str
    domain: str
    title: str
    state: str


@dataclass
class HAService:
    """The one HA integration object in core. Build, then :meth:`start` (or drive
    :meth:`resync` directly in tests)."""

    db: Database
    policy_state: Callable[[], PolicyState]
    refresh_policy: Callable[[], Awaitable[None]]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    debounce_s: float = 0.5
    cache: StateCache = field(default_factory=StateCache)
    connection: HAConnection | None = None
    config: JsonObject = field(default_factory=dict[str, object])
    config_entries: dict[str, ConfigEntry] = field(default_factory=dict[str, ConfigEntry])
    _registry: RegistryIndex = field(default_factory=RegistryIndex)
    _services: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    _resync_task: asyncio.Task[None] | None = None
    _session: aiohttp.ClientSession | None = None
    last_sync: SyncResult | None = None

    def __post_init__(self) -> None:
        self.index = HAEntityIndex(
            lambda: self._registry, self.cache, self.policy_state, self.clock
        )
        self.runtime = HARuntime(
            index=self.index,
            cache=self.cache,
            policy_state=self.policy_state,
            connection=self.connection,
            services=self.services,
            clock=self.clock,
        )
        self.extractor = HATargetExtractor(self.index)

    # --------------------------------------------------------------- lifecycle --

    def attach(self, connection: HAConnection) -> None:
        """Use ``connection`` and register the resync hook and event handlers on it."""
        self.connection = connection
        self.runtime.connection = connection
        connection.on_connect(self._on_connect)
        connection.on("state_changed", self._on_state_changed)
        for event in REGISTRY_EVENTS:
            connection.on(event, self._on_registry_event)
        connection.on("service_registered", self._on_service_event)
        connection.on("service_removed", self._on_service_event)

    async def start(
        self,
        token: Callable[[], Awaitable[SecretStr]],
        *,
        url: str = DEFAULT_WS_URL,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Open the connection in the background; core keeps serving while HA is down."""
        if session is None:
            session = aiohttp.ClientSession()
            self._session = session
        connection = HAConnection(session, url, token)
        self.attach(connection)
        connection.start()

    async def load_mirror(self) -> int:
        """Seed the registry index from ``ha_entities`` before the first live sync, so
        names resolve while HA is unreachable. A live sync replaces it. Returns the count."""
        if self._registry.entities:
            return len(self._registry.entities)
        rows = await self.db.read(lambda c: ha_entities.list_(c))
        self._registry = RegistryIndex.from_mirror(rows)
        return len(self._registry.entities)

    async def stop(self) -> None:
        task = self._resync_task
        if task is not None and not task.done():
            # Let an in-flight resync finish its database work (a cancelled await does not
            # stop the worker thread), then cancel if it is stuck on the network.
            done, _ = await asyncio.wait({task}, timeout=2.0)
            if not done:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.connection is not None:
            await self.connection.close()
        if self._session is not None:
            await self._session.close()

    @property
    def health(self) -> HAHealth:
        if self.connection is None:
            return "needs_login"
        if self.connection.status == "reauth":
            return "reauth"
        return "ok" if self.connection.connected else "unavailable"

    # ----------------------------------------------------------------- clients --

    def services(self) -> ServiceCatalog:
        return self._services

    @property
    def registry(self) -> RegistryIndex:
        return self._registry

    def client_for_hook(self) -> PolicyHAClient:
        """``ctx.ha`` outside a tool call: read-only (``call_service`` raises)."""
        return PolicyHAClient(self.runtime)

    def client_for_call(
        self,
        principal: Principal,
        tool: ToolLike,
        *,
        approval: Approval | None = None,
        deadline: float | None = None,
        authorized_targets: Iterable[str] | None = None,
    ) -> PolicyHAClient:
        """``ctx.ha`` for one call, bound to its principal, tool and verified approval;
        ``registry.dispatch`` narrows it to the targets it authorized."""
        return PolicyHAClient(
            self.runtime,
            principal=principal,
            tool=tool,
            approval=approval,
            deadline=deadline,
            authorized_targets=authorized_targets,
        )

    # ------------------------------------------------------------------- sync --

    async def _on_connect(self, conn: HAConnection) -> None:
        self.cache.stale = True
        config = await conn.call("get_config")
        self.config = cast(JsonObject, config) if isinstance(config, dict) else {}
        await self._sync_registries(conn, states_first=True)
        entries = await conn.call("config_entries/get")
        self._apply_entries(entries)
        for event in ("state_changed", *REGISTRY_EVENTS):
            await conn.subscribe_events(event)
        await conn.subscribe("config_entries/subscribe", self._on_entries_event)
        services = await conn.call("get_services")
        self._apply_services(services)
        await conn.subscribe_events("service_registered")
        await conn.subscribe_events("service_removed")
        get_logger(__name__).info(
            "ha.synced", entities=len(self._registry.entities), states=len(self.cache)
        )

    async def _sync_registries(self, conn: HAConnection, *, states_first: bool = False) -> None:
        if states_first:
            # Names fall back to the state's friendly_name, so states come before the build.
            states = await conn.call("get_states")
            rows = [
                cast(JsonObject, r) for r in cast(list[object], states or []) if isinstance(r, dict)
            ]
            self.cache.replace_all(rows)
        entity_list = await conn.call("config/entity_registry/list")
        device_list = await conn.call("config/device_registry/list")
        area_list = await conn.call("config/area_registry/list")
        floor_list = await conn.call("config/floor_registry/list")
        extended = await self._extended(conn, entity_list)
        await self.apply_registry(
            RegistryIndex.build(
                entity_list=entity_list,
                device_list=device_list,
                area_list=area_list,
                floor_list=floor_list,
                extended=extended,
                states=self.cache.all(),
                version=self._registry.version + 1,
            )
        )

    async def _extended(self, conn: HAConnection, entity_list: object) -> dict[str, JsonObject]:
        """``get_entries`` for aliases and device classes; tolerated if HA refuses it."""
        if not isinstance(entity_list, list):
            return {}
        ids = sorted(
            str(cast(JsonObject, e).get("entity_id"))
            for e in cast(list[object], entity_list)
            if isinstance(e, dict) and isinstance(cast(JsonObject, e).get("entity_id"), str)
        )
        if not ids:
            return {}
        try:
            raw = await conn.call("config/entity_registry/get_entries", entity_ids=ids)
        except HACommandError as exc:
            get_logger(__name__).warning("ha.get_entries.failed", code=exc.code)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): cast(JsonObject, v)
            for k, v in cast(dict[object, object], raw).items()
            if isinstance(v, dict)
        }

    async def apply_registry(self, index: RegistryIndex) -> SyncResult:
        """Swap in ``index``, mirror it into ``ha_entities`` (durable), refresh policy."""
        self._registry = index
        services = self.policy_state().services
        result = await self.db.write(lambda c: sync_entities(c, index, services), durable=True)
        if result.changed:
            await self.refresh_policy()
            get_logger(__name__).info(
                "ha.inbox.synced",
                inserted=len(result.inserted),
                updated=len(result.updated),
                removed=len(result.removed),
            )
        self.last_sync = result
        return result

    def _on_state_changed(self, event: JsonObject) -> None:
        data = event.get("data")
        if isinstance(data, dict):
            self.cache.apply_state_changed(cast(Mapping[str, object], data))

    def _on_registry_event(self, event: JsonObject) -> None:
        if self._resync_task is not None and not self._resync_task.done():
            return
        self._resync_task = asyncio.create_task(self._debounced_resync(), name="ha.resync")

    async def _debounced_resync(self) -> None:
        await asyncio.sleep(self.debounce_s)
        conn = self.connection
        if conn is None or not conn.connected:
            return
        try:
            await self._sync_registries(conn)
        except Exception as exc:
            get_logger(__name__).warning("ha.resync.failed", error=type(exc).__name__)

    def _apply_entries(self, raw: object) -> None:
        if not isinstance(raw, list):
            return
        entries: dict[str, ConfigEntry] = {}
        for item in cast(list[object], raw):
            if isinstance(item, dict):
                entry = self._entry(cast(JsonObject, item))
                if entry is not None:
                    entries[entry.entry_id] = entry
        self.config_entries = entries

    @staticmethod
    def _entry(raw: JsonObject) -> ConfigEntry | None:
        entry_id = raw.get("entry_id")
        if not isinstance(entry_id, str):
            return None
        return ConfigEntry(
            entry_id,
            str(raw.get("domain", "")),
            str(raw.get("title", "")),
            str(raw.get("state", "")),
        )

    def _on_entries_event(self, event: JsonObject) -> None:
        """``config_entries/subscribe``: ``[{type: null|added|removed|updated, entry}]``."""
        items = event.get("items")
        for raw in cast(list[object], items) if isinstance(items, list) else []:
            if not isinstance(raw, dict):
                continue
            change = cast(JsonObject, raw)
            entry_raw = change.get("entry")
            if not isinstance(entry_raw, dict):
                continue
            entry = self._entry(cast(JsonObject, entry_raw))
            if entry is None:
                continue
            if change.get("type") == "removed":
                self.config_entries.pop(entry.entry_id, None)
            else:
                self.config_entries[entry.entry_id] = entry

    def _apply_services(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        catalog: dict[str, dict[str, object]] = {}
        for domain, services in cast(dict[object, object], raw).items():
            if isinstance(services, dict):
                catalog[str(domain)] = {
                    str(k): v for k, v in cast(dict[object, object], services).items()
                }
        self._services = catalog

    def _on_service_event(self, event: JsonObject) -> None:
        data = event.get("data")
        if not isinstance(data, dict):
            return
        body = cast(JsonObject, data)
        domain, service = body.get("domain"), body.get("service")
        if not isinstance(domain, str) or not isinstance(service, str):
            return
        if event.get("event_type") == "service_removed":
            self._services.get(domain, {}).pop(service, None)
        else:
            self._services.setdefault(domain, {}).setdefault(service, {})


async def token_from_secrets(store: SecretStore) -> SecretStr:
    """Read the long-lived token from the envelope store (HOME_ASSISTANT §3)."""
    return await store.get(HA_TOKEN_SECRET)


__all__ = ["DEFAULT_WS_URL", "ConfigEntry", "HAHealth", "HAService", "token_from_secrets"]
