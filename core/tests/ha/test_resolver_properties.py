"""Resolver properties over the fixture household (HOME_ASSISTANT §6.3, ROADMAP v0.1 #4).

``HAEntityIndex`` over ``beepboop/testing/fixtures/ha_household.json`` with random grants
and random inbox statuses; queries are drawn from the household's own vocabulary plus
noise. Whatever the query, the resolver never returns an entity the principal cannot see,
never returns a ``new`` / ``hidden`` / HA-hidden / disabled entity, never names anything to
a guest, and is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from beepboop.ha.cache import StateCache
from beepboop.ha.index import HAEntityIndex, RegistryIndex
from beepboop.policy import (
    ALL_SCOPES,
    EntityInfo,
    Grant,
    GrantIndex,
    PolicyState,
    Principal,
    ProfileView,
)
from beepboop.policy.types import ProfileKind
from beepboop.registry.resolver import TargetQuery, resolve

from .fake_ws import load_household

RAW = load_household()
CACHE = StateCache()
CACHE.replace_all(RAW["states"])
REGISTRY = RegistryIndex.build(
    entity_list=RAW["entities"],
    device_list=RAW["devices"],
    area_list=RAW["areas"],
    floor_list=RAW["floors"],
    extended=RAW["extended"],
    states=CACHE.all(),
    version=1,
)
UUIDS = sorted(REGISTRY.entities)
AREAS = sorted(REGISTRY.areas)
WORDS = sorted(
    {
        w.lower()
        for item in REGISTRY.entities.values()
        for w in (item.ref.name + " " + " ".join(item.ref.aliases)).split()
    }
    | {"lights", "lamp", "the", "all", "kitchen", "upstairs", "tv", "door", "flux", "lounge"}
)
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

statuses = st.dictionaries(st.sampled_from(UUIDS), st.sampled_from(["new", "active", "hidden"]))
entity_grants = st.sets(st.sampled_from(UUIDS), max_size=8)
area_grants = st.sets(st.sampled_from(AREAS), max_size=3)
queries = st.builds(
    TargetQuery,
    name=st.one_of(
        st.none(), st.lists(st.sampled_from(WORDS), min_size=1, max_size=4).map(" ".join)
    ),
    area=st.one_of(st.none(), st.sampled_from([*AREAS, "lounge", "upstairs", "attic"])),
    domain=st.one_of(st.none(), st.sampled_from(["light", "lock", "cover", "media_player"])),
    all=st.booleans(),
)


def _state(status: dict[str, str], entities: set[str], areas: set[str], who: str) -> PolicyState:
    infos = {
        u: EntityInfo(
            u,
            status.get(u, "active"),  # type: ignore[arg-type]
            REGISTRY.entities[u].ref.area_id,
            REGISTRY.entities[u].ref.config_entry_id,
        )
        for u in UUIDS
    }
    grants = [Grant(f"e{u}", "profile", who, "ha_entity", u, "act") for u in sorted(entities)]
    grants += [Grant(f"a{a}", "profile", who, "ha_area", a, "act") for a in sorted(areas)]
    kind: ProfileKind = "guest" if who == "guest" else "member"
    profiles = {who: ProfileView(who, kind, "critical")}
    return PolicyState(profiles=profiles, grants=GrantIndex.build(grants), entities=infos)


def _index(state: PolicyState) -> HAEntityIndex:
    return HAEntityIndex(lambda: REGISTRY, CACHE, lambda: state, lambda: NOW)


def _principal(who: str) -> Principal:
    return Principal(who, "voice", 0.95, 0.3, "main", ALL_SCOPES, False, "T")


@settings(max_examples=300, deadline=None)
@given(statuses, entity_grants, area_grants, queries, st.sampled_from(["sam", "guest"]))
def test_resolver_never_leaks(
    status: dict[str, str],
    entities: set[str],
    areas: set[str],
    query: TargetQuery,
    who: str,
) -> None:
    state = _state(status, entities, areas, who)
    index = _index(state)
    p = _principal(who)
    visible = {e.uuid for e in index.visible(p)}
    result = resolve(p, query, index)
    assert result == resolve(p, query, index)  # deterministic
    targets = {e.uuid for e in result.targets}
    assert targets <= visible  # never an ungranted entity
    assert {e.uuid for e, _ in result.candidates} <= visible  # nor in a clarifying question
    assert not ({e.uuid for e in result.denied} & visible)
    for uuid in targets | {e.uuid for e in result.denied}:
        item = REGISTRY.entities[uuid]
        assert status.get(uuid, "active") == "active"  # new/hidden never resolve
        assert not item.ref.hidden
        assert not item.ref.disabled
    if who == "guest":
        assert result.status != "denied"
        assert not result.denied
    if result.status != "ok":
        assert result.targets == ()


@settings(max_examples=100, deadline=None)
@given(st.sampled_from(UUIDS))
def test_every_active_granted_entity_resolves_by_its_own_name(uuid: str) -> None:
    item = REGISTRY.entities[uuid]
    if item.ref.hidden or item.ref.disabled:
        return
    state = _state({}, set(UUIDS), set(), "sam")
    result = resolve(_principal("sam"), TargetQuery(name=item.ref.name), _index(state))
    assert uuid in {e.uuid for e in result.targets} | {e.uuid for e, _ in result.candidates}


@settings(max_examples=100, deadline=None)
@given(statuses)
def test_new_entities_are_unreachable_for_every_profile(status: dict[str, str]) -> None:
    """ROADMAP v0.1 #4: a new HA entity is unreachable by every profile until granted
    *and* dispositioned: even an explicit entity grant does not make a 'new' row resolvable."""
    state = _state(status, set(UUIDS), set(AREAS), "sam")
    visible = {e.uuid for e in _index(state).visible(_principal("sam"))}
    assert not {u for u, s in status.items() if s != "active"} & visible


def test_fixture_examples() -> None:
    state = _state({}, set(UUIDS), set(), "sam")
    index = _index(state)
    p = _principal("sam")

    def names(q: TargetQuery) -> list[str]:
        return sorted(e.name for e in resolve(p, q, index).targets)

    assert names(TargetQuery(name="kitchen lights")) == ["Counter lights", "Kitchen ceiling"]
    assert names(TargetQuery(name="the reading lamp")) == ["Lamp"]
    assert names(TargetQuery(name="lounge lamp")) == ["Lamp"]  # area alias split out
    assert names(TargetQuery(name="telly")) == ["TV"]  # registry alias
    assert names(TargetQuery(name="kitchn ceiling")) == ["Kitchen ceiling"]  # fuzzy
    assert names(TargetQuery(area="upstairs", domain="light", all=True)) == [
        "Bedroom lamp",
        "Kids light",
    ]  # floor
    assert names(TargetQuery(name="garage door")) == ["Garage door"]  # alias "door" does not tie
    assert resolve(p, TargetQuery(name="lamp"), index).status == "ambiguous"
    assert resolve(p, TargetQuery(name="flux capacitor"), index).status == "none"
    assert resolve(p, TargetQuery(name="lights", area="attic"), index).status == "none"
    on = resolve(p, TargetQuery(name="lights", state_filter="on"), index)
    assert sorted(e.name for e in on.targets) == ["Bedroom lamp", "Kitchen ceiling"]
