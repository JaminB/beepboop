"""Resolver (HOME_ASSISTANT §6.3) on a fixture household, and the BM25 ``find_tools`` index."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from beepboop.module import EntityRef
from beepboop.policy import ALL_SCOPES, Principal
from beepboop.registry import (
    Area,
    StaticEntityIndex,
    TargetQuery,
    ToolIndex,
    load_manifest,
    module_cards,
    resolve,
)
from beepboop.registry.find_tools import agent_md_terms

from .conftest import TEMPLATE_DIR

KITCHEN_1 = EntityRef("u1", "light.kitchen_ceiling", "light", "Kitchen ceiling", area_id="kitchen")
KITCHEN_2 = EntityRef("u2", "light.kitchen_counter", "light", "Counter lights", area_id="kitchen")
LAMP = EntityRef(
    "u3", "light.living_lamp", "light", "Lamp", aliases=("reading lamp",), area_id="living"
)
LOCK = EntityRef("u4", "lock.front_door", "lock", "Front door", area_id="hall", device_class=None)
TV = EntityRef("u5", "media_player.tv", "media_player", "TV", area_id="living", device_class="tv")
AREAS = [
    Area("kitchen", "Kitchen"),
    Area("living", "Living room", ("lounge",)),
    Area("hall", "Hall"),
]


def index(visible: dict[str, frozenset[str]] | None = None) -> StaticEntityIndex:
    return StaticEntityIndex([KITCHEN_1, KITCHEN_2, LAMP, LOCK, TV], AREAS, visible or {})


def principal(pid: str = "alex") -> Principal:
    return Principal(pid, "voice", 0.9, 0.3, "main", ALL_SCOPES, False, "T")


def test_area_query_returns_every_light_in_the_area() -> None:
    r = resolve(principal(), TargetQuery(name="kitchen lights"), index())
    assert r.status == "ok"
    assert {e.uuid for e in r.targets} == {"u1", "u2"}
    assert r.fastpath_ok


def test_alias_and_singular_name() -> None:
    r = resolve(principal(), TargetQuery(name="the reading lamp"), index())
    assert r.status == "ok"
    assert [e.uuid for e in r.targets] == ["u3"]


def test_ambiguous_when_two_distinct_devices_tie() -> None:
    idx = StaticEntityIndex(
        [
            EntityRef("a", "light.lamp_a", "light", "Lamp", area_id="kitchen"),
            EntityRef("b", "light.lamp_b", "light", "Lamp", area_id="living"),
        ],
        AREAS,
    )
    r = resolve(principal(), TargetQuery(name="lamp"), idx)
    assert r.status == "ambiguous"
    assert len(r.candidates) == 2


def test_denied_when_only_the_full_index_matches() -> None:
    idx = index({"mia": frozenset({"u1", "u2"})})
    r = resolve(principal("mia"), TargetQuery(name="front door", domain="lock"), idx)
    assert r.status == "denied"
    assert [e.uuid for e in r.denied] == ["u4"]
    guest = index({"guest": frozenset()})
    assert resolve(principal("guest"), TargetQuery(name="front door"), guest).status == "none"


def test_none_for_unknown_words() -> None:
    assert resolve(principal(), TargetQuery(name="flux capacitor"), index()).status == "none"


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=30))
def test_resolver_is_total_and_deterministic(name: str) -> None:
    r1 = resolve(principal(), TargetQuery(name=name), index())
    r2 = resolve(principal(), TargetQuery(name=name), index())
    assert r1 == r2
    assert r1.status in ("ok", "ambiguous", "none", "denied")
    if r1.status != "ok":
        assert r1.targets == ()


def test_find_tools_index_and_cards() -> None:
    m = load_manifest(TEMPLATE_DIR)
    agent_md = (TEMPLATE_DIR / "AGENT.md").read_text(encoding="utf-8")
    idx = ToolIndex.build([(m, t, agent_md) for t in m.tools])
    hits = idx.search("where is my package")
    assert hits
    assert hits[0].name == "parcel.track"
    assert idx.search("stop tracking the shoes")[0].name == "parcel.remove"
    assert idx.search("where is my package", allowed=["parcel.add"]) == [] or all(
        h.name == "parcel.add" for h in idx.search("where is my package", allowed=["parcel.add"])
    )
    assert "When to use" in agent_md_terms(agent_md)
    cards = module_cards([(m, "reauth")], profile_name="Alice")
    assert cards.splitlines()[0] == "Connected for Alice (call find_tools to load these):"
    assert "- parcel — " in cards
    assert "(needs reconnecting)" in cards
