"""``HAService`` sync (HOME_ASSISTANT §4-§6) and the new-device inbox mirror
(IDENTITY_AND_TRUST §6.2), against the fake WebSocket and a real database."""

from __future__ import annotations

import sqlite3

from beepboop.db import repo
from beepboop.db.repo.ha_entities import EntityRow
from beepboop.ha.inbox import sync_entities
from beepboop.ha.index import RegistryIndex
from beepboop.policy import Grant, load_service_table

from .fake_ws import FakeHAWebSocket, load_household
from .rig import STILL_NEW, Rig, until


async def _rows(rig: Rig) -> dict[str, EntityRow]:
    rows = await rig.db.read(lambda c: repo.ha_entities.list_(c))
    return {r.uuid: r for r in rows}


async def test_startup_sequence_follows_section_4(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    order = [c for c in fake_ws.commands if c not in ("ping",)]
    expected_prefix = [
        "supported_features",
        "get_config",
        "get_states",
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
        "config/floor_registry/list",
        "config/entity_registry/get_entries",
        "config_entries/get",
    ]
    assert order[: len(expected_prefix)] == expected_prefix
    for event in ("state_changed", "entity_registry_updated", "area_registry_updated"):
        assert fake_ws.subscribed(event)
    assert "config_entries/subscribe" in order
    assert "get_services" in order
    assert rig.ha.health == "ok"
    assert rig.ha.services()["lock"].keys() >= {"lock", "unlock"}
    assert rig.ha.config_entries["ce-hue"].title == "Philips Hue"


async def test_every_entity_lands_in_the_inbox_as_new(rig: Rig) -> None:
    rows = await _rows(rig)
    assert len(rows) == len(load_household()["entities"])
    assert rig.ha.last_sync is not None
    # The rig dispositioned all but STILL_NEW; the first sync inserted every one as new.
    assert {u for u, r in rows.items() if r.status == "new"} == STILL_NEW
    # Indirect services start critical: scene, script, template switch.
    assert rows["u-movie"].declared_risk == "critical"
    assert rows["u-away-script"].declared_risk == "critical"
    assert rows["u-tpl-switch"].declared_risk == "critical"
    assert rows["u-kitchen-ceiling"].declared_risk is None
    # Names: registry name > friendly_name > original_name > device name.
    assert rows["u-kitchen-ceiling"].friendly_name == "Kitchen ceiling"
    assert rows["u-front-door"].friendly_name == "Front door"
    assert rows["u-front-door"].area_id == "hall"  # from the device
    assert rows["u-garage"].device_class == "garage"  # from get_entries
    assert rows["u-living-lamp"].aliases == ("reading lamp",)


async def test_state_changed_updates_the_cache(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    assert rig.ha.cache.get("light.kitchen_counter") is not None
    new_state = {
        "entity_id": "light.kitchen_counter",
        "state": "on",
        "attributes": {"brightness": 10},
    }
    await fake_ws.fire(
        "state_changed",
        {"entity_id": "light.kitchen_counter", "old_state": None, "new_state": new_state},
    )
    await until(
        lambda: (s := rig.ha.cache.get("light.kitchen_counter")) is not None and s.state == "on"
    )
    await fake_ws.fire("state_changed", {"entity_id": "light.kitchen_counter", "new_state": None})
    await until(lambda: rig.ha.cache.get("light.kitchen_counter") is None)


async def test_rename_keeps_uuid_status_and_grants(rig: Rig, fake_ws: FakeHAWebSocket) -> None:
    for e in fake_ws.entities:
        if e["id"] == "u-front-door":
            e["entity_id"] = "lock.main_door"
    await fake_ws.fire(
        "entity_registry_updated",
        {"action": "update", "entity_id": "lock.main_door", "old_entity_id": "lock.front_door"},
    )
    await until(lambda: "lock.main_door" in rig.ha.registry.by_entity_id)
    await until(lambda: rig.ha.last_sync is not None and "u-front-door" in rig.ha.last_sync.updated)
    row = (await _rows(rig))["u-front-door"]
    assert row.entity_id == "lock.main_door"
    assert row.status == "active"  # the disposition survives the rename
    assert rig.state().entities["u-front-door"].status == "active"


async def test_new_entity_event_goes_to_inbox_and_removed_is_marked(
    rig: Rig, fake_ws: FakeHAWebSocket
) -> None:
    fake_ws.entities.append(
        {
            "id": "u-new-plug",
            "entity_id": "switch.new_plug",
            "name": "New plug",
            "area_id": "kitchen",
            "platform": "tplink",
            "config_entry_id": "ce-tplink",
        }
    )
    fake_ws.entities[:] = [e for e in fake_ws.entities if e["id"] != "u-blinds"]
    await fake_ws.fire(
        "entity_registry_updated", {"action": "create", "entity_id": "switch.new_plug"}
    )
    await until(lambda: rig.ha.last_sync is not None and "u-new-plug" in rig.ha.last_sync.inserted)
    rows = await _rows(rig)
    assert rows["u-new-plug"].status == "new"
    assert rows["u-blinds"].removed_at is not None
    # Default deny: the kitchen area grant does not cover a 'new' entity.
    assert rig.state().entities["u-new-plug"].status == "new"


async def test_get_entries_refusal_is_tolerated(tmp_path: object, fake_ws: FakeHAWebSocket) -> None:
    fake_ws.refuse_get_entries = True
    raw = load_household()
    index = RegistryIndex.build(entity_list=raw["entities"], area_list=raw["areas"])
    assert index.get("cover.garage_door") is not None
    assert index.entities["u-garage"].ref.device_class is None  # no extended data


# ------------------------------------------------------------- pure inbox --


def _schema(conn: sqlite3.Connection) -> None:
    from beepboop.db.migrate import migrate

    migrate(conn)


def test_sync_entities_personal_config_entry_skips_inbox(tmp_path: object) -> None:
    import pathlib

    path = pathlib.Path(str(tmp_path)) / "x.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _schema(conn)
    repo.profiles.insert(conn, "Alex", "admin", "critical", profile_id="alex")
    repo.grants.insert(
        conn,
        Grant(
            "g1",
            "profile",
            "alex",
            "ha_config_entry",
            "ce-alex-mobile",
            "personal",
            source="auto:config_entry",
        ),
    )
    raw = load_household()
    index = RegistryIndex.build(entity_list=raw["entities"], extended=raw["extended"])
    result = sync_entities(conn, index, load_service_table())
    assert result.changed
    assert len(result.inserted) == len(raw["entities"])
    status = {r.uuid: r.status for r in repo.ha_entities.list_(conn)}
    assert status["u-alex-phone"] == "active"
    assert status["u-kitchen-ceiling"] == "new"
    # A second sync with nothing changed writes nothing.
    again = sync_entities(conn, index, load_service_table())
    assert not again.changed
    conn.close()
