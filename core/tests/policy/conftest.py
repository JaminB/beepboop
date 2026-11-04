"""Fixtures and Hypothesis strategies for the policy tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import strategies as st

from beepboop.policy import (
    ALL_SCOPES,
    RISKS,
    EntityInfo,
    Grant,
    GrantIndex,
    PolicyState,
    Principal,
    ProfileView,
    Risk,
    SimpleTool,
    Target,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

ALEX = ProfileView("alex", "admin", "critical", frozenset({"household", "adults"}), has_login=True)
SAM = ProfileView("sam", "member", "critical", frozenset({"household", "adults"}), has_login=True)
MIA = ProfileView("mia", "child", "act", frozenset({"household", "children"}))
GUEST = ProfileView("guest", "guest", "act")
PROFILES = {p.id: p for p in (ALEX, SAM, MIA, GUEST)}

LOCK = EntityInfo("lock-uuid", "active", area_id="hall")
KITCHEN_LIGHT = EntityInfo("kitchen-light", "active", area_id="kitchen")
NEW_DEVICE = EntityInfo("new-uuid", "new", area_id="kitchen")
ENTITIES = {e.uuid: e for e in (LOCK, KITCHEN_LIGHT, NEW_DEVICE)}

GRANTS = [
    Grant("g1", "group", "household", "tool_glob", "home.*", "critical", source="preset:member"),
    Grant("g2", "group", "household", "ha_area", "kitchen", "act", source="inbox"),
    Grant("g3", "group", "adults", "ha_entity", "lock-uuid", "critical", source="inbox"),
    Grant("g4", "profile", "alex", "tool_glob", "caldav.*", "personal", source="preset:admin"),
    Grant("g5", "profile", "guest", "tool_glob", "home.state", "read", source="preset:guest"),
    Grant("g6", "profile", "guest", "tool_glob", "timer.*", "act", source="preset:guest"),
    Grant("g7", "group", "household", "tool_glob", "timer.*", "act", source="preset:member"),
    Grant("g8", "group", "children", "tool_glob", "home.*", "act", source="preset:child"),
]


@pytest.fixture
def state() -> PolicyState:
    return PolicyState(profiles=PROFILES, grants=GrantIndex.build(GRANTS), entities=ENTITIES)


def voice(
    profile_id: str,
    confidence: float = 0.95,
    margin: float = 0.3,
    *,
    tainted: bool = False,
    device: str = "living-room",
) -> Principal:
    return Principal(
        profile_id=profile_id,
        via="voice",
        confidence=confidence,
        margin=margin,
        device=device,
        scopes=ALL_SCOPES,
        tainted=tainted,
        turn_id="01TURN",
    )


def web(profile_id: str, *, actor: str | None = None, tainted: bool = False) -> Principal:
    return Principal(
        profile_id=profile_id,
        via="web",
        confidence=None,
        margin=None,
        device="web:abc",
        scopes=ALL_SCOPES,
        tainted=tainted,
        turn_id="01TURN",
        actor_profile_id=actor,
    )


# ------------------------------------------------------------- strategies --

risks: st.SearchStrategy[Risk] = st.sampled_from(RISKS)
tool_names = st.sampled_from(
    ["home.turn_on", "home.state", "timer.set", "caldav.events", "lock.unlock", "parcel.track"]
)
tools = st.builds(SimpleTool, name=tool_names, risk=risks)
profile_ids = st.sampled_from(list(PROFILES))
principals = st.builds(
    voice,
    profile_id=profile_ids,
    confidence=st.floats(0, 1),
    margin=st.floats(0, 1),
    tainted=st.booleans(),
)
targets = st.lists(
    st.builds(
        Target,
        uuid=st.sampled_from([*list(ENTITIES), "unknown-uuid"]),
        domain=st.sampled_from(["light", "lock", "cover", "homeassistant"]),
        service=st.sampled_from(["turn_on", "unlock", "open_cover", "turn_off", None]),
        device_class=st.sampled_from([None, "garage", "door"]),
        declared_risk=st.one_of(st.none(), risks),
    ),
    max_size=3,
)
