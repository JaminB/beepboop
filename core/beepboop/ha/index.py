"""The registry index and the per-profile view (HOME_ASSISTANT §6.1, §6.2).

:class:`RegistryIndex` is a pure, immutable build from HA's entity, device, area and floor
registries plus the state cache; it is rebuilt wholesale on every resync and registry
event. :class:`HAEntityIndex` is the resolver's :class:`~beepboop.registry.resolver.EntityIndex`
over it: an entity is resolvable only if HA has it enabled and visible, its ``ha_entities``
row is ``active`` (the new-device inbox, IDENTITY_AND_TRUST §6.2) and, for ``visible``,
some grant of the principal covers it at ``read`` or above right now.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from beepboop.db.repo.ha_entities import EntityRow
from beepboop.ha.cache import State, StateCache
from beepboop.module.types import EntityRef
from beepboop.policy.authorize import PolicyState
from beepboop.policy.grants import effective_grants
from beepboop.policy.types import EntityInfo, Principal
from beepboop.registry.resolver import Area, Floor

type JsonObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    name: str | None
    area_id: str | None


@dataclass(frozen=True, slots=True)
class IndexedEntity:
    """An :class:`EntityRef` plus the registry facts policy needs but modules do not."""

    ref: EntityRef
    platform: str | None = None
    entity_category: str | None = None

    @property
    def is_template_switch(self) -> bool:
        """A template switch is an automation in disguise (HOME_ASSISTANT §9, indirect)."""
        return self.ref.domain == "switch" and self.platform == "template"


def _str(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) and value else None


def _str_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple | set | frozenset):
        return ()
    items = cast("list[object]", list(cast("Any", raw)))
    return tuple(sorted({str(a) for a in items if isinstance(a, str) and a}))


def _objects(raw: object) -> list[JsonObject]:
    if not isinstance(raw, list):
        return []
    return [cast("JsonObject", r) for r in cast("list[object]", raw) if isinstance(r, dict)]


@dataclass(frozen=True)
class RegistryIndex:
    """Entities by UUID, plus areas, floors and devices. Immutable; rebuild to change."""

    entities: Mapping[str, IndexedEntity] = field(default_factory=dict[str, IndexedEntity])
    by_entity_id: Mapping[str, str] = field(default_factory=dict[str, str])
    areas: Mapping[str, Area] = field(default_factory=dict[str, Area])
    floors: Mapping[str, Floor] = field(default_factory=dict[str, Floor])
    devices: Mapping[str, Device] = field(default_factory=dict[str, Device])
    version: int = 0

    @classmethod
    def build(
        cls,
        *,
        entity_list: object,
        device_list: object = (),
        area_list: object = (),
        floor_list: object = (),
        extended: Mapping[str, JsonObject] | None = None,
        states: Mapping[str, State] | None = None,
        version: int = 0,
    ) -> RegistryIndex:
        """Build from the raw ``config/*_registry/list`` results (§4 step 4).

        ``extended`` is ``config/entity_registry/get_entries`` keyed by ``entity_id``: the
        list command omits ``aliases`` and ``device_class``. The name is the registry
        ``name`` (set by a person), else the state's ``friendly_name`` (what HA's own UI
        shows), else ``original_name``, else the device name, else the object id.
        """
        extended = extended or {}
        states = states or {}
        floors: dict[str, Floor] = {}
        for f in _objects(floor_list):
            fid = _str(f, "floor_id")
            if fid:
                floors[fid] = Floor(fid, _str(f, "name") or fid, _str_tuple(f.get("aliases")))
        areas: dict[str, Area] = {}
        for a in _objects(area_list):
            aid = _str(a, "area_id")
            if aid:
                areas[aid] = Area(
                    aid, _str(a, "name") or aid, _str_tuple(a.get("aliases")), _str(a, "floor_id")
                )
        devices: dict[str, Device] = {}
        for d in _objects(device_list):
            did = _str(d, "id")
            if did:
                devices[did] = Device(
                    did, _str(d, "name_by_user") or _str(d, "name"), _str(d, "area_id")
                )
        entities: dict[str, IndexedEntity] = {}
        by_entity_id: dict[str, str] = {}
        for e in _objects(entity_list):
            uuid = _str(e, "id")
            entity_id = _str(e, "entity_id")
            if not uuid or not entity_id or "." not in entity_id:
                continue
            ext = extended.get(entity_id, {})
            state = states.get(entity_id)
            attrs: Mapping[str, Any] = state.attributes if state is not None else {}
            device = devices.get(_str(e, "device_id") or "")
            friendly = attrs.get("friendly_name")
            name = (
                _str(e, "name")
                or (friendly if isinstance(friendly, str) and friendly else None)
                or _str(e, "original_name")
                or (device.name if device is not None else None)
                or entity_id.split(".", 1)[1].replace("_", " ")
            )
            area_id = _str(e, "area_id") or (device.area_id if device is not None else None)
            area = areas.get(area_id or "")
            state_dc = attrs.get("device_class")
            device_class = (
                _str(ext, "device_class")
                or _str(ext, "original_device_class")
                or (state_dc if isinstance(state_dc, str) else None)
            )
            ref = EntityRef(
                uuid=uuid,
                entity_id=entity_id,
                domain=entity_id.split(".", 1)[0],
                name=name,
                aliases=_str_tuple(ext.get("aliases", e.get("aliases"))),
                area_id=area_id,
                floor_id=area.floor_id if area is not None else None,
                device_id=_str(e, "device_id"),
                config_entry_id=_str(e, "config_entry_id"),
                device_class=device_class,
                hidden=e.get("hidden_by") is not None,
                disabled=e.get("disabled_by") is not None,
            )
            entities[uuid] = IndexedEntity(ref, _str(e, "platform"), _str(e, "entity_category"))
            by_entity_id[entity_id] = uuid
        return cls(entities, by_entity_id, areas, floors, devices, version)

    @classmethod
    def from_mirror(cls, rows: Sequence[EntityRow], version: int = 0) -> RegistryIndex:
        """Rebuild from the ``ha_entities`` mirror (DATA_AND_PRIVACY §6.1) so the resolver,
        ``policy explain`` and the fast path work before HA has connected (or while it is
        down after a restart). Areas are reconstructed from the area ids; platforms are
        unknown, so a template switch counts as such only after a live sync."""
        areas: dict[str, Area] = {}
        entities: dict[str, IndexedEntity] = {}
        by_entity_id: dict[str, str] = {}
        for r in rows:
            if r.removed_at is not None:
                continue
            if r.area_id and r.area_id not in areas:
                areas[r.area_id] = Area(r.area_id, r.area_id.replace("_", " ").capitalize())
            ref = EntityRef(
                uuid=r.uuid,
                entity_id=r.entity_id,
                domain=r.domain,
                name=r.friendly_name or r.entity_id.split(".", 1)[1].replace("_", " "),
                aliases=r.aliases,
                area_id=r.area_id,
                device_id=r.device_id,
                config_entry_id=r.config_entry_id,
                device_class=r.device_class,
            )
            entities[r.uuid] = IndexedEntity(ref)
            by_entity_id[r.entity_id] = r.uuid
        return cls(entities, by_entity_id, areas, {}, {}, version)

    def get(self, key: str) -> IndexedEntity | None:
        """By UUID or by current ``entity_id``."""
        found = self.entities.get(key)
        if found is not None:
            return found
        uuid = self.by_entity_id.get(key)
        return None if uuid is None else self.entities.get(uuid)


class HAEntityIndex:
    """The resolver's ``EntityIndex`` over the live registry, cache and policy snapshot."""

    def __init__(
        self,
        registry: Callable[[], RegistryIndex],
        cache: StateCache,
        policy_state: Callable[[], PolicyState],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._registry = registry
        self._cache = cache
        self._policy_state = policy_state
        self._clock = clock
        self._memo: dict[tuple[int, int, str, int], tuple[EntityRef, ...]] = {}

    @property
    def registry(self) -> RegistryIndex:
        return self._registry()

    def entity_info(self, uuid: str) -> EntityInfo:
        """The policy view of one entity; unknown UUIDs are ``new`` (default deny)."""
        return self._policy_state().entities.get(uuid, EntityInfo(uuid=uuid, status="new"))

    def all_active(self) -> Sequence[EntityRef]:
        state = self._policy_state()
        out: list[EntityRef] = []
        for item in self._registry().entities.values():
            ref = item.ref
            if ref.hidden or ref.disabled:
                continue
            info = state.entities.get(ref.uuid)
            if info is None or info.status != "active":
                continue
            out.append(ref)
        out.sort(key=lambda r: (r.entity_id, r.uuid))
        return out

    def visible(self, principal: Principal) -> Sequence[EntityRef]:
        """§6.2: active entities some grant of ``principal`` covers at ``read`` or above now."""
        state = self._policy_state()
        registry = self._registry()
        now = self._clock()
        key = (id(state), registry.version, principal.profile_id, int(now.timestamp() // 60))
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        profile = state.profiles.get(principal.profile_id)
        if profile is None or profile.disabled:
            return ()
        grants = effective_grants(
            state.grants, profile.id, profile.group_ids, "read", now, state.tz
        )
        out: list[EntityRef] = []
        for ref in self.all_active():
            info = state.entities.get(ref.uuid, EntityInfo(uuid=ref.uuid, status="new"))
            if any(g.covers_entity(info) for g in grants):
                out.append(ref)
        result = tuple(out)
        if len(self._memo) > 256:
            self._memo.clear()
        self._memo[key] = result
        return result

    def areas(self) -> Sequence[Area]:
        return sorted(self._registry().areas.values(), key=lambda a: a.id)

    def floors(self) -> Sequence[Floor]:
        return sorted(self._registry().floors.values(), key=lambda f: f.id)

    def state_of(self, entity: EntityRef) -> str | None:
        found = self._cache.get(entity.entity_id)
        return None if found is None else found.state


class HookView:
    """``EntityIndex`` for hooks (no principal): every active entity is visible."""

    def __init__(self, index: HAEntityIndex) -> None:
        self._index = index

    def visible(self, principal: Principal) -> Sequence[EntityRef]:
        return self._index.all_active()

    def all_active(self) -> Sequence[EntityRef]:
        return self._index.all_active()

    def areas(self) -> Sequence[Area]:
        return self._index.areas()

    def floors(self) -> Sequence[Floor]:
        return self._index.floors()

    def state_of(self, entity: EntityRef) -> str | None:
        return self._index.state_of(entity)


__all__ = ["Device", "HAEntityIndex", "HookView", "IndexedEntity", "RegistryIndex"]
