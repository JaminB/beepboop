"""Tool → service plan (HOME_ASSISTANT §9), the registry index (§6.1), the state cache
(§5), and the read-tier rule of the service table."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from beepboop.ha import plan
from beepboop.ha.cache import StateCache
from beepboop.ha.index import RegistryIndex
from beepboop.policy import load_service_table
from beepboop.registry.loader import import_module_file
from beepboop.registry.resolver import StaticEntityIndex

from .fake_ws import load_household
from .rig import HA_MODULE


def _module_tables() -> dict[str, object]:
    klass = import_module_file(HA_MODULE)
    mod = __import__(klass.__module__, fromlist=["TURN_ON"])
    return {k: getattr(mod, k) for k in ("TURN_ON", "TURN_OFF", "SET", "MEDIA")}


def test_module_and_core_use_the_same_service_tables() -> None:
    """The extractor raises the tier from these tables; the handler calls from its copy."""
    tables = _module_tables()
    assert dict(tables["TURN_ON"]) == dict(plan.TURN_ON)  # type: ignore[call-overload]
    assert dict(tables["TURN_OFF"]) == dict(plan.TURN_OFF)  # type: ignore[call-overload]
    assert {k: dict(v) for k, v in tables["SET"].items()} == {  # type: ignore[attr-defined]
        k: dict(v) for k, v in plan.SET.items()
    }
    assert dict(tables["MEDIA"]) == dict(plan.MEDIA)  # type: ignore[call-overload]


def _all_services() -> list[tuple[str, str]]:
    pairs = [(d, s) for d, s in plan.TURN_ON.items()] + [(d, s) for d, s in plan.TURN_OFF.items()]
    pairs += [(d, s) for by_domain in plan.SET.values() for d, s in by_domain.items()]
    pairs += [("media_player", s) for s in plan.MEDIA.values()]
    return sorted(set(pairs))


@pytest.mark.parametrize(("domain", "service"), _all_services())
def test_every_planned_service_is_allowed_by_ha_services_yaml(domain: str, service: str) -> None:
    table = load_service_table()
    assert table.classify(domain, service) in ("direct", "indirect")


def test_tiers_from_the_table() -> None:
    table = load_service_table()
    assert table.risk("lock", "lock", None) == "act"
    assert table.risk("lock", "unlock", None) == "critical"
    assert table.risk("alarm_control_panel", "alarm_disarm", None) == "critical"
    assert table.risk("cover", "open_cover", "garage") == "critical"
    assert table.risk("cover", "open_cover", "curtain") == "act"
    assert table.risk("scene", "turn_on", None) == "critical"
    # Pure reads: every non-blocked domain is readable at read, floors do not apply.
    assert table.classify("sensor", None) == "direct"
    assert table.risk("sensor", None, "temperature") == "read"
    assert table.risk("cover", None, "garage") == "read"
    assert table.risk("script", None, None) == "read"
    assert table.is_blocked("homeassistant", None)
    assert table.is_blocked("sensor", "set_value")  # no service of an unlisted domain


def test_service_for_and_query_from_args() -> None:
    assert plan.service_for("home.turn_off", "lock", {}) == "unlock"
    assert plan.service_for("home.set", "light", {"attribute": "brightness"}) == "turn_on"
    assert plan.service_for("home.set", "lock", {"attribute": "brightness"}) is None
    assert plan.service_for("media.control", "media_player", {"action": "pause"}) == "media_pause"
    assert plan.service_for("media.control", "light", {"action": "pause"}) is None
    assert plan.service_for("home.state", "light", {}) is None
    q = plan.query_from_args("home.set", {"target": {"name": "lamp"}, "attribute": "temperature"})
    assert q is not None
    assert q.domain == "climate"
    q = plan.query_from_args("home.turn_on", {"target": {"area": "kitchen", "all": True}})
    assert q is not None
    assert q.all
    assert q.area == "kitchen"
    assert plan.query_from_args("home.state", {}) is None
    assert plan.query_from_args("home.state", {"target": {}}) is None


def _players(states: Mapping[str, str]) -> StaticEntityIndex:
    from beepboop.module import EntityRef

    refs = [EntityRef(u, f"media_player.{u}", "media_player", u) for u in states]
    return StaticEntityIndex(refs, states=dict(states))


def test_default_players() -> None:
    from beepboop.policy import ALL_SCOPES, Principal

    p = Principal("sam", "voice", 0.9, 0.3, "main", ALL_SCOPES, False, "T")
    one_playing = _players({"a": "playing", "b": "off"})
    assert [e.uuid for e in plan.default_players(one_playing, p, "pause")] == ["a"]
    assert plan.default_players(one_playing, p, "play") == []
    assert [
        e.uuid for e in plan.default_players(_players({"a": "paused", "b": "off"}), p, "play")
    ] == ["a"]
    assert [e.uuid for e in plan.default_players(_players({"only": "off"}), p, "pause")] == ["only"]
    assert plan.default_players(_players({"a": "playing", "b": "playing"}), p, "pause") == []


def test_registry_index_build_rules() -> None:
    raw = load_household()
    cache = StateCache()
    cache.replace_all(raw["states"])
    index = RegistryIndex.build(
        entity_list=raw["entities"],
        device_list=raw["devices"],
        area_list=raw["areas"],
        floor_list=raw["floors"],
        extended=raw["extended"],
        states=cache.all(),
    )
    ceiling = index.entities["u-kitchen-ceiling"].ref
    assert ceiling.name == "Kitchen ceiling"  # friendly_name over device name
    assert ceiling.area_id == "kitchen"
    assert ceiling.floor_id == "ground"
    assert index.entities["u-kitchen-counter"].ref.name == "Counter lights"  # registry name
    assert index.entities["u-tv"].ref.device_class == "tv"  # original_device_class
    assert index.entities["u-hidden"].ref.hidden
    assert index.entities["u-disabled"].ref.disabled
    assert index.entities["u-tpl-switch"].is_template_switch
    assert index.get("lock.front_door") is index.entities["u-front-door"]
    assert index.floors["upstairs"].aliases == ("first floor",)
    # Malformed rows are skipped, not fatal.
    junk = RegistryIndex.build(entity_list=[{"id": "x"}, "nope", {"id": "y", "entity_id": "bad"}])
    assert junk.entities == {}
    assert RegistryIndex.build(entity_list=None).entities == {}


def test_state_cache() -> None:
    cache = StateCache()
    assert cache.stale
    cache.replace_all([{"entity_id": "light.a", "state": "on", "context": {"id": "c"}}, {"bad": 1}])
    assert not cache.stale
    assert len(cache) == 1
    a = cache.get("light.a")
    assert a is not None
    assert a.context_id == "c"
    cache.apply_state_changed(
        {"entity_id": "light.a", "new_state": {"entity_id": "light.a", "state": "off"}}
    )
    a = cache.get("light.a")
    assert a is not None
    assert a.state == "off"
    cache.apply_state_changed({"entity_id": 3})
    cache.apply_state_changed({"entity_id": "light.a", "new_state": None})
    assert cache.get("light.a") is None


def test_registry_index_from_mirror_resolves_names_without_ha() -> None:
    """``policy explain`` and a restart with HA down resolve from ``ha_entities``."""
    from beepboop.db.repo.ha_entities import EntityRow

    rows = [
        EntityRow(
            "u1",
            "light.kitchen_ceiling",
            "light",
            "active",
            area_id="kitchen",
            friendly_name="Kitchen ceiling",
            aliases=("big light",),
        ),
        EntityRow("u2", "lock.front_door", "lock", "active", device_class="door"),
        EntityRow("u3", "light.gone", "light", "active", removed_at=1),
    ]
    index = RegistryIndex.from_mirror(rows)
    assert set(index.entities) == {"u1", "u2"}
    assert index.get("light.kitchen_ceiling") is not None
    assert index.entities["u1"].ref.area_id == "kitchen"
    assert index.areas["kitchen"].name == "Kitchen"
    assert index.entities["u2"].ref.name == "front door"
    assert index.entities["u2"].ref.device_class == "door"
