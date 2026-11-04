"""Grants, schedules, presets and the HA service table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beepboop.policy import (
    PRESET_NAMES,
    EntityInfo,
    Grant,
    GrantIndex,
    Schedule,
    ScheduleError,
    ServiceTable,
    apply_preset,
    load_preset,
    load_service_table,
    parse_service_table,
    schedule_active,
)

LA = ZoneInfo("America/Los_Angeles")


def test_schedule_roundtrip_and_window() -> None:
    s = Schedule.from_json('{"days":["mon","tue"],"start":"07:00","end":"20:30"}')
    assert s is not None
    assert Schedule.from_json(s.to_json()) == s
    monday_noon = datetime(2026, 11, 2, 12, 0, tzinfo=LA)
    assert s.is_active(monday_noon, LA)
    assert not s.is_active(monday_noon.replace(hour=21), LA)
    assert not s.is_active(monday_noon + timedelta(days=2), LA)  # wednesday


def test_schedule_wraps_midnight() -> None:
    s = Schedule.from_json('{"start":"22:00","end":"06:00"}')
    assert s is not None
    assert s.is_active(datetime(2026, 11, 2, 23, 0, tzinfo=LA), LA)
    assert s.is_active(datetime(2026, 11, 3, 5, 0, tzinfo=LA), LA)
    assert not s.is_active(datetime(2026, 11, 3, 12, 0, tzinfo=LA), LA)


def test_schedule_expiry_is_an_instant() -> None:
    s = Schedule.from_json('{"expires_at":"2026-11-01T23:30:00-05:00"}')
    assert s is not None
    assert schedule_active(s, datetime(2026, 11, 2, 4, 29, tzinfo=UTC), UTC)
    assert not schedule_active(s, datetime(2026, 11, 2, 4, 30, tzinfo=UTC), UTC)
    assert schedule_active(None, datetime.now(UTC), UTC)


@pytest.mark.parametrize(
    "raw",
    [
        '{"days":["funday"]}',
        '{"start":"7am"}',
        '{"expires_at":"2026-11-01T23:30:00"}',
        "[1]",
        "{bad",
    ],
)
def test_schedule_rejects_bad_json(raw: str) -> None:
    with pytest.raises(ScheduleError):
        Schedule.from_json(raw)


def test_grant_covers_tool_glob() -> None:
    g = Grant("g", "group", "household", "tool_glob", "home.*", "act")
    assert g.covers_tool("home.turn_on")
    assert not g.covers_tool("homework.do")
    assert Grant("g", "group", "household", "tool_glob", "*", "act").covers_tool("x.y")


def test_grant_covers_entity_requires_active_for_area_and_config_entry() -> None:
    area = Grant("a", "group", "household", "ha_area", "kitchen", "act")
    entry = Grant("c", "profile", "alex", "ha_config_entry", "spot", "personal")
    direct = Grant("d", "profile", "alex", "ha_entity", "u1", "critical")
    active = EntityInfo("u1", "active", area_id="kitchen", config_entry_id="spot")
    new = EntityInfo("u1", "new", area_id="kitchen", config_entry_id="spot")
    assert area.covers_entity(active)
    assert entry.covers_entity(active)
    assert direct.covers_entity(active)
    assert not area.covers_entity(new)
    assert not entry.covers_entity(new)
    assert direct.covers_entity(new)  # an explicit uuid grant is the inbox disposition itself


def test_grant_index_union_of_profile_and_groups() -> None:
    idx = GrantIndex.build(
        [
            Grant("1", "profile", "alex", "tool_glob", "a.*", "act"),
            Grant("2", "group", "adults", "tool_glob", "b.*", "act"),
            Grant("3", "group", "children", "tool_glob", "c.*", "act"),
        ]
    )
    assert {g.id for g in idx.for_subjects("alex", frozenset({"adults"}))} == {"1", "2"}
    assert len(idx) == 3


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_presets_load_and_render(name: str) -> None:
    preset = load_preset(name)
    counter = iter(range(100))
    rows = apply_preset(name, "p1", lambda: f"g{next(counter)}")
    assert rows.kind == name
    assert rows.risk_ceiling == preset.risk_ceiling
    assert all(g.subject_id == "p1" and g.source == f"preset:{name}" for g in rows.grants)
    assert len(rows.grants) == len(preset.grants)


def test_child_and_guest_ceiling_is_act() -> None:
    assert load_preset("child").risk_ceiling == "act"
    assert load_preset("guest").risk_ceiling == "act"
    assert load_preset("guest").groups == []


# ------------------------------------------------------------- service table --


def test_service_table_classes() -> None:
    t = load_service_table()
    assert t.classify("light", "turn_on") == "direct"
    assert t.classify("script", "turn_on") == "indirect"
    assert t.classify("homeassistant", "turn_off") == "blocked"
    assert t.classify("made_up", "x") == "blocked"
    assert t.risk("lock", "unlock", None) == "critical"
    assert t.risk("lock", "lock", None) == "act"
    assert t.risk("cover", "open_cover", "garage") == "critical"
    assert t.risk("cover", "open_cover", "blind") == "act"
    assert t.risk("script", "turn_on", None) == "critical"
    assert t.risk("light", None, None) == "read"


def test_service_table_rejects_overlap() -> None:
    with pytest.raises(ValueError, match="two classes"):
        parse_service_table("direct: {light: {'*': act}}\nblocked: [light]\n")


@given(
    domain=st.sampled_from(["light", "lock", "cover", "script", "homeassistant", "nope"]),
    service=st.sampled_from(["turn_on", "unlock", "open_cover", None]),
    dc=st.sampled_from([None, "garage", "blind"]),
)
def test_service_risk_total_and_consistent(
    domain: str, service: str | None, dc: str | None
) -> None:
    t: ServiceTable = load_service_table()
    if t.is_blocked(domain, service):
        return
    assert t.risk(domain, service, dc) in ("read", "act", "personal", "critical")
